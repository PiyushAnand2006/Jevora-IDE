from __future__ import annotations

import pytest
import httpx

from backend import router
from backend.schemas import TaskCategory


def test_routes_task_category_to_configured_custom_provider(monkeypatch: pytest.MonkeyPatch):
    custom = {"id": "custom-1", "name": "Local API", "base_url": "http://127.0.0.1:11434/v1", "api_key": "sk-test-value"}
    monkeypatch.setattr(router, "load_providers", lambda: [custom])
    monkeypatch.setattr(router, "load_router_config", lambda: {"tiers": {
        "code_generation": {"provider": "custom-1", "model": "local-coder"},
    }})

    provider, model = router.resolve_model(TaskCategory.code_generation)

    assert provider == custom
    assert provider["base_url"] == "http://127.0.0.1:11434/v1"
    assert provider["api_key"] == "sk-test-value"
    assert model == "local-coder"


def test_unknown_task_category_falls_back_without_exception(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(router, "load_providers", lambda: [])
    monkeypatch.setattr(router, "load_router_config", lambda: {"tiers": {}})

    provider, model = router.resolve_model("not-a-real-task")

    assert provider is None
    assert model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_complete_uses_openai_compatible_custom_base_url(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "scripted reply"}}]}

    class Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(router.httpx, "AsyncClient", Client)
    reply = await router.complete({"base_url": "http://local.test/v1", "api_key": "test-token"}, "test-model", "system", "prompt")

    assert reply == "scripted reply"
    assert captured["url"] == "http://local.test/v1/chat/completions"
    assert captured["json"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_complete_handles_provider_failure_without_network(monkeypatch: pytest.MonkeyPatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("offline")

    monkeypatch.setattr(router.httpx, "AsyncClient", Client)
    assert await router.complete({"base_url": "http://local.test", "api_key": "test-token"}, "model", "system", "prompt") is None
