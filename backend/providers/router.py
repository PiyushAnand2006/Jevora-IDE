"""LiteLLM-style provider router.

Resolves a model spec (``auto``, ``provider_id:model``, a UI alias such as ``gpt-4o`` or
``local``, or a bare model id) into an ordered binding chain, then calls the bindings with
retries, circuit breaking, per-provider concurrency limits, fallbacks, vision normalisation,
JSON repair and token/cost accounting.

The category→model mapping lives in ``RouterConfig`` (per user), never in the decision engine.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.core.errors import AppError, NotFound
from backend.core.logging import get_logger
from backend.core.metrics import Metrics
from backend.db.models import LLMCall, ModelBinding, Provider, RouterConfig
from backend.providers.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ContentPart,
    DeltaCallback,
    NoProviderConfigured,
    ProviderError,
    ProviderUnavailable,
    ToolSpec,
)
from backend.providers.catalog import (
    ALL_CATEGORIES,
    CATEGORY_TIER_PREFERENCE,
    UI_MODEL_ALIASES,
    estimate_cost,
    infer_capabilities,
    infer_tier,
    is_chat_model,
    preference_rank,
)
from backend.providers.jsonrepair import repair_json
from backend.providers.resilience import BreakerRegistry, call_with_retries
from backend.providers.service import ProviderService

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

KIND_ORDER = ["anthropic", "openai", "google", "custom", "local"]


@dataclass(frozen=True)
class Binding:
    provider_id: str
    provider_name: str
    kind: str
    model: str
    tier: str
    capabilities: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    source: str = "default"

    @property
    def ref(self) -> str:
        return f"{self.provider_id}:{self.model}"

    def public(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "provider_name": self.provider_name, "kind": self.kind,
                "model": self.model, "tier": self.tier, "capabilities": self.capabilities,
                "source": self.source, "ref": self.ref}


@dataclass
class CallMeta:
    user_id: str
    run_id: str | None = None
    call_id: str | None = None
    category: str | None = None


UsageCallback = Callable[[ChatResponse, Binding], Awaitable[None]]
ResetCallback = Callable[[], Awaitable[None]]
RetryCallback = Callable[[Binding, int, ProviderError, float], Awaitable[None]]


def _binding(provider: Provider, mb: ModelBinding | None, model: str, source: str) -> Binding:
    tier = mb.tier if mb is not None else infer_tier(model)
    caps = dict(mb.capabilities) if mb is not None else infer_capabilities(model, provider.kind)
    return Binding(provider_id=provider.id, provider_name=provider.name, kind=provider.kind, model=model,
                   tier=tier, capabilities=caps, source=source)


def strip_images(messages: list[ChatMessage]) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for msg in messages:
        if msg.has_images() and isinstance(msg.content, list):
            parts = [p for p in msg.content if p.type == "text"]
            dropped = sum(1 for p in msg.content if p.type == "image")
            parts.append(ContentPart(type="text",
                                     text=f"[{dropped} image attachment(s) omitted: model has no vision support]"))
            out.append(msg.model_copy(update={"content": parts}))
        else:
            out.append(msg)
    return out


class ProviderRouter:
    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession],
                 service: ProviderService, metrics: Metrics | None = None) -> None:
        self.settings = settings
        self.sessions = sessions
        self.service = service
        self.metrics = metrics
        self.breakers = BreakerRegistry(settings.circuit_breaker_threshold, settings.circuit_breaker_reset_s)
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------------ mapping
    async def _candidates(self, user_id: str) -> list[tuple[Provider, ModelBinding]]:
        pairs = await self.service.bindings(user_id)
        if not pairs:
            # Lazily discover models for providers we have never listed.
            for provider in await self.service.list_for(user_id):
                await self.service.fetch_models(user_id, provider.id)
            pairs = await self.service.bindings(user_id)
        return [(p, b) for p, b in pairs if is_chat_model(b.model)]

    async def default_mapping(self, user_id: str) -> dict[str, list[Binding]]:
        pairs = await self._candidates(user_id)
        mapping: dict[str, list[Binding]] = {}
        for category in ALL_CATEGORIES:
            prefs = CATEGORY_TIER_PREFERENCE[category]

            def rank(pair: tuple[Provider, ModelBinding], prefs: list[str] = prefs) -> tuple[int, ...]:
                provider, mb = pair
                tier_rank = prefs.index(mb.tier) if mb.tier in prefs else len(prefs)
                kind_rank = KIND_ORDER.index(provider.kind) if provider.kind in KIND_ORDER else len(KIND_ORDER)
                return (tier_rank, kind_rank, *preference_rank(mb.model))

            ordered = sorted(pairs, key=rank)
            if category == "computer_use":
                ordered = [pb for pb in ordered if pb[1].tier == "computer_use" or pb[1].capabilities.get("vision")]
            mapping[category] = [_binding(p, b, b.model, "default") for p, b in ordered[:4]]
        return mapping

    async def stored_mapping(self, user_id: str) -> dict[str, list[dict[str, str]]]:
        async with self.sessions() as session:
            rc = await session.scalar(select(RouterConfig).where(RouterConfig.user_id == user_id))
            return dict(rc.mapping) if rc is not None and rc.mapping else {}

    async def effective_mapping(self, user_id: str) -> dict[str, list[Binding]]:
        defaults = await self.default_mapping(user_id)
        stored = await self.stored_mapping(user_id)
        providers = {p.id: p for p in await self.service.list_for(user_id)}
        binding_rows = {(p.id, b.model): b for p, b in await self.service.bindings(user_id)}
        effective: dict[str, list[Binding]] = {}
        for category in ALL_CATEGORIES:
            chain: list[Binding] = []
            for entry in stored.get(category, []):
                provider = providers.get(entry.get("provider_id", ""))
                model = entry.get("model")
                if provider is None or not model:
                    continue
                chain.append(_binding(provider, binding_rows.get((provider.id, model)), model, "router_config"))
            for binding in defaults.get(category, []):
                if all(b.ref != binding.ref for b in chain):
                    chain.append(binding)
            effective[category] = chain
        return effective

    async def get_config(self, user_id: str) -> dict[str, Any]:
        effective = await self.effective_mapping(user_id)
        return {
            "categories": list(ALL_CATEGORIES),
            "overrides": await self.stored_mapping(user_id),
            "effective": {cat: [b.public() for b in chain] for cat, chain in effective.items()},
            "tiers": {cat: (chain[0].tier if chain else None) for cat, chain in effective.items()},
        }

    async def set_config(self, user_id: str, mapping: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
        providers = {p.id for p in await self.service.list_for(user_id)}
        clean: dict[str, list[dict[str, str]]] = {}
        for category, entries in mapping.items():
            if category not in ALL_CATEGORIES:
                raise AppError(f"unknown category '{category}'", code="invalid_category", status_code=422)
            clean_entries: list[dict[str, str]] = []
            for entry in entries:
                pid, model = entry.get("provider_id"), entry.get("model")
                if not pid or not model:
                    raise AppError("each binding needs provider_id and model", code="invalid_binding",
                                   status_code=422)
                if pid not in providers:
                    raise NotFound(f"provider '{pid}' not found")
                clean_entries.append({"provider_id": pid, "model": model})
            if clean_entries:
                clean[category] = clean_entries
        async with self.sessions() as session:
            rc = await session.scalar(select(RouterConfig).where(RouterConfig.user_id == user_id))
            if rc is None:
                session.add(RouterConfig(user_id=user_id, mapping=clean))
            else:
                rc.mapping = clean
            await session.commit()
        return await self.get_config(user_id)

    # ------------------------------------------------------------------ resolution
    async def resolve(self, user_id: str, model_spec: str | None, category: str | None = None) -> list[Binding]:
        category = category or "code_generation"
        effective = await self.effective_mapping(user_id)
        auto_chain = effective.get(category, [])
        spec = (model_spec or "auto").strip()
        if spec.lower() == "auto":
            if not auto_chain:
                raise NoProviderConfigured(
                    "No model provider is configured. Add a provider (or start Ollama locally) and try again.")
            return auto_chain

        explicit = await self._resolve_explicit(user_id, spec)
        chain = [explicit, *[b for b in auto_chain if b.ref != explicit.ref]]
        return chain

    async def _resolve_explicit(self, user_id: str, spec: str) -> Binding:
        providers = await self.service.list_for(user_id)
        by_id = {p.id: p for p in providers}
        rows = await self.service.bindings(user_id)
        bindings_by_provider: dict[str, dict[str, ModelBinding]] = {}
        for provider, mb in rows:
            bindings_by_provider.setdefault(provider.id, {})[mb.model] = mb

        if ":" in spec and spec.split(":", 1)[0] in by_id:
            pid, model = spec.split(":", 1)
            provider = by_id[pid]
            return _binding(provider, bindings_by_provider.get(pid, {}).get(model), model, "explicit")

        alias = UI_MODEL_ALIASES.get(spec.lower())
        if alias is not None:
            kind, candidates = alias
            for provider in sorted(providers, key=lambda p: p.created_at):
                if provider.kind != kind:
                    continue
                known = bindings_by_provider.get(provider.id) or {}
                if not known:
                    await self.service.fetch_models(user_id, provider.id)
                    known = {mb.model: mb for p, mb in await self.service.bindings(user_id) if p.id == provider.id}
                if kind == "local":
                    chat_models = sorted((m for m in known if is_chat_model(m)), key=preference_rank)
                    if chat_models:
                        return _binding(provider, known[chat_models[0]], chat_models[0], "explicit")
                    continue
                for candidate in candidates:
                    if candidate in known:
                        return _binding(provider, known[candidate], candidate, "explicit")
                return _binding(provider, None, candidates[0], "explicit")
            raise NotFound(f"model '{spec}' needs a saved {kind} provider; add one or choose Auto",
                           code="model_unavailable")

        for provider in providers:
            found = bindings_by_provider.get(provider.id, {}).get(spec)
            if found is not None:
                return _binding(provider, found, spec, "explicit")
        raise NotFound(f"model '{spec}' was not found in any saved provider", code="model_unavailable")

    # ------------------------------------------------------------------ calls
    def _semaphore(self, provider_id: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(provider_id)
        if sem is None:
            sem = asyncio.Semaphore(self.settings.provider_max_concurrency)
            self._semaphores[provider_id] = sem
        return sem

    async def _record(self, meta: CallMeta, binding: Binding, response: ChatResponse | None,
                      error: str | None, latency_ms: float) -> None:
        usage = response.usage if response is not None else None
        cost = response.cost_usd if response is not None else 0.0
        status = "ok" if error is None else "error"
        try:
            async with self.sessions() as session:
                session.add(LLMCall(
                    user_id=meta.user_id, run_id=meta.run_id, call_id=meta.call_id,
                    provider_id=binding.provider_id, provider_kind=binding.kind, model=binding.model,
                    category=meta.category, prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0, cost_usd=cost,
                    latency_ms=latency_ms, status=status, error=error[:2000] if error else None))
                await session.commit()
        except Exception:  # accounting must never break a run
            log.exception("failed to record LLM usage")
        if self.metrics is not None:
            self.metrics.llm_calls.labels(binding.kind, binding.model, status).inc()
            self.metrics.llm_latency.labels(binding.kind).observe(latency_ms / 1000)
            if usage is not None:
                self.metrics.llm_tokens.labels(binding.kind, binding.model, "prompt").inc(usage.prompt_tokens)
                self.metrics.llm_tokens.labels(binding.kind, binding.model, "completion").inc(
                    usage.completion_tokens)
                self.metrics.llm_cost.labels(binding.kind, binding.model).inc(cost)

    async def chat(
        self,
        meta: CallMeta,
        messages: list[ChatMessage],
        *,
        model: str | None = "auto",
        category: str | None = None,
        bindings: list[Binding] | None = None,
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        json_mode: bool = False,
        stream: bool = False,
        on_delta: DeltaCallback | None = None,
        on_reset: ResetCallback | None = None,
        on_usage: UsageCallback | None = None,
        on_retry: RetryCallback | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        if category:
            meta.category = category
        chain = bindings if bindings is not None else await self.resolve(meta.user_id, model, category)
        if not chain:
            raise NoProviderConfigured("No model provider is configured.")
        wants_vision = any(m.has_images() for m in messages)
        if wants_vision:
            chain = sorted(chain, key=lambda b: 0 if b.capabilities.get("vision") else 1)

        providers = {p.id: p for p in await self.service.list_for(meta.user_id)}
        last_error: ProviderError | None = None
        for index, binding in enumerate(chain):
            provider = providers.get(binding.provider_id)
            if provider is None:
                continue
            adapter = self.service.adapter_for(provider)
            payload = messages if (not wants_vision or binding.capabilities.get("vision")) else strip_images(messages)
            request = ChatRequest(model=binding.model, messages=payload, tools=tools or [],
                                  json_schema=json_schema, json_mode=json_mode, stream=stream,
                                  temperature=temperature, max_tokens=max_tokens)
            breaker = self.breakers.get(binding.provider_id)
            started = time.perf_counter()

            async def attempt(adapter: Any = adapter, request: ChatRequest = request,
                              provider_id: str = binding.provider_id) -> ChatResponse:
                async with self._semaphore(provider_id):
                    result: ChatResponse = await adapter.chat(request, on_delta if stream else None)
                    return result

            async def retry_hook(n: int, exc: ProviderError, delay: float, binding: Binding = binding) -> None:
                log.warning("retrying %s (attempt %d) after %.2fs: %s", binding.ref, n, delay, exc)
                if on_retry is not None:
                    await on_retry(binding, n, exc, delay)

            try:
                response, attempts = await call_with_retries(
                    attempt, breaker=breaker, max_retries=self.settings.provider_max_retries,
                    base=self.settings.provider_backoff_base_s, cap=self.settings.provider_backoff_cap_s,
                    on_retry=retry_hook)
            except ProviderError as exc:
                latency = (time.perf_counter() - started) * 1000
                await self._record(meta, binding, None, str(exc), latency)
                last_error = exc
                log.warning("binding %s failed (%s); trying next fallback", binding.ref, exc)
                if exc.partial and on_reset is not None:
                    await on_reset()
                continue
            response.provider_id = binding.provider_id
            response.provider_kind = binding.kind
            response.model = binding.model
            response.attempts = attempts
            response.fallback_used = index > 0
            response.cost_usd = estimate_cost(binding.model, binding.kind, response.usage.prompt_tokens,
                                              response.usage.completion_tokens)
            await self._record(meta, binding, response, None, (time.perf_counter() - started) * 1000)
            if on_usage is not None:
                await on_usage(response, binding)
            return response
        if last_error is None:
            raise ProviderUnavailable("no usable provider binding for this request")
        raise last_error

    async def chat_json(self, meta: CallMeta, messages: list[ChatMessage], schema: type[T],
                        **kwargs: Any) -> tuple[T, ChatResponse]:
        """Ask for JSON matching ``schema``; repair malformed output and re-prompt on validation errors."""
        json_schema = schema.model_json_schema()
        convo = list(messages)
        attempts = max(1, self.settings.json_repair_attempts + 1)
        last_error = "no response"
        response: ChatResponse | None = None
        for _ in range(attempts):
            response = await self.chat(meta, convo, json_schema=json_schema, **kwargs)
            try:
                data = repair_json(response.content)
                return schema.model_validate(data), response
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)[:800]
                convo = [*convo, ChatMessage(role="assistant", content=response.content[:4000]),
                         ChatMessage(role="user", content=(
                             "Your previous reply was not valid JSON for the required schema. "
                             f"Error: {last_error}\nReply with ONLY a JSON object matching this schema:\n"
                             f"{json.dumps(json_schema)[:4000]}"))]
        raise ProviderError(f"model did not return valid JSON after {attempts} attempts: {last_error}")

    async def embed(self, user_id: str, texts: list[str]) -> list[list[float]] | None:
        """Embed with the first embedding-capable binding; ``None`` means use lexical fallback."""
        rows = await self.service.bindings(user_id)
        candidates = [(p, b) for p, b in rows if b.tier == "embedding"]
        for provider, mb in candidates:
            try:
                return await self.service.adapter_for(provider).embed(texts, mb.model)
            except ProviderError as exc:
                log.info("embedding via %s failed: %s", mb.model, exc)
        return None
