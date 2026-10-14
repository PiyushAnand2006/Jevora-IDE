"""JevEngine: optional fallback via TypeSafe's hosted Jev API (same interface; graceful when unavailable).

Jev is waitlisted; when ``JEV_API_URL``/``JEV_API_KEY`` are not configured, or the service is down,
``available()`` is False and the ensemble falls through to the RulesEngine.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from backend.core.logging import get_logger
from backend.decision.interface import DecisionEngine, EngineUnavailable
from backend.decision.laya_engine import laya_questions, state_text
from backend.decision.schemas import DecisionQuestion, DecisionResult, Evidence

log = get_logger(__name__)


class JevEngine(DecisionEngine):
    name = "jev"

    def __init__(self, url: str | None, api_key: str | None, client: httpx.AsyncClient, timeout: float = 5.0) -> None:
        self.url = url.rstrip("/") if url else None
        self.api_key = api_key
        self.client = client
        self.timeout = timeout
        self._health: tuple[float, bool] = (0.0, False)

    async def available(self) -> bool:
        if not self.url or not self.api_key:
            return False
        stamp, ok = self._health
        if time.monotonic() - stamp < 60:
            return ok
        try:
            response = await self.client.get(f"{self.url}/health", timeout=3,
                                             headers={"Authorization": f"Bearer {self.api_key}"})
            ok = response.status_code < 500
        except httpx.HTTPError:
            ok = False
        self._health = (time.monotonic(), ok)
        return ok

    async def _decide(self, question: DecisionQuestion, evidence: Evidence | None) -> DecisionResult:
        if not self.url or not self.api_key:
            raise EngineUnavailable("Jev is not configured (JEV_API_URL / JEV_API_KEY)")
        started = time.perf_counter()
        body = {"state": state_text(question, evidence), "questions": laya_questions(question)}
        try:
            response = await self.client.post(f"{self.url}/v1/decide", json=body, timeout=self.timeout,
                                              headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            self._health = (time.monotonic(), False)
            raise EngineUnavailable(f"Jev request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise EngineUnavailable(f"Jev returned HTTP {response.status_code}")
        data: dict[str, Any] = response.json()
        answer = (data.get("answers") or {}).get("decision") or data
        probs = {k: float(v) for k, v in (answer.get("probabilities") or {}).items() if k in question.options}
        choice = answer.get("choice")
        if choice not in question.options:
            if not probs:
                raise EngineUnavailable("Jev returned no usable answer")
            choice = max(probs, key=lambda k: probs[k])
        confidence = float(answer.get("confidence", probs.get(choice, 0.0)))
        return DecisionResult(type=question.type, choice=choice, confidence=max(0.0, min(1.0, confidence)),
                              scores_per_option={o: round(probs.get(o, 0.0), 4) for o in question.options},
                              engine=self.name, latency_ms=round((time.perf_counter() - started) * 1000, 3),
                              reason="jev api")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "configured": bool(self.url and self.api_key), "url": self.url}
