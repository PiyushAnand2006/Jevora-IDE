from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from backend import main, storage
from backend.schemas import Decision, TaskCategory, WorkVerdict


class FakeProvider:
    """Offline, scripted replacement for the OpenAI-compatible provider."""

    def __init__(self, replies: Iterable[str | None] = ()) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[object, ...]] = []

    async def complete(self, provider, model: str, system: str, prompt: str) -> str | None:
        self.calls.append((provider, model, system, prompt))
        return self.replies.pop(0) if self.replies else None


class StubDecisionEngine:
    """Deterministic DecisionEngine fake with inspectable evidence."""

    name = "stub"

    def __init__(self, verification: Iterable[Decision] = (), route: Decision | None = None) -> None:
        self.verification = list(verification)
        self.route_decision = route or Decision(
            kind="model_routing", choice=TaskCategory.code_generation.value,
            confidence=0.9, engine=self.name, rationale="scripted route",
        )
        self.evidence = []

    def route(self, task: str, mode: str) -> Decision:
        return self.route_decision

    def verify(self, evidence) -> Decision:
        self.evidence.append(evidence)
        if self.verification:
            return self.verification.pop(0)
        return Decision(kind="work_verification", choice=WorkVerdict.confirm_done.value,
                        confidence=0.9, engine=self.name, rationale="scripted verification")


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep configuration, vault notes, and in-memory run state isolated per test."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(storage, "PROVIDERS_FILE", data_dir / "providers.json")
    monkeypatch.setattr(storage, "ROUTER_CONFIG_FILE", data_dir / "router_config.json")
    monkeypatch.setattr(storage, "VAULT_DIR", tmp_path / "vault")
    main.RUNS.clear()
    main.SUBSCRIBERS.clear()
    main.EVENTS.clear()
    yield
    main.RUNS.clear()
    main.SUBSCRIBERS.clear()
    main.EVENTS.clear()


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"
