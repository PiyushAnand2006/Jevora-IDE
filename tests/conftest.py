"""Shared fixtures: isolated settings per test, in-process app client, mock LLM server."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Never touch the developer's real keyring/Ollama/data dir from tests.
os.environ.setdefault("USE_KEYRING", "false")
os.environ.setdefault("OLLAMA_AUTODISCOVER", "false")

from backend.config import Settings  # noqa: E402
from backend.main import create_app  # noqa: E402
from tests.support.servers import MockLLM  # noqa: E402


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "data_dir": tmp_path / "data",
        "workspace_root": tmp_path / "workspace",
        "vault_path": tmp_path / "vault",
        "use_keyring": False,
        "ollama_autodiscover": False,
        "log_json": False,
        "log_level": "WARNING",
        "provider_timeout_s": 5.0,
        "provider_models_timeout_s": 2.0,
        "provider_backoff_base_s": 0.01,
        "provider_backoff_cap_s": 0.05,
        "laya_enabled": False,
        "sandbox_mode": "local",
        "local_unsafe_exec": True,
        "approval_policy": "auto",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


class AppHarness:
    def __init__(self, app: Any, client: httpx.AsyncClient) -> None:
        self.app = app
        self.client = client

    @property
    def ctx(self) -> Any:
        return self.app.state.ctx


async def start_app(settings: Settings) -> tuple[Any, Any]:
    app = create_app(settings)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    return app, lifespan


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[AppHarness]:
    app, lifespan = await start_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield AppHarness(app, client)
    await lifespan.__aexit__(None, None, None)


@pytest.fixture
async def client(harness: AppHarness) -> httpx.AsyncClient:
    return harness.client


@pytest.fixture(scope="session")
def mock_llm_session() -> Iterator[MockLLM]:
    server = MockLLM().start()
    yield server
    server.stop()


@pytest.fixture
def mock_llm(mock_llm_session: MockLLM) -> Iterator[MockLLM]:
    """Session-wide mock LLM server, reset for every test."""
    mock_llm_session.models = ["mock-mini", "mock-pro", "mock-embed"]
    mock_llm_session.requests.clear()
    mock_llm_session.models_status = 200
    mock_llm_session.models_delay = 0.0
    mock_llm_session.required_key = None
    mock_llm_session.replies("ok")
    yield mock_llm_session


async def add_mock_provider(client: httpx.AsyncClient, mock: MockLLM, name: str = "Mock",
                            api_key: str = "sk-test-mock-key-123456") -> dict[str, Any]:
    response = await client.post("/api/providers", json={
        "name": name, "base_url": mock.base_url, "api_key": api_key, "kind": "custom"})
    assert response.status_code == 201, response.text
    provider: dict[str, Any] = response.json()
    models = await client.get(f"/api/providers/{provider['id']}/models")
    assert models.status_code == 200
    return provider
