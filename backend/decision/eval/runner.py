"""Evaluate decision engines on the project's own schemas and gate Laya's promotion.

Metrics per engine: decision latency (mean/p50/p95), verification accuracy, routing quality
(accuracy), incorrect-completion detection (share of not-done cases that were NOT confirmed),
false-confirm rate, and reliability (valid typed output without errors).
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.decision.ensemble import DecisionService
from backend.decision.eval.dataset import DatasetItem
from backend.decision.interface import DecisionEngine, EngineUnavailable
from backend.decision.schemas import SCHEMA_CHOICES


class GateCriteria(BaseModel):
    min_items: int = 30
    verification_accuracy: float = 0.85
    incorrect_completion_detection: float = 0.95
    routing_accuracy: float = 0.75
    reliability: float = 0.99
    p95_latency_ms: float = 300.0


class EngineReport(BaseModel):
    engine: str
    available: bool = True
    error: str | None = None
    n: int = 0
    failures: int = 0
    reliability: float = 0.0
    latency_mean_ms: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    accuracy_by_type: dict[str, float] = Field(default_factory=dict)
    items_by_type: dict[str, int] = Field(default_factory=dict)
    verification_accuracy: float | None = None
    routing_accuracy: float | None = None
    incorrect_completion_detection: float | None = None
    false_confirm_rate: float | None = None
    confusion: dict[str, dict[str, int]] = Field(default_factory=dict)


class EvalReport(BaseModel):
    created_at: str
    dataset_size: int
    dataset_origin: dict[str, int]
    criteria: GateCriteria
    engines: dict[str, EngineReport]
    gate: dict[str, dict[str, bool]] = Field(default_factory=dict)
    promoted: list[str] = Field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 3)


async def evaluate_engine(engine: DecisionEngine, items: list[DatasetItem]) -> EngineReport:
    report = EngineReport(engine=engine.name)
    if not await engine.available():
        report.available = False
        report.error = getattr(engine, "_load_error", None) or "engine unavailable"
        return report
    latencies: list[float] = []
    correct: dict[str, int] = {}
    total: dict[str, int] = {}
    not_done = not_done_rejected = 0
    confirms = false_confirms = 0
    confusion: dict[str, dict[str, int]] = {}
    for item in items:
        question = item.question()
        evidence = item.evidence_obj() if item.evidence else None
        started = time.perf_counter()
        try:
            result = await engine.decide(question, evidence)
        except (EngineUnavailable, Exception) as exc:  # reliability counts every failure
            report.failures += 1
            if isinstance(exc, EngineUnavailable) and report.failures == 1:
                report.error = str(exc)
            total[item.type] = total.get(item.type, 0) + 1
            continue
        latencies.append(result.latency_ms or (time.perf_counter() - started) * 1000)
        valid = result.choice in SCHEMA_CHOICES[item.type] and 0.0 <= result.confidence <= 1.0
        if not valid:
            report.failures += 1
        total[item.type] = total.get(item.type, 0) + 1
        if valid and result.choice == item.label:
            correct[item.type] = correct.get(item.type, 0) + 1
        confusion.setdefault(item.label, {}).setdefault(result.choice, 0)
        confusion[item.label][result.choice] += 1
        if item.type == "verification":
            if item.label != "confirm_done":
                not_done += 1
                if result.choice != "confirm_done":
                    not_done_rejected += 1
            if result.choice == "confirm_done":
                confirms += 1
                if item.label != "confirm_done":
                    false_confirms += 1
    report.n = len(items)
    report.reliability = round(1 - report.failures / max(1, len(items)), 4)
    report.latency_mean_ms = round(statistics.fmean(latencies), 3) if latencies else 0.0
    report.latency_p50_ms = _percentile(latencies, 0.5)
    report.latency_p95_ms = _percentile(latencies, 0.95)
    report.items_by_type = total
    report.accuracy_by_type = {t: round(correct.get(t, 0) / n, 4) for t, n in total.items() if n}
    report.verification_accuracy = report.accuracy_by_type.get("verification")
    report.routing_accuracy = report.accuracy_by_type.get("routing")
    report.incorrect_completion_detection = round(not_done_rejected / not_done, 4) if not_done else None
    report.false_confirm_rate = round(false_confirms / confirms, 4) if confirms else None
    report.confusion = confusion
    return report


def gate(report: EngineReport, criteria: GateCriteria) -> dict[str, bool]:
    """Which schemas this engine is validated for."""
    passed: dict[str, bool] = dict.fromkeys(SCHEMA_CHOICES, False)
    if not report.available or report.reliability < criteria.reliability:
        return passed
    if report.latency_p95_ms > criteria.p95_latency_ms:
        return passed
    if (report.items_by_type.get("verification", 0) >= criteria.min_items
            and (report.verification_accuracy or 0) >= criteria.verification_accuracy
            and (report.incorrect_completion_detection or 0) >= criteria.incorrect_completion_detection):
        passed["verification"] = True
    if (report.items_by_type.get("routing", 0) >= criteria.min_items
            and (report.routing_accuracy or 0) >= criteria.routing_accuracy):
        passed["routing"] = True
    return passed


async def run_eval(service: DecisionService, items: list[DatasetItem], engines: list[str] | None = None,
                   criteria: GateCriteria | None = None, promote: bool = False) -> EvalReport:
    criteria = criteria or GateCriteria()
    names = engines or ["rules", "laya", "jev"]
    reports: dict[str, EngineReport] = {}
    for name in names:
        engine = service.engine(name)
        if engine is not None:
            reports[name] = await evaluate_engine(engine, items)
    origins: dict[str, int] = {}
    for item in items:
        origins[item.origin] = origins.get(item.origin, 0) + 1
    report = EvalReport(created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"), dataset_size=len(items),
                        dataset_origin=origins, criteria=criteria, engines=reports,
                        gate={name: gate(r, criteria) for name, r in reports.items()})
    if promote and "laya" in reports:
        laya_gate = report.gate["laya"]
        validated = {schema: ok for schema, ok in laya_gate.items()}
        await service.save_config({"validated": validated})
        report.promoted = [schema for schema, ok in validated.items() if ok]
    return report


def render_markdown(report: EvalReport) -> str:
    def fmt(value: Any) -> str:
        if value is None:
            return "—"
        return f"{value:.1%}" if isinstance(value, float) and value <= 1 else str(value)

    lines = [f"# Decision engine evaluation — {report.created_at}", "",
             f"Dataset: {report.dataset_size} items "
             f"({', '.join(f'{k}: {v}' for k, v in report.dataset_origin.items())})",
             "", "| Engine | Available | Verification acc. | Incorrect-completion detection | False confirms | "
                 "Routing acc. | Reliability | p50 ms | p95 ms |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, r in report.engines.items():
        lines.append(f"| {name} | {'yes' if r.available else 'no'} | {fmt(r.verification_accuracy)} | "
                     f"{fmt(r.incorrect_completion_detection)} | {fmt(r.false_confirm_rate)} | "
                     f"{fmt(r.routing_accuracy)} | {fmt(r.reliability)} | {r.latency_p50_ms} | {r.latency_p95_ms} |")
    lines += ["", "## Promotion gate", "", f"Criteria: {report.criteria.model_dump()}", ""]
    for name, schemas in report.gate.items():
        ok = [s for s, v in schemas.items() if v]
        lines.append(f"- **{name}**: validated for {', '.join(ok) if ok else 'nothing'}")
    if report.promoted:
        lines.append(f"\nLaya promoted (validated=true) for: {', '.join(report.promoted)}")
    for name, r in report.engines.items():
        if r.error:
            lines.append(f"\n> {name}: {r.error}")
    return "\n".join(lines) + "\n"
