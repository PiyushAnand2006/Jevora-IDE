"""Plugin registry: built-in toggles that change orchestrator behaviour + third-party plugins.

Third-party plugins are discovered from the ``jevora.plugins`` Python entry-point group and from
``plugin.json`` manifests in ``PLUGINS_DIR``. A plugin exposes ``register(api)`` and may add tools,
graph hooks (before/after a node), and run/decision hooks. See docs/PLUGINS.md.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from backend.core.errors import NotFound
from backend.core.logging import get_logger
from backend.db.models import PluginState

if TYPE_CHECKING:
    from backend.agents.tools.base import Tool
    from backend.app_context import AppContext

log = get_logger(__name__)

Hook = Callable[..., Awaitable[Any]]
HOOK_POINTS = ("before_run", "after_run", "before_node", "after_node", "on_decision")


@dataclass
class PluginInfo:
    id: str
    name: str
    description: str
    default_enabled: bool = True
    builtin: bool = True
    version: str = "1.0"
    source: str = "builtin"
    error: str | None = None
    tools: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)


BUILTINS: list[PluginInfo] = [
    PluginInfo("decision_verification", "Decision-Engine Verification Loop",
               "Anti-hallucination: every build is verified from tool evidence by the Decision Engine and "
               "re-tried / reassigned / escalated until confirmed."),
    PluginInfo("iterative_tester", "Iterative Multi-Agent Tester",
               "Auto test pass after every coding iteration (writes missing tests, runs the suite, screen tests "
               "when requested) feeding bug-fix passes."),
    PluginInfo("dynamic_model_router", "Dynamic Model Router",
               "Auto-allocates heavy/light LLMs per task category; when off, every agent uses one default model."),
]


class PluginAPI:
    """What a third-party plugin's ``register(api)`` receives."""

    def __init__(self, registry: PluginRegistry, plugin_id: str) -> None:
        self._registry = registry
        self._plugin_id = plugin_id

    def add_tool(self, tool: Tool) -> None:
        self._registry.app.tools.register(tool)
        self._registry.infos[self._plugin_id].tools.append(tool.name)

    def add_hook(self, point: str, fn: Hook) -> None:
        if point not in HOOK_POINTS:
            raise ValueError(f"unknown hook point '{point}' (expected one of {HOOK_POINTS})")
        self._registry.hooks.setdefault(point, []).append((self._plugin_id, fn))
        self._registry.infos[self._plugin_id].hooks.append(point)

    @property
    def app(self) -> AppContext:
        return self._registry.app


class PluginRegistry:
    def __init__(self, app: AppContext) -> None:
        self.app = app
        self.infos: dict[str, PluginInfo] = {p.id: PluginInfo(**p.__dict__) for p in BUILTINS}
        self.hooks: dict[str, list[tuple[str, Hook]]] = {}

    # ------------------------------------------------------------------ discovery
    def load_third_party(self, plugins_dir: Path | None) -> None:
        for ep in entry_points(group="jevora.plugins"):
            self._load(ep.name, ep.load, f"entry-point:{ep.value}")
        if plugins_dir is not None and plugins_dir.exists():
            for manifest in sorted(plugins_dir.glob("*/plugin.json")):
                self._load_manifest(manifest)

    def _load_manifest(self, manifest: Path) -> None:
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            plugin_id = str(meta["id"])
            module_file = manifest.parent / str(meta.get("module", "plugin.py"))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("invalid plugin manifest %s: %s", manifest, exc)
            return

        def load() -> Any:
            spec = importlib.util.spec_from_file_location(f"jevora_plugin_{plugin_id}", module_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {module_file}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module

        self._load(plugin_id, load, f"manifest:{manifest}", meta)

    def _load(self, plugin_id: str, loader: Callable[[], Any], source: str,
              meta: dict[str, Any] | None = None) -> None:
        meta = meta or {}
        info = PluginInfo(id=plugin_id, name=str(meta.get("name", plugin_id)),
                          description=str(meta.get("description", "")), builtin=False,
                          default_enabled=bool(meta.get("default_enabled", False)),
                          version=str(meta.get("version", "0")), source=source)
        self.infos[plugin_id] = info
        try:
            target = loader()
            register = getattr(target, "register", target)
            if hasattr(target, "PLUGIN"):
                for key in ("name", "description", "version"):
                    if key in target.PLUGIN:
                        setattr(info, key, str(target.PLUGIN[key]))
            register(PluginAPI(self, plugin_id))
            log.info("loaded plugin %s from %s", plugin_id, source)
        except Exception as exc:
            info.error = f"{type(exc).__name__}: {exc}"
            log.warning("plugin %s failed to load: %s", plugin_id, info.error)

    # ------------------------------------------------------------------ state
    async def states(self, user_id: str) -> dict[str, bool]:
        async with self.app.sessions() as session:
            rows = {r.plugin_id: r.enabled for r in await session.scalars(
                select(PluginState).where(PluginState.user_id == user_id))}
        return {pid: rows.get(pid, info.default_enabled) for pid, info in self.infos.items()}

    async def flags(self, user_id: str) -> dict[str, bool]:
        return await self.states(user_id)

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        states = await self.states(user_id)
        return [{"id": p.id, "name": p.name, "description": p.description, "enabled": states[p.id],
                 "builtin": p.builtin, "version": p.version, "source": p.source, "error": p.error,
                 "tools": p.tools, "hooks": p.hooks} for p in self.infos.values()]

    async def set_enabled(self, user_id: str, plugin_id: str, enabled: bool) -> dict[str, Any]:
        if plugin_id not in self.infos:
            raise NotFound(f"plugin '{plugin_id}' not found")
        async with self.app.sessions() as session:
            row = await session.scalar(select(PluginState).where(PluginState.user_id == user_id,
                                                                 PluginState.plugin_id == plugin_id))
            if row is None:
                session.add(PluginState(user_id=user_id, plugin_id=plugin_id, enabled=enabled))
            else:
                row.enabled = enabled
            await session.commit()
        return next(p for p in await self.list(user_id) if p["id"] == plugin_id)

    async def run_hooks(self, point: str, user_id: str, **payload: Any) -> None:
        handlers = self.hooks.get(point, [])
        if not handlers:
            return
        enabled = await self.states(user_id)
        for plugin_id, fn in handlers:
            if not enabled.get(plugin_id):
                continue
            try:
                await fn(**payload)
            except Exception:
                log.exception("plugin %s hook %s failed", plugin_id, point)
