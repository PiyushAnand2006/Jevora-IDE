"""Provider persistence: CRUD with encrypted keys, model discovery, Ollama autodiscovery."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.core.crypto import DecryptionError, SecretBox
from backend.core.errors import AppError, Conflict, NotFound
from backend.core.ids import iso, utcnow
from backend.core.logging import get_logger
from backend.db.models import Agent, ModelBinding, Provider, RouterConfig, SettingKV
from backend.providers.anthropic import AnthropicAdapter
from backend.providers.base import ProviderAdapter, ProviderError
from backend.providers.catalog import PROVIDER_KINDS, detect_kind, infer_capabilities, infer_tier
from backend.providers.gemini import GeminiAdapter
from backend.providers.ollama import OllamaAdapter, looks_like_ollama
from backend.providers.openai_compat import OpenAICompatAdapter

log = get_logger(__name__)


def validate_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AppError("base_url must be an absolute http(s) URL", code="invalid_base_url", status_code=422)
    return url


def provider_public(provider: Provider) -> dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "kind": provider.kind,
        "has_key": bool(provider.encrypted_key),
        "auto_discovered": provider.auto_discovered,
        "created_at": iso(provider.created_at),
        "updated_at": iso(provider.updated_at),
    }


class ProviderService:
    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession], secrets: SecretBox,
                 http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.sessions = sessions
        self.secrets = secrets
        self.http = http
        self._ollama_probe: tuple[float, bool] = (0.0, False)
        self._ollama_lock = asyncio.Lock()

    # ------------------------------------------------------------------ adapters
    def adapter_for(self, provider: Provider) -> ProviderAdapter:
        api_key: str | None = None
        if provider.encrypted_key:
            try:
                api_key = self.secrets.decrypt(provider.encrypted_key)
            except DecryptionError:
                log.warning("stored key for provider %s cannot be decrypted", provider.id)
                api_key = None
        kwargs = {"client": self.http, "timeout": self.settings.provider_timeout_s,
                  "models_timeout": self.settings.provider_models_timeout_s}
        if provider.kind == "anthropic":
            return AnthropicAdapter(provider.base_url, api_key, **kwargs)  # type: ignore[arg-type]
        if provider.kind == "google":
            return GeminiAdapter(provider.base_url, api_key, **kwargs)  # type: ignore[arg-type]
        if provider.kind == "local" and looks_like_ollama(provider.base_url):
            return OllamaAdapter(provider.base_url, api_key, **kwargs)  # type: ignore[arg-type]
        adapter = OpenAICompatAdapter(provider.base_url, api_key, **kwargs)  # type: ignore[arg-type]
        adapter.kind = provider.kind
        return adapter

    # ------------------------------------------------------------------ CRUD
    async def list_for(self, user_id: str) -> list[Provider]:
        await self.ensure_ollama(user_id)
        async with self.sessions() as session:
            rows = await session.scalars(
                select(Provider).where(Provider.user_id == user_id).order_by(Provider.created_at)
            )
            return list(rows)

    async def get(self, user_id: str, provider_id: str) -> Provider:
        async with self.sessions() as session:
            provider = await session.get(Provider, provider_id)
            if provider is None or provider.user_id != user_id:
                raise NotFound("provider not found")
            return provider

    async def create(self, user_id: str, *, name: str, base_url: str, api_key: str | None,
                     kind: str | None) -> Provider:
        name = (name or "").strip()
        if not name or len(name) > 120:
            raise AppError("provider name must be 1-120 characters", code="invalid_name", status_code=422)
        base_url = validate_base_url(base_url)
        resolved_kind = detect_kind(base_url, kind)
        if resolved_kind not in PROVIDER_KINDS:
            raise AppError(f"kind must be one of {', '.join(PROVIDER_KINDS)}", code="invalid_kind",
                           status_code=422)
        async with self.sessions() as session:
            existing = await session.scalar(
                select(Provider).where(Provider.user_id == user_id, Provider.name == name)
            )
            if existing is not None:
                raise Conflict(f"a provider named '{name}' already exists", code="duplicate_provider",
                               existing_id=existing.id)
            provider = Provider(user_id=user_id, name=name, base_url=base_url, kind=resolved_kind,
                                encrypted_key=self.secrets.encrypt(api_key) if api_key else None)
            session.add(provider)
            await session.commit()
            await session.refresh(provider)
            return provider

    async def update(self, user_id: str, provider_id: str, *, name: str | None = None,
                     base_url: str | None = None, api_key: str | None = None,
                     kind: str | None = None) -> Provider:
        async with self.sessions() as session:
            provider = await session.get(Provider, provider_id)
            if provider is None or provider.user_id != user_id:
                raise NotFound("provider not found")
            if name is not None and name.strip() and name.strip() != provider.name:
                clash = await session.scalar(select(Provider).where(
                    Provider.user_id == user_id, Provider.name == name.strip(), Provider.id != provider_id))
                if clash is not None:
                    raise Conflict(f"a provider named '{name.strip()}' already exists",
                                   code="duplicate_provider", existing_id=clash.id)
                provider.name = name.strip()
            url_changed = False
            if base_url is not None and base_url.strip():
                new_url = validate_base_url(base_url)
                url_changed = new_url != provider.base_url
                provider.base_url = new_url
            if kind is not None or url_changed:
                provider.kind = detect_kind(provider.base_url, kind if kind is not None else "custom")
            if api_key:  # empty/None keeps the existing key
                provider.encrypted_key = self.secrets.encrypt(api_key)
            provider.updated_at = utcnow()
            if url_changed:
                await session.execute(delete(ModelBinding).where(ModelBinding.provider_id == provider_id))
            await session.commit()
            await session.refresh(provider)
            return provider

    async def delete(self, user_id: str, provider_id: str) -> None:
        async with self.sessions() as session:
            provider = await session.get(Provider, provider_id)
            if provider is None or provider.user_id != user_id:
                raise NotFound("provider not found")
            if provider.auto_discovered:
                session.add(SettingKV(key=f"ollama_dismissed:{user_id}", value=True))
            # Unbind agents (they fall back to Auto) and drop router config entries.
            agents = await session.scalars(select(Agent).where(Agent.user_id == user_id))
            for agent in agents:
                binding = dict((agent.config or {}).get("model_binding") or {})
                if binding.get("provider_id") == provider_id:
                    config = dict(agent.config)
                    config["model_binding"] = {"mode": "auto", "category": binding.get("category")}
                    agent.config = config
            rc = await session.scalar(select(RouterConfig).where(RouterConfig.user_id == user_id))
            if rc is not None:
                mapping = {cat: [b for b in entries if b.get("provider_id") != provider_id]
                           for cat, entries in (rc.mapping or {}).items()}
                rc.mapping = {cat: entries for cat, entries in mapping.items() if entries}
            await session.delete(provider)
            await session.commit()

    # ------------------------------------------------------------------ models
    async def fetch_models(self, user_id: str, provider_id: str) -> tuple[list[str], str | None]:
        provider = await self.get(user_id, provider_id)
        adapter = self.adapter_for(provider)
        try:
            models = await adapter.list_models()
        except ProviderError as exc:
            return [], str(exc)
        except Exception as exc:  # defensive: never 500 the models endpoint
            log.exception("model listing failed")
            return [], f"unexpected error: {type(exc).__name__}"
        await self.upsert_bindings(provider, models)
        return models, None

    async def upsert_bindings(self, provider: Provider, models: list[str]) -> None:
        async with self.sessions() as session:
            existing = {b.model: b for b in await session.scalars(
                select(ModelBinding).where(ModelBinding.provider_id == provider.id))}
            for model in models:
                if model in existing:
                    continue
                session.add(ModelBinding(provider_id=provider.id, model=model,
                                         capabilities=infer_capabilities(model, provider.kind),
                                         tier=infer_tier(model)))
            await session.commit()

    async def bindings(self, user_id: str) -> list[tuple[Provider, ModelBinding]]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(Provider, ModelBinding).join(ModelBinding, ModelBinding.provider_id == Provider.id)
                .where(Provider.user_id == user_id).order_by(Provider.created_at, ModelBinding.model)
            )
            return [(p, b) for p, b in rows.all()]

    async def refresh_all(self, user_id: str) -> dict[str, str | None]:
        providers = await self.list_for(user_id)
        results = await asyncio.gather(*(self.fetch_models(user_id, p.id) for p in providers))
        return {p.id: err for p, (_, err) in zip(providers, results, strict=True)}

    # ------------------------------------------------------------------ ollama autodiscovery
    async def ollama_reachable(self) -> bool:
        stamp, ok = self._ollama_probe
        if time.monotonic() - stamp < 60:
            return ok
        try:
            response = await self.http.get(f"{self.settings.ollama_base_url.rstrip('/')}/api/tags", timeout=1.5)
            ok = response.status_code == 200
        except httpx.HTTPError:
            ok = False
        self._ollama_probe = (time.monotonic(), ok)
        return ok

    async def ensure_ollama(self, user_id: str) -> None:
        if not self.settings.ollama_autodiscover:
            return
        async with self._ollama_lock:
            async with self.sessions() as session:
                dismissed = await session.get(SettingKV, f"ollama_dismissed:{user_id}")
                if dismissed is not None:
                    return
                existing = await session.scalar(select(Provider).where(
                    Provider.user_id == user_id, Provider.kind == "local"))
                if existing is not None:
                    return
            if not await self.ollama_reachable():
                return
            name = "Ollama (local)"
            async with self.sessions() as session:
                clash = await session.scalar(select(Provider).where(
                    Provider.user_id == user_id, Provider.name == name))
                if clash is not None:
                    return
                provider = Provider(user_id=user_id, name=name, base_url=self.settings.ollama_base_url.rstrip("/"),
                                    kind="local", auto_discovered=True)
                session.add(provider)
                await session.commit()
                await session.refresh(provider)
            log.info("auto-discovered local Ollama provider for user %s", user_id)
            try:
                models = await self.adapter_for(provider).list_models()
                await self.upsert_bindings(provider, models)
            except ProviderError:
                pass
