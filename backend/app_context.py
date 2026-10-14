"""Application service container, built once per app in the lifespan handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.agents.generator import AgentGenerator
from backend.agents.memory import MemoryService
from backend.agents.pool import AgentPool
from backend.agents.runtime import AgentRuntime
from backend.agents.service import AgentService
from backend.agents.tools import build_registry
from backend.agents.tools.base import ToolRegistry
from backend.agents.tools.search import WebSearchService, WebSearchTool
from backend.config import Settings
from backend.core.crypto import SecretBox, load_secret_box
from backend.core.events import EventBus
from backend.core.metrics import Metrics
from backend.core.security import SlidingWindowRateLimiter, TokenService, load_jwt_secret
from backend.core.users import ensure_local_user
from backend.db.session import make_engine, make_sessionmaker, run_migrations
from backend.decision.ensemble import DecisionService
from backend.decision.jev_engine import JevEngine
from backend.decision.laya_engine import LayaEngine
from backend.decision.schemas import configure_evidence_key
from backend.mcp.manager import MCPManager
from backend.orchestrator.approvals import ApprovalBroker
from backend.orchestrator.commands import parse_command
from backend.orchestrator.executor import RunExecutor
from backend.orchestrator.graph import Orchestrator
from backend.orchestrator.runs import RunService
from backend.plugins.registry import PluginRegistry
from backend.providers.router import ProviderRouter
from backend.providers.service import ProviderService
from backend.sandbox.manager import SandboxManager
from backend.screen_control.computer_use_agent import ScreenService, ScreenTestTool
from backend.vault.service import VaultService


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    secrets: SecretBox
    tokens: TokenService
    http: httpx.AsyncClient
    metrics: Metrics
    providers: ProviderService
    router: ProviderRouter
    login_limiter: SlidingWindowRateLimiter
    ip_limiter: SlidingWindowRateLimiter
    local_user_id: str
    events: EventBus
    vault: VaultService
    approvals: ApprovalBroker
    executor: RunExecutor
    runs: RunService
    sandboxes: SandboxManager
    agent_pool: AgentPool
    tools: ToolRegistry
    decisions: DecisionService
    web_search: WebSearchService
    oauth_transport: httpx.AsyncBaseTransport | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    memory: MemoryService = field(init=False)
    runtime: AgentRuntime = field(init=False)
    agents: AgentService = field(init=False)
    agent_generator: AgentGenerator = field(init=False)
    plugins: PluginRegistry = field(init=False)
    orchestrator: Orchestrator = field(init=False)
    screen: ScreenService = field(init=False)
    mcp: MCPManager = field(init=False)

    def __post_init__(self) -> None:
        self.memory = MemoryService(self)
        self.runtime = AgentRuntime(self, self.tools, self.memory)
        self.agents = AgentService(self)
        self.agent_generator = AgentGenerator(self)
        self.plugins = PluginRegistry(self)
        self.orchestrator = Orchestrator(self)
        self.executor.runner = self.orchestrator.run
        self.runs.command_parser = parse_command
        self.runs.delete_hooks.append(self.orchestrator.forget)
        self.tools.register(WebSearchTool())
        self.screen = ScreenService(self)
        self.tools.register(ScreenTestTool())
        self.mcp = MCPManager(self)
        self.tools.add_provider(self.mcp.agent_tools)

        async def _decision_hook(result: Any, user_id: str | None, run_id: str | None) -> None:
            if user_id:
                await self.plugins.run_hooks("on_decision", user_id, decision=result.model_dump(), run_id=run_id)

        self.decisions.observers.append(_decision_hook)

    async def start(self) -> None:
        import asyncio

        self.events.bind_loop(asyncio.get_running_loop())
        await self.vault.load_config()
        await self.decisions.load_config()
        self.plugins.load_third_party(self.settings.plugins_dir)
        await self.orchestrator.start()
        await self.mcp.start_enabled()
        await self.executor.start()
        await self.executor.recover()
        if self.settings.laya_enabled and self.settings.laya_preload:
            self.extras["laya_preload"] = asyncio.create_task(self._preload_laya())

    async def _preload_laya(self) -> None:
        try:
            await self.decisions.laya.load()
        except Exception as exc:  # weights missing → engine stays unavailable
            self.extras["laya_preload_error"] = str(exc)

    async def aclose(self) -> None:
        await self.executor.stop()
        await self.orchestrator.stop()
        await self.screen.shutdown()
        await self.mcp.shutdown()
        await self.sandboxes.shutdown()
        await self.http.aclose()
        await self.engine.dispose()


async def build_context(settings: Settings, http_transport: httpx.AsyncBaseTransport | None = None) -> AppContext:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    configure_evidence_key(settings.data_dir)
    url = settings.resolved_database_url
    await run_migrations(url)
    engine = make_engine(url)
    sessions = make_sessionmaker(engine)
    secrets = load_secret_box(
        settings.data_dir,
        settings.jevora_fernet_key.get_secret_value() if settings.jevora_fernet_key else None,
        settings.use_keyring,
    )
    tokens = TokenService(
        load_jwt_secret(settings.data_dir, settings.jwt_secret.get_secret_value() if settings.jwt_secret else None),
        settings.access_token_ttl_minutes,
        settings.refresh_token_ttl_days,
    )
    http = httpx.AsyncClient(
        transport=http_transport,
        timeout=httpx.Timeout(settings.provider_timeout_s, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True,
    )
    metrics = Metrics()
    providers = ProviderService(settings, sessions, secrets, http)
    router = ProviderRouter(settings, sessions, providers, metrics)
    local_user = await ensure_local_user(sessions)
    events = EventBus(metrics)
    vault = VaultService(settings, sessions, events)
    approvals = ApprovalBroker()
    executor = RunExecutor(sessions, events, settings.max_concurrent_runs, metrics)
    runs = RunService(sessions, events, executor, vault, settings.max_prompt_chars, approvals)

    async def _write_vault(run_id: str) -> None:
        await vault.write_run(run_id)

    executor.finish_hooks.append(_write_vault)
    sandboxes = SandboxManager(settings, sessions)
    agent_pool = AgentPool(events, metrics)

    async def _cancel_tree(run_id: str) -> None:
        agent_pool.cancel_run(run_id)  # parent → every descendant call
        await sandboxes.kill_run(run_id)

    async def _release(run_id: str) -> None:
        agent_pool.drop(run_id)
        await sandboxes.release_run(run_id)

    executor.cancel_hooks.append(_cancel_tree)
    executor.finish_hooks.append(_release)
    ctx = AppContext(
        settings=settings,
        engine=engine,
        sessions=sessions,
        secrets=secrets,
        tokens=tokens,
        http=http,
        metrics=metrics,
        providers=providers,
        router=router,
        login_limiter=SlidingWindowRateLimiter(settings.login_rate_limit_per_minute, 60.0),
        ip_limiter=SlidingWindowRateLimiter(settings.login_rate_limit_per_ip_per_minute, 60.0),
        local_user_id=local_user.id,
        events=events,
        vault=vault,
        approvals=approvals,
        executor=executor,
        runs=runs,
        sandboxes=sandboxes,
        agent_pool=agent_pool,
        tools=build_registry(),
        decisions=DecisionService(
            sessions,
            laya=LayaEngine(model_id=settings.laya_model_id, device=settings.laya_device,
                            enabled=settings.laya_enabled,
                            calibration_path=settings.data_dir / "laya_calibration.json"),
            jev=JevEngine(settings.jev_api_url,
                          settings.jev_api_key.get_secret_value() if settings.jev_api_key else None, http),
            metrics=metrics, default_threshold=settings.escalation_threshold),
        web_search=WebSearchService(settings, http),
    )

    async def _vault_health(_user_id: str) -> dict[str, Any]:
        return await vault.status()

    async def _sandbox_health(_user_id: str) -> dict[str, Any]:
        return await sandboxes.status()

    async def _decision_health(_user_id: str) -> dict[str, Any]:
        return await ctx.decisions.status()

    async def _orchestrator_health(_user_id: str) -> dict[str, Any]:
        return {"ok": ctx.orchestrator.graph is not None, "engine": "LangGraph",
                "active_runs": executor.active_count, "queued_runs": executor.queued_count,
                "checkpoints": str(settings.checkpoint_db_path)}

    async def _mcp_health(user_id: str) -> dict[str, Any]:
        builtin = [
            {"name": "Decision Engine", "kind": "builtin", **(await ctx.decisions.status())},
            {"name": "LangGraph Orchestrator", "kind": "builtin", "ok": ctx.orchestrator.graph is not None},
            {"name": "System Tool & Sandbox", "kind": "builtin", **(await sandboxes.status())},
        ]
        return {**(await ctx.mcp.status(user_id)), "builtin": builtin}

    ctx.extras["health_components"] = {"mcp": _mcp_health, "vault": _vault_health, "sandbox": _sandbox_health,
                                       "decision_engine": _decision_health, "orchestrator": _orchestrator_health}
    return ctx
