"""Natural-language agent creation: an LLM drafts an AgentConfig that is validated before saving."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from backend.agents.config import ALL_PERMISSIONS
from backend.agents.service import build_config
from backend.agents.templates import BUILTIN_TEMPLATES, template
from backend.core.errors import Conflict
from backend.core.logging import get_logger
from backend.providers.base import ChatMessage, ProviderError
from backend.providers.router import CallMeta

if TYPE_CHECKING:
    from backend.app_context import AppContext

log = get_logger(__name__)

TemplateName = Literal["planner", "coder", "tester", "critic", "ui-designer", "researcher", "analyst", "chat"]
PermissionName = Literal["fs.read", "fs.write", "fs.search", "terminal.run", "tests.run", "git.read", "git.write",
                         "subagent.spawn", "mcp.call", "web.search", "memory.write"]


class AgentDraft(BaseModel):
    name: str = Field(description="Short kebab-case agent name, e.g. 'api-test-writer'")
    description: str = Field(description="One sentence describing what the agent does")
    template: TemplateName = Field(description="Closest built-in role to start from")
    system_prompt: str = Field(description="Complete system prompt for the agent (role, method, constraints)")
    tool_permissions: list[PermissionName] = Field(description="Least-privilege tool permissions the agent needs")
    category: Literal["heavy_reasoning", "fast_classification", "code_generation", "creative_ui",
                      "research_retrieval"] = Field(description="Task category used to pick its model tier")
    memory_scope: Literal["run", "agent", "workspace", "none"] = "run"
    max_depth: int = Field(default=2, ge=0, le=4)


GENERATOR_PROMPT = (
    "You design autonomous coding agents for the Jevora IDE. Given the user's description, return ONLY a JSON "
    "object describing one agent. Grant the minimum tool permissions needed. Available permissions: "
    + ", ".join(p for p in ALL_PERMISSIONS if p != "screen.control")
    + ". Available templates: " + ", ".join(k for k in BUILTIN_TEMPLATES if k != "screen-tester")
    + ". The system prompt must be specific, actionable and at least three sentences long."
)

_KEYWORDS: list[tuple[str, str]] = [
    (r"\b(test|qa|pytest|coverage|unit test)", "tester"),
    (r"\b(review|audit|critic|lint|security)", "critic"),
    (r"\b(ui|ux|css|design|layout|frontend|component|landing)", "ui-designer"),
    (r"\b(research|summar|compare|sources|literature|docs)", "researcher"),
    (r"\b(analy[sz]|metrics|data|profil|bottleneck|complexity)", "analyst"),
    (r"\b(plan|roadmap|architect|break down)", "planner"),
    (r"\b(chat|explain|teach|answer questions)", "chat"),
]


def heuristic_draft(description: str) -> dict[str, Any]:
    text = description.lower()
    chosen = next((name for pattern, name in _KEYWORDS if re.search(pattern, text)), "coder")
    base = template(chosen)
    words = re.findall(r"[a-z0-9]+", text)[:4] or ["custom"]
    name = "-".join(words)[:60] + "-agent"
    return {
        "name": name, "description": description.strip()[:300], "template": chosen,
        "system_prompt": (f"{base.system_prompt}\n\nSpecialisation requested by the user: {description.strip()}"),
        "tool_permissions": list(base.tool_permissions), "model_binding": base.model_binding.model_dump(),
        "memory_scope": base.memory_scope, "max_depth": min(base.max_depth, 2),
    }


class AgentGenerator:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def draft(self, user_id: str, description: str) -> tuple[dict[str, Any], str]:
        messages = [ChatMessage(role="system", content=GENERATOR_PROMPT),
                    ChatMessage(role="user", content=description)]
        try:
            draft, _ = await self.ctx.router.chat_json(
                CallMeta(user_id=user_id, category="heavy_reasoning"), messages, AgentDraft,
                category="heavy_reasoning")
        except ProviderError as exc:
            log.info("agent generation fell back to heuristics: %s", exc)
            return heuristic_draft(description), "heuristic"
        payload = draft.model_dump(exclude={"category"})
        payload["model_binding"] = {"mode": "auto", "category": draft.category}
        payload["name"] = re.sub(r"[^a-z0-9-]+", "-", draft.name.lower()).strip("-")[:80] or "custom-agent"
        return payload, "llm"

    async def generate(self, user_id: str, description: str, save: bool = True) -> dict[str, Any]:
        payload, source = await self.draft(user_id, description)
        config = build_config(payload)  # schema validation (raises 422 on an invalid draft)
        if not save:
            return {"config": config.public(), "generated_by": source, "saved": False}
        name = config.name
        for attempt in range(2, 12):
            try:
                agent = await self.ctx.agents.create(user_id, {**config.public(), "name": name},
                                                     generated_by=source)
                break
            except Conflict:
                name = f"{config.name[:70]}-{attempt}"
        else:  # pragma: no cover - 10 name clashes in a row
            raise Conflict("could not find a free agent name")
        public = await self.ctx.agents.public(agent)
        return {**public, "saved": True}
