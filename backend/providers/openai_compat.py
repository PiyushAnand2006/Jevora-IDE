"""OpenAI-compatible adapter (OpenAI, LM Studio, vLLM, Ollama /v1, any custom endpoint)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

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


def _message_to_openai(msg: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role}
    if isinstance(msg.content, str):
        out["content"] = msg.content
    else:
        parts: list[dict[str, Any]] = []
        for part in msg.content:
            if part.type == "text":
                parts.append({"type": "text", "text": part.text or ""})
            elif part.image_b64:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{part.mime or 'image/png'};base64,{part.image_b64}"},
                })
        out["content"] = parts
    if msg.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
            for tc in msg.tool_calls
        ]
        if not out.get("content"):
            out["content"] = None
    if msg.role == "tool":
        out["tool_call_id"] = msg.tool_call_id
        if msg.name:
            out["name"] = msg.name
    return out


class OpenAICompatAdapter(ProviderAdapter):
    kind = "openai"

    @property
    def api_base(self) -> str:
        return self.base_url

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def list_models(self) -> list[str]:
        try:
            response = await self.client.get(
                f"{self.api_base}/models", headers=self._headers(), timeout=self.models_timeout
            )
        except Exception as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider /models returned non-JSON content") from exc
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        models: list[str] = []
        for item in items or []:
            if isinstance(item, str):
                models.append(item)
            elif isinstance(item, dict):
                ident = item.get("id") or item.get("name") or item.get("model")
                if ident:
                    models.append(str(ident))
        return sorted(dict.fromkeys(models))

    def _body(self, request: ChatRequest, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_to_openai(m) for m in request.messages],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": request.json_schema, "strict": False},
            }
        elif request.json_mode:
            body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _responses_input(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                instructions.append(message.text())
                continue
            content: list[dict[str, Any]] = []
            if isinstance(message.content, str):
                content.append({"type": "input_text", "text": message.content})
            else:
                for part in message.content:
                    if part.type == "text" and part.text:
                        content.append({"type": "input_text", "text": part.text})
                    elif part.type == "image" and part.image_b64:
                        content.append({"type": "input_image", "image_url":
                                        f"data:{part.mime or 'image/png'};base64,{part.image_b64}"})
            if content:
                role = "assistant" if message.role == "assistant" else "user"
                items.append({"role": role, "content": content})
        return "\n\n".join(p for p in instructions if p), items

    async def _web_search(self, request: ChatRequest, on_delta: DeltaCallback | None = None) -> ChatResponse:
        if self.kind != "openai":
            raise ProviderError(f"{self.kind} provider does not expose native OpenAI web search")
        started = time.perf_counter()
        instructions, input_items = self._responses_input(request.messages)
        body: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "tools": [{"type": "web_search_preview"}],
        }
        if instructions:
            body["instructions"] = instructions
        try:
            response = await self.client.post(f"{self.api_base}/responses", json=body,
                                              headers=self._headers(), timeout=self.timeout)
        except Exception as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned non-JSON web-search response", retryable=True) from exc
        text_parts: list[str] = []
        sources: list[dict[str, Any]] = []
        for item in data.get("output", []) or []:
            for block in item.get("content", []) or []:
                if block.get("type") in ("output_text", "text"):
                    text_parts.append(str(block.get("text") or ""))
                    for annotation in block.get("annotations", []) or []:
                        if annotation.get("type") != "url_citation":
                            continue
                        url = annotation.get("url") or ""
                        if url:
                            sources.append({"kind": "web", "title": annotation.get("title") or url,
                                            "location": url, "excerpt": ""})
        content = str(data.get("output_text") or "".join(text_parts))
        usage_raw = data.get("usage") or {}
        usage = Usage(prompt_tokens=int(usage_raw.get("input_tokens") or 0),
                      completion_tokens=int(usage_raw.get("output_tokens") or 0))
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        unique_sources = list({source["location"]: source for source in sources}.values())
        if on_delta is not None and content:
            await on_delta(content)
        return ChatResponse(content=content, usage=usage, model=data.get("model") or request.model,
                            finish_reason="stop", latency_ms=(time.perf_counter() - started) * 1000,
                            sources=unique_sources)

    async def chat(self, request: ChatRequest, on_delta: DeltaCallback | None = None) -> ChatResponse:
        if request.web_search:
            return await self._web_search(request, on_delta)
        started = time.perf_counter()
        stream = request.stream and on_delta is not None
        body = self._body(request, stream)
        url = f"{self.api_base}/chat/completions"
        if not stream:
            try:
                response = await self.client.post(url, json=body, headers=self._headers(), timeout=self.timeout)
            except Exception as exc:
                raise wrap_transport_error(exc) from exc
            if response.status_code == 400 and "response_format" in body:
                # Some OpenAI-compatible servers reject json_schema; retry with plain JSON mode.
                body["response_format"] = {"type": "json_object"}
                response = await self.client.post(url, json=body, headers=self._headers(), timeout=self.timeout)
            if response.status_code >= 400:
                raise classify_http_error(response)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("provider returned non-JSON completion", retryable=True) from exc
            return self._parse_completion(request, data, started)
        return await self._stream(request, body, url, on_delta, started)

    def _parse_completion(self, request: ChatRequest, data: dict[str, Any], started: float) -> ChatResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("provider returned no choices", retryable=True)
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        tool_calls = [
            ToolCall(
                id=tc.get("id") or f"call_{idx}",
                name=(tc.get("function") or {}).get("name", ""),
                arguments=parse_arguments((tc.get("function") or {}).get("arguments")),
                raw_arguments=(tc.get("function") or {}).get("arguments")
                if isinstance((tc.get("function") or {}).get("arguments"), str) else None,
            )
            for idx, tc in enumerate(message.get("tool_calls") or [])
        ]
        usage_raw = data.get("usage") or {}
        usage = Usage(prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                      completion_tokens=int(usage_raw.get("completion_tokens") or 0))
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        return ChatResponse(
            content=content, tool_calls=tool_calls, usage=usage, model=data.get("model") or request.model,
            finish_reason=choices[0].get("finish_reason"),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def _stream(self, request: ChatRequest, body: dict[str, Any], url: str,
                      on_delta: DeltaCallback | None, started: float) -> ChatResponse:
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
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
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    model = str(chunk.get("model") or model)
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        usage = Usage(prompt_tokens=int(u.get("prompt_tokens") or 0),
                                      completion_tokens=int(u.get("completion_tokens") or 0))
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            content_parts.append(str(piece))
                            emitted = True
                            if on_delta is not None:
                                await on_delta(str(piece))
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_acc.setdefault(int(tc.get("index", 0)), {"id": None, "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except ProviderError as exc:
            exc.partial = emitted
            raise
        except Exception as exc:
            raise wrap_transport_error(exc, partial=emitted) from exc
        if finish is None and not content_parts and not tool_acc:
            raise ProviderError("stream ended without any completion data", retryable=True, partial=emitted)
        content = "".join(content_parts)
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"],
                     arguments=parse_arguments(slot["args"]), raw_arguments=slot["args"])
            for idx, slot in sorted(tool_acc.items())
        ]
        return ChatResponse(content=content, tool_calls=tool_calls, usage=usage, model=model,
                            finish_reason=finish, latency_ms=(time.perf_counter() - started) * 1000)

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        try:
            response = await self.client.post(
                f"{self.api_base}/embeddings", json={"model": model, "input": texts},
                headers=self._headers(), timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        data = response.json()
        return [list(map(float, item["embedding"])) for item in data.get("data", [])]
