"""Domain allow-list proxy: unit + live-loopback tests (no Docker required)."""
from __future__ import annotations

import asyncio

import pytest

from backend.sandbox.netfilter import DomainAllowProxy, host_allowed


class TestHostAllowed:
    def test_exact(self):
        assert host_allowed("example.com", ["example.com"])

    def test_subdomain(self):
        assert host_allowed("api.example.com", ["example.com"])

    def test_unrelated(self):
        assert not host_allowed("evil.com", ["example.com"])

    def test_suffix_not_enough(self):
        assert not host_allowed("badexample.com", ["example.com"])

    def test_case_and_port(self):
        assert host_allowed("API.Example.COM:443", ["example.com"])

    def test_empty(self):
        assert not host_allowed("", ["example.com"])
        assert not host_allowed("example.com", [])


class _Origin:
    """Tiny echo server: replies `OK <first line>` then closes."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            line = await reader.readline()
            writer.write(b"OK " + line)
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


async def _connect_via_proxy(proxy: DomainAllowProxy, authority: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    await writer.drain()
    status_line = await reader.readline()
    code = int(status_line.split()[1])
    # consume the rest of the response headers (empty line terminates)
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return code, (status_line + b"|")


@pytest.mark.asyncio
async def test_connect_allowed_and_denied():
    origin = _Origin()
    await origin.start()
    proxy = DomainAllowProxy(["localhost"])
    await proxy.start()
    try:
        # allowed: CONNECT localhost:<origin> succeeds and data flows
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
        writer.write(f"CONNECT localhost:{origin.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        writer.write(b"hello origin\n")
        await writer.drain()
        reply = await reader.readline()
        assert reply == b"OK hello origin\n"
        writer.close()

        # denied: a non-allow-listed host gets 403
        code, _ = await _connect_via_proxy(proxy, "evil.example.net:443")
        assert code == 403

        # a lookalike suffix that is NOT a subdomain is denied too
        code, _ = await _connect_via_proxy(proxy, "badlocalhost:443")
        assert code == 403
    finally:
        await proxy.stop()
        await origin.stop()


@pytest.mark.asyncio
async def test_plain_http_denied_for_unknown_host():
    proxy = DomainAllowProxy(["localhost"])
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.listen_port)
        writer.write(b"GET http://evil.example.net/x HTTP/1.1\r\nHost: evil.example.net\r\n\r\n")
        await writer.drain()
        status = await reader.readline()
        assert b"403" in status
        writer.close()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_stop_closes_listener():
    proxy = DomainAllowProxy(["localhost"])
    host, port = await proxy.start()
    await proxy.stop()
    with pytest.raises((ConnectionError, OSError)):
        await asyncio.open_connection(host, port)
