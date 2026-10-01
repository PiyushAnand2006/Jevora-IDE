"""IDE Coder — backend configuration.

All paths are resolved relative to the project root (the folder that
contains ``backend/`` and ``frontend/``) so the app is portable.
"""
from __future__ import annotations

import os
from pathlib import Path

# Project root = parent of this file's directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where agents are allowed to read/write files and run commands.
# Defaults to the project root; override with IDE_WORKSPACE_DIR.
WORKSPACE_DIR = Path(os.environ.get("IDE_WORKSPACE_DIR", PROJECT_ROOT)).resolve()

# Obsidian vault for run history / decision logs (architecture §2.6).
VAULT_DIR = Path(os.environ.get("IDE_VAULT_DIR", PROJECT_ROOT / "vault")).resolve()

# Local JSON config store (providers, model-router mapping).
DATA_DIR = Path(os.environ.get("IDE_DATA_DIR", PROJECT_ROOT / "backend" / "data")).resolve()
PROVIDERS_FILE = DATA_DIR / "providers.json"
ROUTER_CONFIG_FILE = DATA_DIR / "router_config.json"

# Frontend directory served statically by FastAPI.
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Decision layer settings.
# Engine name: "heuristic" (default, offline) | "laya" | "jev"
DECISION_ENGINE = os.environ.get("IDE_DECISION_ENGINE", "heuristic")
# Below this confidence the decision layer escalates to the user.
ESCALATION_CONFIDENCE_THRESHOLD = float(os.environ.get("IDE_ESCALATION_THRESHOLD", "0.55"))
# Hard iteration ceiling per task (architecture §2.1).
MAX_ITERATIONS = int(os.environ.get("IDE_MAX_ITERATIONS", "3"))

# Optional engine endpoints/keys (only used when the engine is selected).
JEV_API_URL = os.environ.get("JEV_API_URL", "")
JEV_API_KEY = os.environ.get("JEV_API_KEY", "")
LAYA_MODEL_ID = os.environ.get("LAYA_MODEL_ID", "convai/laya-base")

# Default model-router mapping: task category -> model tier.
# Tiers resolve against the saved providers at runtime.
DEFAULT_ROUTER_CONFIG = {
    "tiers": {
        "heavy_reasoning": {"provider": None, "model": "gpt-4o"},
        "fast_classification": {"provider": None, "model": "gpt-4o-mini"},
        "code_generation": {"provider": None, "model": "gpt-4o"},
        "creative_ui": {"provider": None, "model": "gpt-4o"},
        "research_retrieval": {"provider": None, "model": "gpt-4o-mini"},
    }
}

for d in (VAULT_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)
