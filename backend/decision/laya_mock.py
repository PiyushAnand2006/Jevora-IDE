"""Stand-in Laya backends for tests and demos when real weights are not available.

* ``TrainedLayaMock``  — behaves like a fine-tuned checkpoint: derives its answer from the evidence
  (via the rules logic) and returns a calibrated-looking distribution.
* ``ZeroShotLayaMock`` — behaves like the base checkpoint zero-shot: near-random choices.

Both implement the real ``laya`` Agent's ``predict_batch(states, questions)`` shape exactly.
"""

from __future__ import annotations

import json
import random
from typing import Any

from backend.decision.rules_engine import RulesEngine
from backend.decision.schemas import PAYLOAD_MODELS, DecisionQuestion, Evidence, EvidenceFactory


def _answer(options: list[str], choice: str, top: float) -> dict[str, Any]:
    rest = (1.0 - top) / max(1, len(options) - 1)
    probs = {o: (top if o == choice else rest) for o in options}
    return {"answers": {"decision": {"type": "choice", "choice": choice, "probabilities": probs,
                                     "confidence": top, "answer_confidence": top}}}


class TrainedLayaMock:
    def __init__(self, confidence: float = 0.9) -> None:
        self.confidence = confidence
        self._rules = RulesEngine()
        self.calls = 0

    def predict_batch(self, states: list[str], questions: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls += 1
        options = list(questions["decision"]["criteria"])
        out = []
        for raw in states:
            data = json.loads(raw)
            qtype = _infer_type(options)
            evidence = Evidence(items=[
                EvidenceFactory.make(e["kind"], "sandbox", PAYLOAD_MODELS[e["kind"]].model_validate(
                    {k: v for k, v in e.items() if k != "kind"}))
                for e in data.get("evidence", []) if e.get("kind") in PAYLOAD_MODELS])
            question = DecisionQuestion(type=qtype, options=options, state=data.get("state", {}))
            handler = {"verification": self._rules._verify, "routing": self._rules._route,
                       "next_action": self._rules._next_action, "assignment": self._rules._assign,
                       "retry": self._rules._retry, "completion": self._rules._completion}[qtype]
            result = handler(question, evidence)
            out.append(_answer(options, result.choice, self.confidence))
        return out


class ZeroShotLayaMock:
    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)

    def predict_batch(self, states: list[str], questions: dict[str, Any]) -> list[dict[str, Any]]:
        options = list(questions["decision"]["criteria"])
        return [_answer(options, self._rng.choice(options), round(self._rng.uniform(0.3, 0.6), 3)) for _ in states]


def _infer_type(options: list[str]) -> str:
    from backend.decision.schemas import SCHEMA_CHOICES

    for name, choices in SCHEMA_CHOICES.items():
        if set(options) == set(choices):
            return name
    return "verification"
