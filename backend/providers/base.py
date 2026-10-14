"""Vendor-neutral chat types and the adapter contract every provider implements."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
DeltaCallback = Callable[[str], Awaitable[None]]


class ContentPart(BaseModel):
    type: Literal["text", "image"]
    text: str | None = None
    image_b64: str | None = None
    mime: str | None = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None


class ChatMessage(BaseModel):
    role: Role
    content: str | list[ContentPart] = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(p.text or "" for p in self.content if p.type == "text")

    def has_images(self) -> bool:
        return isinstance(self.content, list) and any(p.type == "image" for p in self.content)


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    json_mode: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class ChatResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    provider_id: str | None = None
    provider_kind: str | None = None
    finish_reason: str | None = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    attempts: int = 1
    fallback_used: bool = False


# --------------------------------------------------------------------------- errors
class ProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 retry_after: float | None = None, partial: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.partial = partial  # failure happened after some streamed output


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    """Circuit open, or no usable provider for the request."""


class NoProviderConfigured(ProviderUnavailable):
    pass


def classify_http_error(response: httpx.Response, body_text: str | None = None) -> ProviderError:
    status = response.status_code
    text = body_text if body_text is not None else _safe_text(response)
    from backend.core.logging import redact

    message = redact(f"provider returned HTTP {status}: {text[:300]}")
    retry_after = _retry_after(response)
    if status in (401, 403):
        return ProviderAuthError(message, status=status)
    if status == 429:
        return ProviderRateLimited(message, status=status, retryable=True, retry_after=retry_after)
    if status in (408, 409, 500, 502, 503, 504, 529):
        return ProviderError(message, status=status, retryable=True, retry_after=retry_after)
    return ProviderError(message, status=status)


def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:
        return ""


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def wrap_transport_error(exc: Exception, partial: bool = False) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(f"provider timed out ({type(exc).__name__})", retryable=True, partial=partial)
    if isinstance(exc, httpx.TransportError):
        return ProviderError(f"provider connection failed ({type(exc).__name__}: {exc})",
                             retryable=True, partial=partial)
    return ProviderError(f"provider call failed: {exc}", partial=partial)


def parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        from backend.providers.jsonrepair import repair_json

        try:
            value = repair_json(raw)
        except ValueError:
            return {"__unparsed__": raw}
    return value if isinstance(value, dict) else {"value": value}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


# --------------------------------------------------------------------------- adapter contract
class ProviderAdapter(ABC):
    kind: str = "custom"

    def __init__(self, base_url: str, api_key: str | None, client: httpx.AsyncClient,
                 timeout: float = 120.0, models_timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.client = client
        self.timeout = timeout
        self.models_timeout = models_timeout

    @abstractmethod
    async def list_models(self) -> list[str]: ...

    @abstractmethod
    async def chat(self, request: ChatRequest, on_delta: DeltaCallback | None = None) -> ChatResponse: ...

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        raise ProviderError(f"{self.kind} adapter does not support embeddings")

    @staticmethod
    def _estimate_usage(request: ChatRequest, content: str) -> Usage:
        prompt = sum(estimate_tokens(m.text()) for m in request.messages)
        return Usage(prompt_tokens=prompt, completion_tokens=estimate_tokens(content), estimated=True)
