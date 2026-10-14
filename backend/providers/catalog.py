"""Static knowledge about providers and models: kinds, aliases, tiers, prices, capabilities."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

ProviderKind = Literal["openai", "google", "anthropic", "custom", "local"]
PROVIDER_KINDS: tuple[str, ...] = ("openai", "google", "anthropic", "custom", "local")

Category = Literal[
    "heavy_reasoning", "fast_classification", "code_generation", "creative_ui", "research_retrieval",
    "computer_use",
]
ROUTING_CATEGORIES: tuple[str, ...] = (
    "heavy_reasoning", "fast_classification", "code_generation", "creative_ui", "research_retrieval",
)
ALL_CATEGORIES: tuple[str, ...] = (*ROUTING_CATEGORIES, "computer_use")

Tier = Literal["fast", "balanced", "reasoning", "computer_use", "embedding"]

# Which tier each routing category prefers (first = best fit).
CATEGORY_TIER_PREFERENCE: dict[str, list[str]] = {
    "heavy_reasoning": ["reasoning", "balanced", "fast"],
    "fast_classification": ["fast", "balanced", "reasoning"],
    "code_generation": ["reasoning", "balanced", "fast"],
    "creative_ui": ["balanced", "reasoning", "fast"],
    "research_retrieval": ["balanced", "fast", "reasoning"],
    "computer_use": ["computer_use", "reasoning", "balanced"],
}


def detect_kind(base_url: str, declared: str | None = None) -> str:
    """Infer the provider kind from its base URL (the UI always sends ``custom``)."""
    if declared and declared not in ("custom", ""):
        return declared
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host.endswith("api.openai.com"):
        return "openai"
    if host.endswith("generativelanguage.googleapis.com"):
        return "google"
    if host.endswith("api.anthropic.com"):
        return "anthropic"
    if parsed.port in (11434, 1234) or "ollama" in host or host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):  # noqa: S104
        return "local"
    return "custom"


# Model ids the UI's static picker sends → (provider kind, candidate model names in order).
UI_MODEL_ALIASES: dict[str, tuple[str, list[str]]] = {
    "gpt-4o": ("openai", ["gpt-4o", "gpt-4o-2024-11-20"]),
    "claude-3-7-sonnet": ("anthropic", ["claude-3-7-sonnet-latest", "claude-3-7-sonnet-20250219"]),
    "gemini-2-5-pro": ("google", ["gemini-2.5-pro", "gemini-2.5-pro-latest"]),
    "local": ("local", []),
}

_SEG = r"(?:^|[-_.:/ ])"
_FAST_RE = re.compile(_SEG + r"(mini|nano|flash|haiku|small|lite|tiny|instant|turbo)|"
                      r"(?:^|[-_.:/ ])[0-3](\.\d)?b(?![a-z0-9])", re.I)
_LIGHT_RE = re.compile(_SEG + r"(mini|nano|flash|haiku|lite)", re.I)
_REASONING_RE = re.compile(r"(opus|sonnet|\bpro\b|-pro|gpt-4o(?!-mini)|gpt-4\.1(?!-mini|-nano)|gpt-5|\bo1\b|"
                           r"\bo3\b|\bo4\b|o1-|o3-|70b|72b|405b|deepseek-r1|qwq|reason|large)", re.I)
_CU_RE = re.compile(r"(computer-use|computer_use)", re.I)
_EMBED_RE = re.compile(r"(embed|embedding)", re.I)
_VISION_RE = re.compile(r"(gpt-4o|gpt-4\.1|gpt-5|claude-3|claude-(sonnet|opus|haiku)-4|gemini|llava|vision|"
                        r"qwen2\.5-vl|llama3\.2-vision|pixtral|minicpm-v|moondream|bakllava)", re.I)


_NON_CHAT_RE = re.compile(r"(dall-e|whisper|tts|moderation|davinci|babbage|text-embedding|gpt-image|"
                          r"-audio|realtime|transcribe|-search-|imagen|veo|aqa|-tts|lyria)", re.I)

# Preferred models (earlier = better) used to order defaults inside a tier.
MODEL_PREFERENCE: list[str] = [
    "claude-sonnet-4", "claude-3-7-sonnet", "gpt-4.1", "gpt-4o", "gemini-2.5-pro", "o3", "claude-opus-4",
    "claude-3-5-haiku", "claude-haiku-4", "gpt-4.1-mini", "gpt-4o-mini", "gemini-2.5-flash",
    "gemini-2.0-flash", "qwen2.5-coder", "llama3.1", "llama3.2", "qwen2.5", "mistral",
]


def is_chat_model(model: str) -> bool:
    return not _NON_CHAT_RE.search(model) and infer_tier(model) != "embedding"


def preference_rank(model: str) -> tuple[int, int, int]:
    name = model.lower().removeprefix("models/")
    pref = next((i for i, p in enumerate(MODEL_PREFERENCE) if name.startswith(p)), len(MODEL_PREFERENCE))
    noisy = int(bool(re.search(r"(preview|exp|\d{4}-\d{2}-\d{2}|\d{8}|latest-\d)", name)))
    return pref, noisy, len(name)


def infer_tier(model: str) -> str:
    if _EMBED_RE.search(model):
        return "embedding"
    if _CU_RE.search(model):
        return "computer_use"
    if _REASONING_RE.search(model) and not _LIGHT_RE.search(model):
        return "reasoning"
    if _FAST_RE.search(model):
        return "fast"
    return "balanced"


def infer_capabilities(model: str, kind: str) -> dict[str, Any]:
    tier = infer_tier(model)
    context = 128_000
    if kind == "google":
        context = 1_000_000
    elif kind == "anthropic":
        context = 200_000
    elif kind == "local":
        context = 32_000
    return {
        "vision": bool(_VISION_RE.search(model)),
        "tools": tier != "embedding",
        "json": tier != "embedding",
        "context_len": context,
        "computer_use": tier == "computer_use",
    }


# USD per 1M tokens (input, output). Local models are free.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    "computer-use-preview": (3.00, 12.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-haiku-4": (1.00, 5.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-computer-use": (1.25, 10.00),
}


def estimate_cost(model: str, kind: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    if kind == "local":
        return 0.0
    name = model.lower().removeprefix("models/")
    best: tuple[float, float] | None = None
    best_len = 0
    for prefix, price in PRICING.items():
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    if best is None:
        return 0.0
    return round(prompt_tokens / 1e6 * best[0] + completion_tokens / 1e6 * best[1], 6)


def default_model_for(kind: str, tier: str) -> str | None:
    """Sensible defaults when a provider's model list is unavailable."""
    defaults: dict[str, dict[str, str]] = {
        "openai": {"fast": "gpt-4o-mini", "balanced": "gpt-4o", "reasoning": "gpt-4o",
                   "computer_use": "computer-use-preview"},
        "anthropic": {"fast": "claude-3-5-haiku-latest", "balanced": "claude-3-7-sonnet-latest",
                      "reasoning": "claude-3-7-sonnet-latest", "computer_use": "claude-3-7-sonnet-latest"},
        "google": {"fast": "gemini-2.5-flash", "balanced": "gemini-2.5-flash", "reasoning": "gemini-2.5-pro",
                   "computer_use": "gemini-2.5-computer-use-preview-10-2025"},
    }
    return defaults.get(kind, {}).get(tier)
