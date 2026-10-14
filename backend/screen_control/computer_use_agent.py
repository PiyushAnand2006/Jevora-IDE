"""Screen-control testing agent: a computer-use loop that produces *evidence only*.

Loop guards: max steps, repeated-action detection, per-step screenshot, coordinate scaling/clamping,
domain allow-list. The output is a signed ``screenshot`` evidence item (screenshots, action log,
console/network errors, driver-evaluated assertions). The model's own narrative is never evidence.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.agents.pool import CallNode
from backend.agents.templates import template
from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema
from backend.core.logging import get_logger
from backend.decision.schemas import Assertion, EvidenceFactory, EvidenceItem, ScreenshotPayload
from backend.providers.base import ProviderError
from backend.providers.router import CallMeta
from backend.screen_control.driver import Action, AssertionSpec, DomainNotAllowed, ScreenDriver
from backend.screen_control.models import ComputerUseModel, select_model

if TYPE_CHECKING:
    from backend.agents.runtime import RunContext
    from backend.app_context import AppContext
    from backend.orchestrator.state import GraphState

log = get_logger(__name__)
REPEAT_LIMIT = 3
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_EXPECT_RE = re.compile(r"(?:expect|expects|see|shows?|contains?|displays?)\s+[\"“']([^\"”']+)[\"”']", re.I)


@dataclass
class ScreenRunResult:
    call_id: str
    evidence: EvidenceItem
    payload: ScreenshotPayload


def clamp(action: Action, width: int, height: int, scale_x: float = 1.0, scale_y: float = 1.0) -> Action:
    if action.x is None or action.y is None:
        return action
    x = min(max(0, round(action.x * scale_x)), width - 1)
    y = min(max(0, round(action.y * scale_y)), height - 1)
    return action.model_copy(update={"x": x, "y": y})


def parse_test_request(text: str) -> tuple[str | None, list[AssertionSpec]]:
    url_match = _URL_RE.search(text)
    url = url_match.group(0).rstrip(".,)") if url_match else None
    specs = [AssertionSpec(type="text_visible", value=v.strip()) for v in _EXPECT_RE.findall(text)]
    return url, specs


class ComputerUseAgent:
    def __init__(self, app: AppContext) -> None:
        self.app = app

    def _driver(self) -> ScreenDriver:
        from backend.screen_control.playwright_driver import PlaywrightDriver

        s = self.app.settings
        ws_endpoint = self.app.extras.get("playwright_ws_endpoint") if s.screen_browser == "docker" else None
        return PlaywrightDriver(allowed_domains=list(s.screen_allowed_domains), width=s.screen_viewport_width,
                                height=s.screen_viewport_height, ws_endpoint=ws_endpoint)

    async def run(self, rc: RunContext, *, goal: str, url: str | None, assertions: list[AssertionSpec],
                  driver: ScreenDriver | None = None, model: ComputerUseModel | None = None,
                  parent_call_id: str | None = None, max_steps: int | None = None) -> ScreenRunResult:
        settings = self.app.settings
        max_steps = max_steps or settings.screen_max_steps
        call_id = await rc.recorder.start_call(role="screen-tester", agent_name="screen-tester", task=goal[:4000],
                                               parent_call_id=parent_call_id, depth=1 if parent_call_id else 0,
                                               category="computer_use")
        node = CallNode(call_id=call_id, config=template("screen-tester"), key=f"screen:{goal[:40]}",
                        depth=1 if parent_call_id else 0)
        self.app.agent_pool.enter(rc.scope, node)
        driver = driver or self._driver()
        artifacts = settings.data_dir / "artifacts" / rc.run_id
        artifacts.mkdir(parents=True, exist_ok=True)
        screenshots: list[dict[str, str]] = []
        action_log: list[dict[str, Any]] = []
        recent: deque[str] = deque(maxlen=REPEAT_LIMIT)
        stopped = "max steps reached"
        results: list[Assertion] = []
        steps = 0

        async def capture(label: str) -> bytes:
            png = await driver.screenshot()
            digest = hashlib.sha256(png).hexdigest()
            path = artifacts / f"{call_id}-{len(screenshots):02d}-{label}.png"
            path.write_bytes(png)
            rel = path.relative_to(settings.data_dir).as_posix()
            screenshots.append({"path": rel, "sha256": digest})
            await rc.recorder.add_artifact("screenshot", path=rel, meta={"sha256": digest, "step": len(screenshots)},
                                           call_id=call_id)
            return png

        try:
            if model is None:
                model = await select_model(self.app.router, CallMeta(user_id=rc.user_id, run_id=rc.run_id,
                                                                     call_id=call_id, category="computer_use"),
                                           self.app.http, None)
            await driver.start(url)
            for step in range(max_steps):
                if rc.scope.cancelled or node.cancelled:
                    stopped = "cancelled"
                    break
                png = await capture(f"step{step}")
                try:
                    action = await model.next_action(goal, png, action_log, driver.width, driver.height)
                except (ProviderError, ValueError) as exc:
                    stopped = f"model error: {exc}"[:300]
                    break
                if step == 0:
                    await rc.recorder.set_call_model(call_id, getattr(model, "model", None) or model.name,
                                                     getattr(model, "provider_id", None))
                action = clamp(action, driver.width, driver.height)
                if action.action == "done":
                    stopped = f"model finished: {action.reason}"[:300]
                    break
                recent.append(action.signature())
                if len(recent) == REPEAT_LIMIT and len(set(recent)) == 1:
                    stopped = f"repeated action {action.action} {REPEAT_LIMIT} times"
                    break
                rc.recorder.emit("tool.call", {"tool": "screen", "action": action.model_dump(exclude_none=True)},
                                 call_id=call_id)
                try:
                    outcome = await driver.perform(action)
                except DomainNotAllowed as exc:
                    action_log.append({"action": action.model_dump(exclude_none=True), "result": f"blocked: {exc}"})
                    stopped = f"blocked: {exc}"
                    break
                steps += 1
                action_log.append({"action": action.model_dump(exclude_none=True), "result": outcome,
                                   "url": await driver.current_url()})
                rc.recorder.emit("tool.result", {"tool": "screen", "ok": True, "preview": outcome}, call_id=call_id)
            await capture("final")
            specs = list(assertions)
            if not any(s.type == "no_console_errors" for s in specs):
                specs.append(AssertionSpec(type="no_console_errors"))
            results = [await driver.check(spec) for spec in specs]
        except DomainNotAllowed as exc:
            stopped = f"blocked: {exc}"
        except ProviderError as exc:
            stopped = f"no computer-use model available: {exc}"[:300]
        except Exception as exc:
            log.exception("screen test failed")
            stopped = f"driver error: {type(exc).__name__}: {exc}"[:300]
        finally:
            final_url = await driver.current_url()
            console, network = driver.console_errors(), driver.network_errors()
            await driver.close()
            self.app.agent_pool.exit(rc.scope, node)
        payload = ScreenshotPayload(url=final_url or (url or ""), screenshots=screenshots, assertions=results,
                                    console_errors=console[:50], network_errors=network[:50], steps=steps,
                                    stopped_reason=stopped, action_log=action_log[:100])
        evidence = EvidenceFactory.make("screenshot", "screen_driver", payload, call_id)
        await rc.recorder.add_artifact("log", meta={"screen_action_log": action_log[:100], "stopped": stopped},
                                       call_id=call_id)
        passed = sum(1 for a in results if a.passed)
        summary = (f"screen test: {passed}/{len(results)} assertions passed, {steps} actions, "
                   f"{len(screenshots)} screenshots; stopped: {stopped}")
        await rc.recorder.finish_call(call_id, status="done" if results else "failed", output=summary,
                                      error=None if results else stopped)
        return ScreenRunResult(call_id=call_id, evidence=evidence, payload=payload)


class ScreenService:
    def __init__(self, app: AppContext) -> None:
        self.app = app
        self.agent = ComputerUseAgent(app)
        self._container: str | None = None

    async def ensure_browser_server(self) -> str:
        """SCREEN_BROWSER=docker: run a Playwright server container matching the client version."""
        from importlib.metadata import version

        from backend.sandbox.docker_sandbox import docker

        endpoint = self.app.extras.get("playwright_ws_endpoint")
        if endpoint:
            return str(endpoint)
        pw_version = version("playwright")
        image = f"mcr.microsoft.com/playwright:v{pw_version}-noble"
        name = "jevora-playwright"
        await docker("rm", "-f", name, timeout=30)
        result = await docker("run", "-d", "--rm", "--name", name, "-p", "127.0.0.1:3907:3000",
                              "--add-host=host.docker.internal:host-gateway", image, "npx", "-y",
                              f"playwright@{pw_version}", "run-server", "--port", "3000",
                              "--host", "0.0.0.0",  # noqa: S104 - inside the container; published on 127.0.0.1
                              timeout=900)
        if result.exit_code != 0:
            raise RuntimeError(f"could not start the Playwright server container: {result.stderr[-300:]}")
        self._container = name
        endpoint = "ws://127.0.0.1:3907/"
        self.app.extras["playwright_ws_endpoint"] = endpoint
        return endpoint

    async def shutdown(self) -> None:
        if self._container:
            from backend.sandbox.docker_sandbox import docker

            await docker("rm", "-f", self._container, timeout=30)
            self._container = None

    async def run_for_task(self, rc: RunContext, state: GraphState) -> list[EvidenceItem]:
        text = state.get("command_arg") or state["task"]
        url, specs = parse_test_request(text)
        if url is None:
            return []
        if self.app.settings.screen_browser == "docker":
            await self.ensure_browser_server()
        result = await self.agent.run(rc, goal=f"Test the application: {text}", url=url, assertions=specs)
        return [result.evidence]


class ScreenTestTool(Tool):
    name = "screen_test"
    description = ("Drive the web app at `url` in a real browser with the computer-use agent to accomplish `goal`, "
                   "then check `expect_text` strings are visible. Returns screenshot evidence and assertion results.")
    permission = "screen.control"
    parameters = schema({"url": {"type": "string"}, "goal": {"type": "string"},
                         "expect_text": {"type": "array", "items": {"type": "string"}}}, ["url", "goal"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        rc = ctx.extras.get("run_context")
        if rc is None:
            return ToolResult(ok=False, content="error: screen tests need a run context")
        specs = [AssertionSpec(type="text_visible", value=str(t)) for t in args.get("expect_text") or []]
        result = await ctx.app.screen.agent.run(rc, goal=str(args["goal"]), url=str(args["url"]), assertions=specs,
                                                parent_call_id=ctx.call_id)
        payload = result.payload
        ok = bool(payload.assertions) and all(a.passed for a in payload.assertions)
        lines = [f"{'PASS' if a.passed else 'FAIL'} {a.name} {a.detail}".rstrip() for a in payload.assertions]
        return ToolResult(ok=ok, content=f"screen test stopped: {payload.stopped_reason}\n" + "\n".join(lines),
                          evidence=[result.evidence])


def artifact_path(app: AppContext, rel: str) -> Path:
    return app.settings.data_dir / rel
