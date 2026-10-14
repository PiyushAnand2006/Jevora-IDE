"""CLI: ``python -m backend.decision.eval``.

Examples::

    python -m backend.decision.eval                       # rules + laya + jev on seed + labelled data
    python -m backend.decision.eval --promote             # flip Laya validated=true where the gate passes
    python -m backend.decision.eval --mock-laya trained   # demo without weights
    python -m backend.decision.eval --export data.jsonl   # export labelled decisions only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import get_settings
from backend.db.session import make_engine, make_sessionmaker, run_migrations
from backend.decision.ensemble import DecisionService
from backend.decision.eval.dataset import export_logged, load_jsonl, seed_dataset, write_jsonl
from backend.decision.eval.runner import GateCriteria, render_markdown, run_eval
from backend.decision.jev_engine import JevEngine
from backend.decision.laya_engine import LayaEngine
from backend.decision.laya_mock import TrainedLayaMock, ZeroShotLayaMock
from backend.decision.schemas import configure_evidence_key


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.decision.eval")
    parser.add_argument("--dataset", type=Path, help="JSONL dataset (default: seed + labelled decisions)")
    parser.add_argument("--no-seed", action="store_true", help="do not include the synthetic seed dataset")
    parser.add_argument("--engines", default="rules,laya,jev")
    parser.add_argument("--promote", action="store_true", help="apply the promotion gate to Laya")
    parser.add_argument("--mock-laya", choices=["trained", "zero-shot"], help="use a stand-in Laya backend")
    parser.add_argument("--export", type=Path, help="export labelled decisions to JSONL and exit")
    parser.add_argument("--out", type=Path, help="report directory (default: <data_dir>/eval)")
    parser.add_argument("--min-items", type=int, default=30)
    args = parser.parse_args(argv)

    import httpx

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_evidence_key(settings.data_dir)
    await run_migrations(settings.resolved_database_url)
    engine = make_engine(settings.resolved_database_url)
    sessions = make_sessionmaker(engine)
    if args.export:
        path, count = await export_logged(sessions, args.export)
        print(f"exported {count} labelled decisions to {path}")
        await engine.dispose()
        return 0

    factory: Any = None
    if args.mock_laya == "trained":
        factory = TrainedLayaMock
    elif args.mock_laya == "zero-shot":
        factory = ZeroShotLayaMock
    async with httpx.AsyncClient() as client:
        service = DecisionService(
            sessions,
            laya=LayaEngine(model_id=settings.laya_model_id, device=settings.laya_device, enabled=settings.laya_enabled,
                            calibration_path=settings.data_dir / "laya_calibration.json", backend_factory=factory),
            jev=JevEngine(settings.jev_api_url,
                          settings.jev_api_key.get_secret_value() if settings.jev_api_key else None, client),
            default_threshold=settings.escalation_threshold)
        await service.load_config()
        items = [] if args.no_seed else seed_dataset()
        if args.dataset:
            items += load_jsonl(args.dataset)
        else:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            labelled_path, _ = await export_logged(sessions, settings.data_dir / "datasets" / f"labelled-{stamp}.jsonl")
            items += load_jsonl(labelled_path)
        report = await run_eval(service, items, [e.strip() for e in args.engines.split(",") if e.strip()],
                                GateCriteria(min_items=args.min_items), promote=args.promote)
    out = args.out or settings.data_dir / "eval"
    out.mkdir(parents=True, exist_ok=True)
    stamp = report.created_at.replace(":", "").replace("-", "")[:15]
    (out / f"report-{stamp}.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / f"report-{stamp}.md").write_text(render_markdown(report), encoding="utf-8")
    (out / "latest.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    write_jsonl(items, out / f"dataset-{stamp}.jsonl")
    print(render_markdown(report))
    print(json.dumps({"report": str(out / f"report-{stamp}.md"), "promoted": report.promoted}))
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
