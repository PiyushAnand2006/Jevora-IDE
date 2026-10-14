"""Fine-tuning harness for Laya on the project's own decision data.

    python -m backend.decision.finetune export [--weak]          # our JSONL + laya (state, questions, targets)
    python -m backend.decision.finetune calibrate                # fit Laya temperatures on labelled data (CPU ok)
    python -m backend.decision.finetune train --trainer PATH     # run Laya's official RLCD training script
    python -m backend.decision.finetune all [--trainer PATH]     # export → (train) → calibrate → eval --promote

Laya is near-random zero-shot; its headline accuracy only holds after fine-tuning on your own
decisions. The promotion gate (``backend.decision.eval --promote``) is what finally flips
``validated=true`` — fine-tuning alone never does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from backend.config import get_settings
from backend.db.session import make_engine, make_sessionmaker, run_migrations
from backend.decision.eval.dataset import dump_laya_jsonl, export_logged, load_jsonl, seed_dataset, to_laya_records
from backend.decision.schemas import configure_evidence_key


async def export(weak: bool, include_seed: bool) -> tuple[Path, Path, int]:
    settings = get_settings()
    configure_evidence_key(settings.data_dir)
    await run_migrations(settings.resolved_database_url)
    engine = make_engine(settings.resolved_database_url)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = settings.data_dir / "datasets"
    path, count = await export_logged(make_sessionmaker(engine), out_dir / f"decisions-{stamp}.jsonl",
                                      include_engine_choices=weak)
    await engine.dispose()
    items = load_jsonl(path)
    if include_seed:
        items += seed_dataset()
    laya_path = dump_laya_jsonl(items, out_dir / f"laya-{stamp}.jsonl")
    print(f"exported {count} logged decisions (+{len(items) - count} seed) → {path}\nlaya format → {laya_path}")
    return path, laya_path, len(items)


def calibrate(dataset: Path | None) -> Path:
    settings = get_settings()
    try:
        import laya
        from laya.calibrate import records_from_labeled
    except ImportError:
        raise SystemExit("laya is not installed: pip install 'laya>=0.3.20' (pulls torch + transformers)") from None
    items = load_jsonl(dataset) if dataset else seed_dataset()
    pairs = to_laya_records(items)
    agent = laya.load(settings.laya_model_id, device=settings.laya_device)
    records = records_from_labeled(agent, pairs)
    result = agent.fit_temperatures(records, compute_ece=True)
    target = settings.data_dir / "laya_calibration.json"
    agent.save_calibration(str(target))
    print(json.dumps({"calibration": str(target), "fit": {k: v for k, v in result.items() if k != "records"}},
                     default=str, indent=2))
    return target


def train(trainer: Path, laya_data: Path, extra: str) -> int:
    if not trainer.exists():
        raise SystemExit(f"trainer script not found: {trainer} (get it from the laya repository, "
                         "notebooks/laya_finetune_typed_decisions_mps.py)")
    settings = get_settings()
    out = settings.data_dir / "laya-finetuned"
    argv = [sys.executable, str(trainer), "--data", str(laya_data), "--output", str(out), *shlex.split(extra)]
    print("running:", " ".join(shlex.quote(a) for a in argv))
    code = subprocess.call(argv)
    if code == 0:
        print(f"\nfine-tuned checkpoint in {out}. Set LAYA_MODEL_ID={out} and run "
              "`python -m backend.decision.eval --promote`.")
    return code


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.decision.finetune")
    sub = parser.add_subparsers(dest="command", required=True)
    p_export = sub.add_parser("export")
    p_export.add_argument("--weak", action="store_true", help="add unlabelled rules decisions as weak labels")
    p_export.add_argument("--no-seed", action="store_true")
    p_cal = sub.add_parser("calibrate")
    p_cal.add_argument("--dataset", type=Path)
    p_train = sub.add_parser("train")
    p_train.add_argument("--trainer", type=Path, required=True)
    p_train.add_argument("--data", type=Path)
    p_train.add_argument("--trainer-args", default="")
    p_all = sub.add_parser("all")
    p_all.add_argument("--trainer", type=Path)
    p_all.add_argument("--trainer-args", default="")
    p_all.add_argument("--weak", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "export":
        await export(args.weak, not args.no_seed)
        return 0
    if args.command == "calibrate":
        calibrate(args.dataset)
        return 0
    if args.command == "train":
        data = args.data or (await export(False, True))[1]
        return train(args.trainer, data, args.trainer_args)
    dataset, laya_data, _ = await export(args.weak, True)
    if args.trainer:
        code = train(args.trainer, laya_data, args.trainer_args)
        if code != 0:
            return code
    calibrate(dataset)
    from backend.decision.eval.__main__ import main as eval_main

    return await eval_main(["--promote", "--dataset", str(dataset)])


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
