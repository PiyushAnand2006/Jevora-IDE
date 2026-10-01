"""Provider selection and OpenAI-compatible model calls."""
from __future__ import annotations

import httpx

from .schemas import TaskCategory
from .storage import load_providers, load_router_config


def resolve_model(category: TaskCategory | str) -> tuple[dict | None, str]:
    """Resolve a configured tier and safely fall back for unknown categories."""
    key = category.value if isinstance(category, TaskCategory) else str(category)
    tiers = load_router_config().get("tiers", {})
    tier = tiers.get(key)
    if not isinstance(tier, dict):
        return None, "gpt-4o-mini"
    providers = load_providers()
    provider = next((p for p in providers if p["id"] == tier.get("provider")), None)
    return provider, tier.get("model", "gpt-4o-mini")


async def complete(provider: dict | None, model: str, system: str, prompt: str) -> str | None:
    """Return None when no configured compatible provider is available."""
    if not provider or not provider.get("api_key"):
        return None
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "temperature": 0.2}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError):
        return None


async def list_models(provider: dict | None) -> list[str]:
    """Return model IDs from an OpenAI-compatible provider without exposing its key."""
    if not provider or not provider.get("base_url"):
        return []
    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    url = provider["base_url"].rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json().get("data", [])
            return sorted({item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)})
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return []
