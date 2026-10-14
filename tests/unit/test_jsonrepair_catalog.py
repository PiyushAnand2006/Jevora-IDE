from __future__ import annotations

import pytest

from backend.providers.catalog import detect_kind, estimate_cost, infer_tier, is_chat_model
from backend.providers.jsonrepair import repair_json


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1,}\n```', {"a": 1}),
    ('Sure! Here it is: {"choice": "plan", "ok": True} hope that helps', {"choice": "plan", "ok": True}),
    ("{'a': 'b'}", {"a": "b"}),
    ('{"text": "line one\nline two"}', {"text": "line one\nline two"}),
    ('{"items": [1, 2, 3', {"items": [1, 2, 3]}),
    ("[1, 2, 3,]", [1, 2, 3]),
])
def test_repair_json(raw: str, expected: object) -> None:
    assert repair_json(raw) == expected


def test_repair_json_gives_up() -> None:
    with pytest.raises(ValueError):
        repair_json("no json here at all")


@pytest.mark.parametrize("url,kind", [
    ("https://api.openai.com/v1", "openai"),
    ("https://generativelanguage.googleapis.com/v1beta", "google"),
    ("https://api.anthropic.com/v1", "anthropic"),
    ("http://localhost:11434", "local"),
    ("http://127.0.0.1:1234/v1", "local"),
    ("https://my-vllm.example.com/v1", "custom"),
])
def test_detect_kind(url: str, kind: str) -> None:
    assert detect_kind(url, "custom") == kind


def test_explicit_kind_is_kept() -> None:
    assert detect_kind("https://proxy.example.com/v1", "anthropic") == "anthropic"


@pytest.mark.parametrize("model,tier", [
    ("gpt-4o-mini", "fast"), ("gpt-4o", "reasoning"), ("claude-3-5-haiku-latest", "fast"),
    ("claude-3-7-sonnet-latest", "reasoning"), ("gemini-2.5-flash", "fast"), ("gemini-2.5-pro", "reasoning"),
    ("llama3.2:3b", "fast"), ("nomic-embed-text:latest", "embedding"), ("computer-use-preview", "computer_use"),
    ("mock-mini", "fast"), ("mock-pro", "reasoning"),
])
def test_infer_tier(model: str, tier: str) -> None:
    assert infer_tier(model) == tier


def test_non_chat_models_filtered() -> None:
    assert not is_chat_model("whisper-1")
    assert not is_chat_model("text-embedding-3-small")
    assert is_chat_model("gpt-4o")


def test_cost_estimation() -> None:
    assert estimate_cost("gpt-4o-mini", "openai", 1_000_000, 0) == pytest.approx(0.15)
    assert estimate_cost("gpt-4o-mini-2024-07-18", "openai", 0, 1_000_000) == pytest.approx(0.60)
    assert estimate_cost("llama3.2", "local", 10_000, 10_000) == 0.0
    assert estimate_cost("unknown-model", "custom", 10, 10) == 0.0
