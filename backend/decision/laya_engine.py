"""LayaEngine: the primary, local, non-generative Decision Engine (``pip install laya``).

* Weights come from Hugging Face (``LAYA_MODEL_ID``); loading is lazy, done once (singleton) in a
  worker thread, and failures (package or weights missing) make the engine *unavailable* rather
  than breaking the run.
* Calls are micro-batched: concurrent requests for the same schema share one ``predict_batch``.
* Confidence is Laya's calibrated ``answer_confidence``; a calibration file fitted by the
  fine-tuning harness is loaded when present.
* Laya is near-random zero-shot (~36%), so each schema carries a ``validated`` flag that stays
  ``False`` until the project's own evaluation passes; until then results are **shadow** only.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from backend.core.logging import get_logger
from backend.decision.interface import DecisionEngine, EngineUnavailable
from backend.decision.schemas import DecisionQuestion, DecisionResult, Evidence

log = get_logger(__name__)

OPTION_CRITERIA: dict[str, dict[str, str]] = {
    "verification": {
        "confirm_done": "the evidence shows the task is complete: tests pass and the required changes exist",
        "restart_same_agent": "the evidence shows failures the same agent can fix by trying again",
        "reassign_new_agent": "the same agent keeps failing; a different agent or stronger model should take over",
        "escalate": "the evidence is contradictory, risky or the retry budget is exhausted; ask the user",
    },
    "routing": {
        "heavy_reasoning": "complex planning, architecture, analysis or multi-step reasoning",
        "fast_classification": "short, simple classification or conversational reply",
        "code_generation": "writing or fixing source code and tests",
        "creative_ui": "visual design, UI layout, styling or front-end components",
        "research_retrieval": "finding, comparing and summarising information from sources",
    },
    "next_action": {o: o.replace("_", " ") for o in ("plan", "code", "design_ui", "research", "analyze", "test",
                                                     "critique", "verify", "finalize", "escalate")},
    "assignment": {o: f"the {o} agent" for o in ("planner", "coder", "tester", "critic", "ui-designer",
                                                  "researcher", "analyst", "screen-tester", "chat")},
    "retry": {"retry_same": "transient error; retry the same way", "retry_stronger_model": "use a stronger model",
              "give_up": "the error cannot be fixed by retrying", "escalate": "ask the user"},
    "completion": {"complete": "the output fully answers the request with grounded evidence",
                   "incomplete": "the output is missing parts or is not grounded",
                   "escalate": "unclear; ask the user"},
}
INSTRUCTIONS: dict[str, str] = {
    "verification": "Given only the machine-produced evidence, is the agent's work done?",
    "routing": "Which model category fits this task?",
    "next_action": "What should the orchestrator do next?",
    "assignment": "Which agent should handle this task?",
    "retry": "How should the system react to this failure?",
    "completion": "Is the output complete and grounded?",
}


def state_text(question: DecisionQuestion, evidence: Evidence | None) -> str:
    """Compact, deterministic serialisation of features + evidence (never agent narrative)."""
    payload: dict[str, Any] = {"state": question.state}
    if evidence is not None and evidence.items:
        payload["evidence"] = [{"kind": i.kind, **{k: v for k, v in i.payload.items()
                                                   if k not in ("output_tail", "diff_tail", "stdout_tail",
                                                                "stderr_tail", "action_log")}}
                               for i in evidence.items[-8:]]
    return json.dumps(payload, sort_keys=True, default=str)[:6000]


def laya_questions(question: DecisionQuestion) -> dict[str, Any]:
    criteria = OPTION_CRITERIA[question.type]
    return {"decision": {"type": "choice", "instructions": question.instructions or INSTRUCTIONS[question.type],
                         "criteria": {o: criteria.get(o, o) for o in question.options}}}


class LayaBackend(Protocol):
    def predict_batch(self, states: list[str], questions: dict[str, Any]) -> list[dict[str, Any]]: ...


def load_real_backend(model_id: str, device: str | None, calibration: Path | None) -> LayaBackend:
    import laya

    agent = laya.load(model_id, device=device)
    if calibration is not None and calibration.exists():
        agent.load_calibration(str(calibration))
        log.info("loaded Laya calibration from %s", calibration)
    return agent


@dataclass
class _Pending:
    state: str
    future: asyncio.Future[dict[str, Any]]


class LayaEngine(DecisionEngine):
    name = "laya"

    def __init__(self, *, model_id: str, device: str | None = None, enabled: bool = True,
                 calibration_path: Path | None = None,
                 backend_factory: Callable[[], LayaBackend] | None = None,
                 validated: Callable[[str], bool] | None = None, batch_window_s: float = 0.005) -> None:
        self.model_id = model_id
        self.device = device
        self.enabled = enabled
        self.calibration_path = calibration_path
        self._custom_factory = backend_factory is not None
        self._factory = backend_factory or (lambda: load_real_backend(model_id, device, calibration_path))
        self._validated = validated or (lambda _t: False)
        self._backend: LayaBackend | None = None
        self._load_error: str | None = None
        self._load_lock = threading.Lock()
        self._batch_window = batch_window_s
        self._queues: dict[str, list[_Pending]] = {}
        self._flushers: dict[str, asyncio.Task[None]] = {}
        self.load_seconds: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def _load_sync(self) -> LayaBackend:
        with self._load_lock:
            if self._backend is not None:
                return self._backend
            if self._load_error is not None:
                raise EngineUnavailable(self._load_error)
            started = time.perf_counter()
            try:
                self._backend = self._factory()
            except ImportError:
                self._load_error = "laya is not installed (pip install laya)"
            except Exception as exc:  # weights missing, no network, OOM, …
                self._load_error = f"could not load Laya weights '{self.model_id}': {type(exc).__name__}: {exc}"
            if self._backend is None:
                log.warning("Laya unavailable: %s", self._load_error)
                raise EngineUnavailable(self._load_error or "unavailable")
            self.load_seconds = round(time.perf_counter() - started, 2)
            log.info("Laya loaded in %.2fs", self.load_seconds)
            return self._backend

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    async def available(self) -> bool:
        if not self.enabled:
            return False
        if self._backend is not None:
            return True
        if self._load_error is not None:
            return False
        if not self._custom_factory:
            import importlib.util

            if importlib.util.find_spec("laya") is None:
                self._load_error = "laya is not installed (pip install laya)"
                return False
        return True

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    def validated(self, decision_type: str) -> bool:
        return self._validated(decision_type)

    # ------------------------------------------------------------------ inference
    async def _predict(self, schema_key: str, questions: dict[str, Any], state: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._queues.setdefault(schema_key, []).append(_Pending(state, future))
        if schema_key not in self._flushers or self._flushers[schema_key].done():
            self._flushers[schema_key] = asyncio.create_task(self._flush(schema_key, questions))
        return await future

    async def _flush(self, schema_key: str, questions: dict[str, Any]) -> None:
        await asyncio.sleep(self._batch_window)  # gather concurrent requests into one forward pass
        batch = self._queues.pop(schema_key, [])
        if not batch:
            return
        try:
            backend = await asyncio.to_thread(self._load_sync)
            results = await asyncio.to_thread(backend.predict_batch, [p.state for p in batch], questions)
        except Exception as exc:
            for pending in batch:
                if not pending.future.done():
                    pending.future.set_exception(exc if isinstance(exc, EngineUnavailable)
                                                 else EngineUnavailable(f"Laya inference failed: {exc}"))
            return
        for pending, result in zip(batch, results, strict=False):
            if not pending.future.done():
                pending.future.set_result(result)

    async def _decide(self, question: DecisionQuestion, evidence: Evidence | None) -> DecisionResult:
        if not self.enabled:
            raise EngineUnavailable("Laya disabled (LAYA_ENABLED=false)")
        started = time.perf_counter()
        questions = laya_questions(question)
        raw = await self._predict(f"{question.type}:{','.join(question.options)}", questions,
                                  state_text(question, evidence))
        answer = (raw.get("answers") or {}).get("decision") or {}
        probs = {k: float(v) for k, v in (answer.get("probabilities") or {}).items() if k in question.options}
        choice = answer.get("choice")
        if choice not in question.options:
            if not probs:
                raise EngineUnavailable("Laya returned no usable answer")
            choice = max(probs, key=lambda k: probs[k])
        confidence = float(answer.get("answer_confidence", answer.get("confidence", probs.get(choice, 0.0))))
        return DecisionResult(type=question.type, choice=choice, confidence=max(0.0, min(1.0, confidence)),
                              scores_per_option={o: round(probs.get(o, 0.0), 4) for o in question.options},
                              engine=self.name, latency_ms=round((time.perf_counter() - started) * 1000, 3),
                              reason="laya forward pass")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "model_id": self.model_id, "enabled": self.enabled, "loaded": self.loaded,
                "load_error": self._load_error, "load_seconds": self.load_seconds,
                "calibration": str(self.calibration_path) if self.calibration_path else None}
