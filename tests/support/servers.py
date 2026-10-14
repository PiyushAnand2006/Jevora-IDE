"""Test servers: threaded uvicorn runner and a scriptable mock OpenAI-compatible LLM."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ThreadedServer:
    """Run an ASGI app with uvicorn in a background thread (its own event loop)."""

    def __init__(self, app: Any, port: int | None = None) -> None:
        self.port = port or free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on",
                                ws="websockets-sansio")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        loop = asyncio.ProactorEventLoop() if hasattr(asyncio, "ProactorEventLoop") else asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.server.serve())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> ThreadedServer:
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("server failed to start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


@dataclass
class MockReply:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    delay: float = 0.0
    drop_after_chunks: int | None = None  # stream: close the connection after N chunks
    headers: dict[str, str] = field(default_factory=dict)
    prompt_tokens: int = 11
    completion_tokens: int = 7


Handler = Callable[[dict[str, Any]], MockReply]


class MockLLM:
    """A minimal OpenAI-compatible server whose replies are scripted per test."""

    def __init__(self, models: list[str] | None = None) -> None:
        self.models = models if models is not None else ["mock-mini", "mock-pro", "mock-embed"]
        self.handler: Handler = lambda body: MockReply(content="ok")
        self.requests: list[dict[str, Any]] = []
        self.models_status = 200
        self.models_delay = 0.0
        self.required_key: str | None = None
        self.server = ThreadedServer(self._app())

    # -- lifecycle
    def start(self) -> MockLLM:
        self.server.start()
        return self

    def stop(self) -> None:
        self.server.stop()

    @property
    def base_url(self) -> str:
        return f"{self.server.url}/v1"

    # -- helpers
    def replies(self, *replies: MockReply | str) -> None:
        queue = [r if isinstance(r, MockReply) else MockReply(content=r) for r in replies]

        def handler(_: dict[str, Any]) -> MockReply:
            return queue.pop(0) if len(queue) > 1 else queue[0]

        self.handler = handler

    def calls_for(self, model: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("model") == model]

    # -- app
    def _authorized(self, request: Request) -> bool:
        if self.required_key is None:
            return True
        return request.headers.get("authorization") == f"Bearer {self.required_key}"

    def _app(self) -> Starlette:
        async def models(request: Request) -> Response:
            if self.models_delay:
                await asyncio.sleep(self.models_delay)
            if not self._authorized(request):
                return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
            if self.models_status != 200:
                return JSONResponse({"error": {"message": "boom"}}, status_code=self.models_status)
            return JSONResponse({"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})

        async def chat(request: Request) -> Response:
            body = await request.json()
            self.requests.append(body)
            if not self._authorized(request):
                return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
            reply = self.handler(body)
            if reply.delay:
                await asyncio.sleep(reply.delay)
            if reply.status != 200:
                return JSONResponse({"error": {"message": f"scripted {reply.status}"}}, status_code=reply.status,
                                    headers=reply.headers)
            if body.get("stream"):
                return StreamingResponse(self._stream(body, reply), media_type="text/event-stream")
            message: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                message["tool_calls"] = [
                    {"id": tc.get("id", f"call_{i}"), "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}))}}
                    for i, tc in enumerate(reply.tool_calls)]
            return JSONResponse({
                "id": "cmpl-mock", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if reply.tool_calls else "stop"}],
                "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens},
            })

        async def embeddings(request: Request) -> Response:
            body = await request.json()
            inputs = body.get("input") or []
            inputs = [inputs] if isinstance(inputs, str) else inputs
            data = []
            for i, text in enumerate(inputs):
                vec = [0.0] * 16
                for token in str(text).lower().split():
                    vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % 16] += 1.0  # deterministic
                data.append({"index": i, "embedding": vec})
            return JSONResponse({"data": data, "model": body.get("model")})

        return Starlette(routes=[
            Route("/v1/models", models), Route("/v1/chat/completions", chat, methods=["POST"]),
            Route("/v1/embeddings", embeddings, methods=["POST"]),
        ])

    async def _stream(self, body: dict[str, Any], reply: MockReply) -> Any:
        model = body.get("model")
        words = [w + " " for w in reply.content.split(" ")] if reply.content else []
        if words:
            words[-1] = words[-1].rstrip(" ")
        for index, word in enumerate(words):
            if reply.drop_after_chunks is not None and index >= reply.drop_after_chunks:
                raise ConnectionResetError("scripted mid-stream drop")
            chunk = {"model": model, "choices": [{"index": 0, "delta": {"content": word}}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0)
        if reply.tool_calls:
            for i, tc in enumerate(reply.tool_calls):
                chunk = {"model": model, "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": i, "id": tc.get("id", f"call_{i}"), "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}))}}]}}]}
                yield f"data: {json.dumps(chunk)}\n\n"
        final = {"model": model, "choices": [{"index": 0, "delta": {},
                                              "finish_reason": "tool_calls" if reply.tool_calls else "stop"}],
                 "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"
