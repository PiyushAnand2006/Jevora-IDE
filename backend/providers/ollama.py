"""Ollama adapter: chat via its OpenAI-compatible /v1 API, models via native /api/tags."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from backend.providers.base import ProviderError, classify_http_error, wrap_transport_error
from backend.providers.openai_compat import OpenAICompatAdapter


def ollama_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    for suffix in ("/v1", "/api"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    return root


def looks_like_ollama(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.port == 11434 or "ollama" in (parsed.hostname or "")


class OllamaAdapter(OpenAICompatAdapter):
    kind = "local"

    @property
    def root(self) -> str:
        return ollama_root(self.base_url)

    @property
    def api_base(self) -> str:
        return f"{self.root}/v1"

    async def list_models(self) -> list[str]:
        try:
            response = await self.client.get(f"{self.root}/api/tags", timeout=self.models_timeout)
        except Exception as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code == 404:
            return await super().list_models()
        if response.status_code >= 400:
            raise classify_http_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("Ollama /api/tags returned non-JSON content") from exc
        names = {str(m.get("name") or m.get("model") or "") for m in data.get("models", []) if isinstance(m, dict)}
        return sorted(n for n in names if n)

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        try:
            response = await self.client.post(
                f"{self.root}/api/embed", json={"model": model, "input": texts}, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc) from exc
        if response.status_code >= 400:
            raise classify_http_error(response)
        return [list(map(float, v)) for v in response.json().get("embeddings", [])]
