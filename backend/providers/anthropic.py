"""Anthropic Messages API adapter (native shapes, streaming, tools, vision)."""

from __future__ import annotations

import json
import time
from typing import Any

from backend.providers.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    DeltaCallback,
    ProviderAdapter,
    ProviderError,
    ToolCall,
    Usage,
    classify_http_error,
    parse_arguments,
    wrap_transport_error,
)

ANTHROPIC_VERSION = "2023-06-01"
JSON_TOOL = "respond_json"


def _blocks(msg: ChatMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if isinstance(msg.content, str):
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
    else:
        for part in msg.content:
            if part.type == "text" and part.text:
                blocks.append({"type": "text", "text": part.text})
            elif part.type == "image" and part.image_b64:
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": part.mime or "image/png", "data": part.image_b64}})
    return blocks


def to_anthropic_messages(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.text())
            continue
        if msg.role == "tool":
            block = {"type": "tool_result", "tool_use_id": msg.tool_call_id or "", "content": msg.text()}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        blocks = _blocks(msg)
        if msg.role == "assistant":
            for tc in msg.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
        if not blocks:
            blocks = [{"type": "text", "text": "(empty)"}]
        if out and out[-1]["role"] == msg.role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": msg.role, "content": blocks})
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(context follows)"}]})
    return "\n\n".join(p for p in system_parts if p), out


class AnthropicAdapter(ProviderAdapter):
    kind = "anthropic"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def list_models(self) -> list[str]:
        models: list[str] = []
        after: str | None = None
        for _ in range(10):
            params: dict[str, str] = {"limit": "100"}
            if after:
                params["after_id"] = after
            try:
                response = await self.client.get(f"{self.base_url}/models", headers=self._headers(),
                                                 params=params, timeout=self.models_timeout)
            except Exception as exc:
                raise wrap_transport_error(exc) from exc
            if response.status_code >= 400:
                raise classify_http_error(response)
            data = response.json()
            models.extend(str(m["id"]) for m in data.get("data", []) if m.get("id"))
            if not data.get("has_more"):
                break
            after = data.get("last_id")
        return sorted(dict.fromkeys(models))

    def _body(self, request: ChatRequest, stream: bool) -> dict[str, Any]:
        system, messages = to_anthropic_messages(request.messages)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
        }
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                 for t in request.tools]
        if request.json_schema is not None and not request.tools:
            # Anthropic has no response_format: force a single tool whose input is the JSON.
            tools.append({"name": JSON_TOOL, "description": "Return the answer as structured JSON.",
                          "input_schema": request.json_schema})
            body["tool_choice"] = {"type": "tool", "name": JSON_TOOL}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        return body

    async def chat(self, request: ChatRequest, on_delta: DeltaCallback | None = None) -> ChatResponse:
        started = time.perf_counter()
        stream = request.stream and on_delta is not None and request.json_schema is None
        body = self._body(request, stream)
        url = f"{self.base_url}/messages"
        if stream:
            return await self._stream(request, body, url, on_delta, started)
        try:
            response = await self.client.post(url, json=body, headers=self._headers(), timeout=self.timeout)
        except Exception as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        data = response.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                if block.get("name") == JSON_TOOL:
                    text_parts.append(json.dumps(block.get("input") or {}))
                else:
                    calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""),
                                          arguments=block.get("input") or {}))
        usage_raw = data.get("usage") or {}
        content = "".join(text_parts)
        usage = Usage(prompt_tokens=int(usage_raw.get("input_tokens") or 0),
                      completion_tokens=int(usage_raw.get("output_tokens") or 0))
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        return ChatResponse(content=content, tool_calls=calls, usage=usage,
                            model=data.get("model", request.model), finish_reason=data.get("stop_reason"),
                            latency_ms=(time.perf_counter() - started) * 1000)

    async def _stream(self, request: ChatRequest, body: dict[str, Any], url: str,
                      on_delta: DeltaCallback | None, started: float) -> ChatResponse:
        text_parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish: str | None = None
        model = request.model
        emitted = False
        try:
            async with self.client.stream("POST", url, json=body, headers=self._headers(),
                                          timeout=self.timeout) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    raise classify_http_error(response, text)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "message_start":
                        msg = event.get("message") or {}
                        model = msg.get("model", model)
                        usage.prompt_tokens = int((msg.get("usage") or {}).get("input_tokens") or 0)
                    elif etype == "content_block_start":
                        blk = event.get("content_block") or {}
                        blocks[int(event.get("index", 0))] = {
                            "type": blk.get("type"), "id": blk.get("id"), "name": blk.get("name"), "json": ""}
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            text_parts.append(text)
                            emitted = True
                            if on_delta is not None and text:
                                await on_delta(text)
                        elif delta.get("type") == "input_json_delta":
                            blocks.setdefault(int(event.get("index", 0)), {"json": ""})["json"] += \
                                delta.get("partial_json", "")
                    elif etype == "message_delta":
                        finish = (event.get("delta") or {}).get("stop_reason") or finish
                        usage.completion_tokens = int((event.get("usage") or {}).get("output_tokens") or 0)
                    elif etype == "error":
                        err = event.get("error") or {}
                        raise ProviderError(f"stream error: {err.get('message', 'unknown')}",
                                            retryable=err.get("type") in ("overloaded_error", "api_error"),
                                            partial=emitted)
        except ProviderError as exc:
            exc.partial = exc.partial or emitted
            raise
        except Exception as exc:
            raise wrap_transport_error(exc, partial=emitted) from exc
        if finish is None and not text_parts and not blocks:
            raise ProviderError("stream ended without data", retryable=True, partial=emitted)
        calls = [ToolCall(id=b.get("id") or f"call_{i}", name=b.get("name") or "",
                          arguments=parse_arguments(b.get("json")), raw_arguments=b.get("json"))
                 for i, b in sorted(blocks.items()) if b.get("type") == "tool_use"]
        content = "".join(text_parts)
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        return ChatResponse(content=content, tool_calls=calls, usage=usage, model=model,
                            finish_reason=finish, latency_ms=(time.perf_counter() - started) * 1000)
