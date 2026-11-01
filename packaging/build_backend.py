"""Build the PyInstaller onedir backend consumed by electron-builder.

Output: dist-backend/jevora-backend/  (referenced by electron/backend.js and
the `extraResources` block in package.json).

Run via:  npm run backend:build   (i.e. python packaging/build_backend.py)

Invokes PyInstaller as a module under the current interpreter
(``python -m PyInstaller``) so no external binary needs to be located; every
command element below is a compile-time constant or a path derived from
__file__ — nothing user-supplied ever reaches argv, and the subprocess always
runs with shell=False.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist-backend"
BUILD = ROOT / "build-backend"
PACK = ROOT / "packaging"
ENTRY = PACK / "backend_main.py"
NAME = "jevora-backend"

# uvicorn pulls its loop/protocol implementations dynamically, so they must be
# listed explicitly or the frozen exe fails at boot with "module not found".
HIDDEN = (
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "aiosqlite",
    "anyio._backends._asyncio",
)


# The packaged app is governed by the Rules engine; the Laya real-weights stack
# (torch/transformers/laya) is a dev-env workflow and would add ~450 MB, so it
# is excluded — laya_engine handles the ImportError and reports unavailable.
EXCLUDED = (
    "torch", "torchvision", "torchaudio", "transformers", "tokenizers",
    "safetensors", "huggingface_hub", "hf_xet", "laya", "unsloth",
    "triton", "sympy", "networkx", "jinja2",
)


def pyinstaller_args() -> list[str]:
    """Literal, constant-derived argv for the frozen build."""
    return [
        "-m", "PyInstaller",
        "--name", NAME,
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        f"--distpath={OUT}",
        f"--workpath={BUILD}",
        f"--specpath={PACK}",
        *(f"--hidden-import={m}" for m in HIDDEN),
        *(f"--exclude-module={m}" for m in EXCLUDED),
        # playwright's node driver + driver package must ship inside the frozen
        # backend for browser control (screen testing, browser-control sessions)
        "--collect-all", "playwright",
        "--collect-all", "greenlet",
        # package data the backend reads at import/runtime (prompts, alembic)
        f"--add-data={ROOT / 'backend' / 'agents' / 'system_prompt.md'};backend/agents",
        f"--add-data={ROOT / 'backend' / 'db' / 'migrations'};backend/db/migrations",
        str(ENTRY),
    ]


def main() -> int:
    if not ENTRY.exists():
        print(f"entry script missing: {ENTRY}", file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, *pyinstaller_args()],
        cwd=str(ROOT),
        shell=False,
    )
    if result.returncode == 0:
        print(f"[backend-build] OK -> {OUT / NAME}")
    else:
        print("[backend-build] FAILED", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
