"""Skill tool: the agent loads an installed skill's SKILL.md instructions on demand."""

from __future__ import annotations

from typing import Any

from backend.agents.skills import discover_skills, read_skill
from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema


class UseSkill(Tool):
    name = "use_skill"
    description = ("Load an installed skill's instructions (SKILL.md) into context and follow them for the "
                   "current task. Call this whenever the task matches an installed skill.")
    permission = "skills.use"
    parameters = schema({"name": {"type": "string", "description": "Skill name (folder or frontmatter name)"},
                         "args": {"type": "string", "description": "Optional arguments for the skill"}},
                        ["name"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        skills = discover_skills(ctx.workspace, ctx.app.settings.data_dir / "skills")
        wanted = str(args["name"]).strip().lower().replace(" ", "-")
        match = next((s for s in skills if s.name == wanted), None)
        if match is None:
            installed = ", ".join(s.name for s in skills) or "none"
            return ToolResult(ok=False,
                              content=f"error: no skill named '{wanted}' is installed (installed: {installed})")
        content = read_skill(match, str(args.get("args") or "") or None)
        ctx.emit("skill.used", {"name": match.name, "source": match.source})
        return ToolResult(content=content[:24_500])


SKILL_TOOLS: list[Tool] = [UseSkill()]
