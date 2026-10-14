"""Google Gemini (Generative Language API v1beta) native adapter."""

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
    wrap_transport_error,
)

# Gemini accepts an OpenAPI subset for function parameters.
_ALLOWED_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "properties", "required", "items",
    "minItems", "maxItems", "minimum", "maximum", "anyOf", "propertyOrdering",
}


def sanitize_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [sanitize_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: sanitize_schema(v) for k, v in value.items()}
        elif key in ("items", "anyOf"):
            out[key] = sanitize_schema(value)
        elif key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            out[key] = (non_null[0] if non_null else "string").upper()
            if "null" in value:
                out["nullable"] = True
        elif key == "type" and isinstance(value, str):
            out[key] = value.upper()
        else:
            out[key] = value
    if out.get("type") == "OBJECT" and not out.get("properties"):
        out.pop("properties", None)
    return out


def _model_path(model: str) -> str:
    return model if model.startswith("models/") else f"models/{model}"


def to_gemini_contents(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    contents: list[dict[str, Any]] = []
    names_by_id: dict[str, str] = {}
    for msg in messages:
        if msg.role == "system":
            system.append(msg.text())
            continue
        parts: list[dict[str, Any]] = []
        if msg.role == "tool":
            name = msg.name or names_by_id.get(msg.tool_call_id or "", "tool")
            parts.append({"functionResponse": {"name": name, "response": {"content": msg.text()}}})
            role = "user"
        else:
            role = "model" if msg.role == "assistant" else "user"
            if isinstance(msg.content, str):
                if msg.content:
                    parts.append({"text": msg.content})
            else:
                for part in msg.content:
                    if part.type == "text" and part.text:
                        parts.append({"text": part.text})
                    elif part.type == "image" and part.image_b64:
                        parts.append({"inlineData": {"mimeType": part.mime or "image/png",
                                                     "data": part.image_b64}})
            for tc in msg.tool_calls:
                names_by_id[tc.id] = tc.name
                parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
        if not parts:
            parts = [{"text": "(empty)"}]
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    return "\n\n".join(s for s in system if s), contents


class GeminiAdapter(ProviderAdapter):
    kind = "google"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    async def list_models(self) -> list[str]:
        models: list[str] = []
        token: str | None = None
        for _ in range(20):
            params: dict[str, str] = {"pageSize": "1000"}
            if token:
                params["pageToken"] = token
            try:
                response = await self.client.get(f"{self.base_url}/models", headers=self._headers(),
                                                 params=params, timeout=self.models_timeout)
            except Exception as exc:
                raise wrap_transport_error(exc) from exc
            if response.status_code >= 400:
                raise classify_http_error(response)
            data = response.json()
            for item in data.get("models", []):
                methods = item.get("supportedGenerationMethods") or []
                if methods and "generateContent" not in methods and "embedContent" not in methods:
                    continue
                name = str(item.get("name", ""))
                models.append(name.removeprefix("models/"))
            token = data.get("nextPageToken")
            if not token:
                break
        return sorted(dict.fromkeys(m for m in models if m))

    def _body(self, request: ChatRequest) -> dict[str, Any]:
        system, contents = to_gemini_contents(request.messages)
        body: dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        generation: dict[str, Any] = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation["maxOutputTokens"] = request.max_tokens
        if request.json_schema is not None:
            generation["responseMimeType"] = "application/json"
            generation["responseSchema"] = sanitize_schema(request.json_schema)
        elif request.json_mode:
            generation["responseMimeType"] = "application/json"
        if generation:
            body["generationConfig"] = generation
        if request.tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": sanitize_schema(t.parameters)}
                for t in request.tools]}]
        return body

    def _parse_candidate(self, data: dict[str, Any]) -> tuple[str, list[ToolCall], str | None]:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            if feedback.get("blockReason"):
                raise ProviderError(f"Gemini blocked the prompt: {feedback['blockReason']}")
            return "", [], None
        cand = candidates[0]
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for idx, part in enumerate((cand.get("content") or {}).get("parts") or []):
            if "text" in part and not part.get("thought"):
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                calls.append(ToolCall(id=fc.get("id") or f"call_{fc.get('name', 'fn')}_{idx}",
                                      name=fc.get("name", ""), arguments=fc.get("args") or {}))
        return "".join(text_parts), calls, cand.get("finishReason")

    @staticmethod
    def _usage(data: dict[str, Any]) -> Usage:
        meta = data.get("usageMetadata") or {}
        return Usage(prompt_tokens=int(meta.get("promptTokenCount") or 0),
                     completion_tokens=int(meta.get("candidatesTokenCount") or 0))

    async def chat(self, request: ChatRequest, on_delta: DeltaCallback | None = None) -> ChatResponse:
        started = time.perf_counter()
        body = self._body(request)
        model_path = _model_path(request.model)
        if request.stream and on_delta is not None:
            return await self._stream(request, body, model_path, on_delta, started)
        try:
            response = await self.client.post(f"{self.base_url}/{model_path}:generateContent", json=body,
                                              headers=self._headers(), timeout=self.timeout)
        except Exception as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        data = response.json()
        content, calls, finish = self._parse_candidate(data)
        usage = self._usage(data)
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        return ChatResponse(content=content, tool_calls=calls, usage=usage, model=request.model,
                            finish_reason=finish, latency_ms=(time.perf_counter() - started) * 1000)

    async def _stream(self, request: ChatRequest, body: dict[str, Any], model_path: str,
                      on_delta: DeltaCallback, started: float) -> ChatResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage = Usage()
        finish: str | None = None
        emitted = False
        url = f"{self.base_url}/{model_path}:streamGenerateContent"
        try:
            async with self.client.stream("POST", url, params={"alt": "sse"}, json=body,
                                          headers=self._headers(), timeout=self.timeout) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    raise classify_http_error(response, text)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    text, chunk_calls, chunk_finish = self._parse_candidate(data)
                    if text:
                        text_parts.append(text)
                        emitted = True
                        await on_delta(text)
                    calls.extend(chunk_calls)
                    finish = chunk_finish or finish
                    if data.get("usageMetadata"):
                        usage = self._usage(data)
        except ProviderError as exc:
            exc.partial = exc.partial or emitted
            raise
        except Exception as exc:
            raise wrap_transport_error(exc, partial=emitted) from exc
        if finish is None and not text_parts and not calls:
            raise ProviderError("stream ended without data", retryable=True, partial=emitted)
        content = "".join(text_parts)
        if usage.total == 0:
            usage = self._estimate_usage(request, content)
        return ChatResponse(content=content, tool_calls=calls, usage=usage, model=request.model,
                            finish_reason=finish, latency_ms=(time.perf_counter() - started) * 1000)

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        model_path = _model_path(model)
        body = {"requests": [{"model": model_path, "content": {"parts": [{"text": t}]}} for t in texts]}
        try:
            response = await self.client.post(f"{self.base_url}/{model_path}:batchEmbedContents",
                                              json=body, headers=self._headers(), timeout=self.timeout)
        except Exception as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        return [list(map(float, e.get("values", []))) for e in response.json().get("embeddings", [])]
