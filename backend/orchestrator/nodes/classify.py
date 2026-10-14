"""router/classify node: fast-tier task triage → Decision Engine route_model + assign_agent."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from backend.agents.tools.terminal import detect_test_command
from backend.core.errors import NotFound
from backend.orchestrator.nodes.common import ctx_for, load_attachments, set_status
from backend.providers.base import ChatMessage, NoProviderConfigured, ProviderError
from backend.providers.router import CallMeta

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


class TaskSpec(BaseModel):
    title: str = Field(description="A short title for the task (max 8 words)")
    intent: Literal["build", "fix", "design", "research", "analysis", "question", "other"]
    needs_code_changes: bool = Field(description="Does completing the task require changing files?")
    needs_tests: bool = Field(description="Should automated tests verify the result?")
    ui_work: bool = Field(description="Is the task mainly visual/UI design?")
    keywords: list[str] = Field(default_factory=list, description="Up to 8 technical keywords")
    risk: Literal["low", "medium", "high"] = "low"


TRIAGE_PROMPT = ("You triage tasks for an agentic IDE. Read the user's request and return ONLY a JSON object with "
                 "the fields: title, intent, needs_code_changes, needs_tests, ui_work, keywords, risk.")

WORKER_NODES = {"coder": "coder", "ui-designer": "ui_designer", "chat": "chat", "researcher": "researcher",
                "analyst": "analyst", "tester": "coder", "critic": "coder", "planner": "coder",
                "screen-tester": "coder"}


def heuristic_spec(task: str, mode: str) -> dict[str, Any]:
    text = task.lower()
    ui = bool(re.search(r"\b(ui|ux|css|landing|layout|design|style|page|component|frontend)\b", text))
    code = mode == "build" or bool(re.search(r"\b(create|build|implement|fix|write|add|refactor)\b", text))
    return {"title": task.strip().splitlines()[0][:60] if task.strip() else "task",
            "intent": {"build": "build", "research": "research", "analysis": "analysis"}.get(mode, "question"),
            "needs_code_changes": code, "needs_tests": code and not ui, "ui_work": ui,
            "keywords": re.findall(r"[a-z][a-z0-9+#.-]{2,}", text)[:8], "risk": "low"}


async def classify(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    rc = ctx_for(o, state)
    await set_status(o, state, "planning")
    mode, task, command = state["mode"], state["task"], state.get("command")
    updates: dict[str, Any] = {}
    # Fail fast with a clear message when no model can be reached at all.
    try:
        await o.app.router.resolve(rc.user_id, rc.model_override or "auto", "code_generation")
    except NoProviderConfigured as exc:
        return {"final_status": "failed", "error": str(exc), "worker_node": "finalize"}
    except NotFound as exc:
        rc.recorder.emit("error", {"source": "router", "message": f"{exc.message}; using Auto routing instead"})
        rc.model_override = None
        updates["model_override"] = None

    spec = heuristic_spec(task, mode)
    if mode != "chat" and command not in ("/run",):
        call_id = await rc.recorder.start_call(role="router", agent_name="router", task=task[:4000],
                                               category="fast_classification")
        try:
            messages = [ChatMessage(role="system", content=TRIAGE_PROMPT), ChatMessage(role="user", content=task)]
            parsed, response = await o.app.router.chat_json(
                CallMeta(user_id=rc.user_id, run_id=rc.run_id, call_id=call_id), messages, TaskSpec,
                category="fast_classification" if rc.flags.get("dynamic_model_router", True) else "code_generation",
                model=rc.model_override or "auto")
            await rc.recorder.set_call_model(call_id, response.model, response.provider_id)
            await rc.recorder.add_call_usage(call_id, response.usage.total, response.cost_usd)
            await rc.recorder.add_usage(response.usage.total, response.cost_usd)
            rc.scope.budget.add(response.usage.total, response.cost_usd)
            spec = parsed.model_dump()
            await rc.recorder.finish_call(call_id, status="done", output=parsed.model_dump_json())
        except ProviderError as exc:
            await rc.recorder.finish_call(call_id, status="failed", error=f"triage fell back to heuristics: {exc}")

    attachments = await load_attachments(o, state)
    route = await o.app.decisions.route_model(
        {**spec, "task": task[:2000], "mode": mode, "command": command,
         "has_images": any(a["kind"] == "image" for a in attachments)}, recorder=rc.recorder)
    category = route.result.choice
    assign = await o.app.decisions.assign_agent({"mode": mode, "category": category, "command": command},
                                                recorder=rc.recorder)
    assignee = assign.result.choice
    worker = WORKER_NODES.get(assignee, "coder")
    if mode == "build" and category == "creative_ui" and command not in ("/fix",):
        worker = "ui_designer"
    if state.get("agent_id"):
        worker = "custom_agent"
    flags = state.get("flags", {})
    tests_present = detect_test_command(rc.workspace) is not None
    requires_changes = (mode == "build" and bool(spec.get("needs_code_changes", True))
                        and command not in ("/test", "/review", "/plan", "/run"))
    requires_tests = mode == "build" and (bool(spec.get("needs_tests")) or tests_present) and (
        flags.get("iterative_tester", True) or tests_present)
    if command == "/test":
        # A URL means a screen test; unit tests are then required only if the project has a framework.
        has_url = re.search(r"https?://", state.get("command_arg") or task) is not None
        requires_tests = tests_present or not has_url
    updates.update({"spec": spec, "category": category, "assignee": assignee, "worker_node": worker,
                    "requires_tests": requires_tests, "requires_changes": requires_changes})
    return updates
