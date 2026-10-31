"""Domain allow-list enforcement for sandboxes: a host-side CONNECT proxy.

The sandbox network toggle is binary (on/off); this adds per-sandbox domain
allow-lists. When ``allowed_domains`` is set on a sandbox, the host starts an
ephemeral :class:`DomainAllowProxy` and the sandbox is pointed at it via the
standard ``HTTP_PROXY`` / ``HTTPS_PROXY`` environment variables. The proxy
tunnels ``CONNECT host:port`` (HTTPS and other TLS traffic) and absolute-form
plain-HTTP requests **only** when the target host matches the allow-list
(exact or subdomain, case-insensitive); everything else gets an explicit 403.

This is proxy/DNS-level enforcement, not a firewall: a process inside the
sandbox that ignores the proxy env and speaks raw sockets to a numeric IP is
not covered (documented in KNOWN_LIMITS).
"""

from __future__ import annotations

import asyncio
import contextlib

from backend.core.logging import get_logger

log = get_logger(__name__)

PROXY_CONNECT_OK = b"HTTP/1.1 200 Connection established\r\n\r\n"
PROXY_DENIED = (b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n")
PROXY_BAD_REQUEST = (b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                     b"Connection: close\r\n\r\n")


def host_allowed(host: str, allowed: list[str]) -> bool:
    """Exact or subdomain match, case-insensitive, port stripped."""
    name = (host or "").strip().lower()
    if ":" in name and not name.startswith("["):  # host:port
        name = name.rsplit(":", 1)[0]
    name = name.strip("[]")
    if not name:
        return False
    for entry in allowed:
        rule = (entry or "").strip().lower().rstrip(".")
        if not rule:
            continue
        if name == rule or name.endswith("." + rule):
            return True
    return False


class DomainAllowProxy:
    """Ephemeral asyncio TCP server enforcing an allow-list over HTTP proxying."""

    def __init__(self, allowed_domains: list[str], *, listen_host: str = "127.0.0.1", listen_port: int = 0) -> None:
        self.allowed = list(allowed_domains)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server: asyncio.AbstractServer | None = None
        self._tunnels: set[asyncio.Task[None]] = set()

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, self.listen_host, self.listen_port)
        addr = self._server.sockets[0].getsockname()
        self.listen_host, self.listen_port = addr[0], int(addr[1])
        log.info("sandbox allow-list proxy on %s:%d (%d domains)",
                 self.listen_host, self.listen_port, len(self.allowed))
        return self.listen_host, self.listen_port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._tunnels):
            task.cancel()
        if self._tunnels:
            await asyncio.gather(*self._tunnels, return_exceptions=True)
        self._tunnels.clear()

    # ------------------------------------------------------------------ internals
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tunnels.add(task)
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not request_line:
                return
            parts = request_line.decode("latin-1", "replace").split()
            if len(parts) < 2:
                writer.write(PROXY_BAD_REQUEST)
                await writer.drain()
                return
            method, target = parts[0].upper(), parts[1]
            # a proxy must consume the client's full header block before acting,
            # or the leftover bytes would leak into the tunnel
            header_lines: list[bytes] = []
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30)
                if line in (b"\r\n", b"\n", b""):
                    break
                header_lines.append(line)
            if method == "CONNECT":
                await self._tunnel(reader, writer, target)
            elif target.lower().startswith("http://"):
                # absolute-form plain HTTP: validate the target host, then forward
                host = _host_from_authority(target.split("//", 1)[1].split("/", 1)[0])
                if not host_allowed(host, self.allowed):
                    writer.write(PROXY_DENIED)
                    await writer.drain()
                    return
                await self._forward_absolute(writer, target, host, header_lines)
            else:
                # relative-form (transparent proxying) can't be attributed to an
                # allow-list rule — deny conservatively
                writer.write(PROXY_DENIED)
                await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("allow-list proxy handler failed")
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            if task is not None:
                self._tunnels.discard(task)

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      authority: str) -> None:
        host = _host_from_authority(authority)
        port = _port_from_authority(authority, default=443)
        if not host_allowed(host, self.allowed):
            writer.write(PROXY_DENIED)
            await writer.drain()
            return
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15)
        except (TimeoutError, OSError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        writer.write(PROXY_CONNECT_OK)
        await writer.drain()
        await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer),
                             return_exceptions=True)
        try:
            remote_writer.close()
            await remote_writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _forward_absolute(self, writer: asyncio.StreamWriter, target: str, host: str,
                                header_lines: list[bytes]) -> None:
        """Forward one plain-HTTP request (absolute-form URI) to the origin."""
        port = _port_from_authority(target.split("//", 1)[1].split("/", 1)[0], default=80)
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15)
        except (OSError, TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        try:
            remote_writer.write(f"GET {target} HTTP/1.0\r\n".encode("latin-1"))
            for line in header_lines:
                lowered = line.lower()
                if lowered.startswith(b"proxy-") or lowered.startswith(b"keep-alive"):
                    continue
                remote_writer.write(line)
            remote_writer.write(b"\r\n")
            await remote_writer.drain()
            while True:
                chunk = await remote_reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            remote_writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await remote_writer.wait_closed()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(ConnectionError, OSError):
            dst.close()


def _host_from_authority(authority: str) -> str:
    authority = authority.strip()
    if "@" in authority:  # user:pass@host
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):  # IPv6 [::1]:443
        return authority.split("]", 1)[0].lstrip("[")
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


def _port_from_authority(authority: str, *, default: int) -> int:
    authority = authority.strip()
    if authority.startswith("["):
        rest = authority.split("]", 1)[1]
        return int(rest.lstrip(":")) if rest.startswith(":") else default
    if ":" in authority:
        try:
            return int(authority.rsplit(":", 1)[1])
        except ValueError:
            return default
    return default
