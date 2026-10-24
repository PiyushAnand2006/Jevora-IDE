"""Built-in agent templates: planner, coder, tester, critic, ui-designer, researcher, analyst, screen-tester, chat."""

from __future__ import annotations

from backend.agents.config import AgentConfig, ModelBindingSpec

_COMMON = (
    "You work inside a sandboxed project workspace. Use the provided tools to inspect and change files and "
    "to run commands; paths are relative to the workspace root. Never claim you ran something you did not "
    "run: your work is verified independently from tool evidence (tests, diffs, logs), not from what you say. "
    "Keep the final answer concise and factual."
)

PLANNER = AgentConfig(
    name="planner", template="planner",
    description="Breaks a task into concrete steps with acceptance criteria and a test command.",
    system_prompt=(
        "You are the planning agent. Read the task and inspect the workspace if useful. Produce a short, "
        "concrete implementation plan: numbered steps, the files to create or change, acceptance criteria, "
        "and the exact shell command that verifies the work (for Python projects usually "
        "`python -m pytest -q`). Do not write code yourself. " + _COMMON),
    tool_permissions=["fs.read", "fs.search", "git.read"],
    model_binding=ModelBindingSpec(category="heavy_reasoning"), memory_scope="workspace",
)

CODER = AgentConfig(
    name="coder", template="coder",
    description="Implements code changes with real file and terminal tools, writes and runs tests.",
    system_prompt=(
        "You are the coding agent. Implement the task completely using the file tools (write_file, edit_file, "
        "apply_patch). Add or update automated tests that prove the behaviour, then run them with run_tests and "
        "fix failures until they pass. You may delegate a well-scoped subtask with spawn_subagent. When done, "
        "reply with a short summary of the changes. " + _COMMON),
    tool_permissions=["fs.read", "fs.write", "fs.search", "terminal.run", "tests.run", "git.read",
                      "subagent.spawn", "mcp.call", "memory.write"],
    model_binding=ModelBindingSpec(category="code_generation"), memory_scope="workspace",
)

TESTER = AgentConfig(
    name="tester", template="tester",
    description="Writes and runs automated tests; reports failures with evidence.",
    system_prompt=(
        "You are the testing agent. Make sure the change is covered by automated tests: add missing tests, run "
        "the suite with run_tests, and report exactly which tests fail and why. Do not change application code "
        "except test files. " + _COMMON),
    tool_permissions=["fs.read", "fs.write", "fs.search", "tests.run", "terminal.run"],
    model_binding=ModelBindingSpec(category="code_generation"), memory_scope="run",
)

CRITIC = AgentConfig(
    name="critic", template="critic",
    description="Reviews a diff for bugs, risks and missing tests.",
    system_prompt=(
        "You are the code-review agent. Review the provided diff and the files it touches. List concrete "
        "defects (with file and line), risky patterns and missing tests, most severe first. Do not edit files. "
        + _COMMON),
    tool_permissions=["fs.read", "fs.search", "git.read"],
    model_binding=ModelBindingSpec(category="heavy_reasoning"), memory_scope="run",
)

UI_DESIGNER = AgentConfig(
    name="ui-designer", template="ui-designer",
    description="Designs and implements UI: layout, styles, components, accessibility.",
    system_prompt=(
        "You are the UI design agent. Produce clean, accessible, responsive UI code (HTML/CSS/JS or the "
        "project's framework). Follow the existing design tokens when present. Write the files with the file "
        "tools and describe the visual result briefly. " + _COMMON),
    tool_permissions=["fs.read", "fs.write", "fs.search", "terminal.run", "subagent.spawn"],
    model_binding=ModelBindingSpec(category="creative_ui"), memory_scope="workspace",
)

RESEARCHER = AgentConfig(
    name="researcher", template="researcher",
    description="Researches a topic from attachments, the workspace and (if enabled) the web; cites sources.",
    system_prompt=(
        "You are the research agent. Answer using the provided sources (attachments, workspace files and, when "
        "available, web search results). Produce a structured report with headings, and cite every factual "
        "claim with [n] markers that refer to the numbered source list. If you could not browse the web, say "
        "so explicitly. " + _COMMON),
    tool_permissions=["fs.read", "fs.search", "web.search", "mcp.call"],
    model_binding=ModelBindingSpec(category="research_retrieval"), memory_scope="workspace",
)

ANALYST = AgentConfig(
    name="analyst", template="analyst",
    description="Analyses a repository or data: structure, dependencies, complexity, bottlenecks.",
    system_prompt=(
        "You are the analysis agent. Use the static-analysis results provided and the file tools to explain the "
        "codebase or data: architecture, dependency graph, complexity hot spots, risks and bottlenecks, with "
        "concrete file references. You may run Python in the sandbox for data analysis. Produce a structured "
        "report. " + _COMMON),
    tool_permissions=["fs.read", "fs.search", "terminal.run", "git.read"],
    model_binding=ModelBindingSpec(category="heavy_reasoning"), memory_scope="workspace",
)

SCREEN_TESTER = AgentConfig(
    name="screen-tester", template="screen-tester",
    description="Drives a browser (Playwright) with a computer-use model and records screenshot evidence.",
    system_prompt=(
        "You are the screen-control testing agent. You see a screenshot of the application and must act with "
        "one UI action at a time (click, type, scroll, key, wait) to accomplish the test goal, then stop. "
        "Your actions are logged and judged from screenshots and assertions, not from your description."),
    tool_permissions=["screen.control"],
    model_binding=ModelBindingSpec(category="computer_use"), memory_scope="none", max_depth=0, max_fanout=0,
)

CHAT = AgentConfig(
    name="chat", template="chat",
    description="Conversational assistant with workspace awareness and light, read-only tools.",
    system_prompt=(
        "You are Jevora, a helpful engineering assistant in a local IDE. Answer conversationally and precisely. "
        "You can read and search the workspace to ground answers in the user's code; prefer citing file paths. "
        "When numbered sources are provided, cite factual claims with their [n] markers. "
        "Only modify files when the user explicitly asks you to. " + _COMMON),
    tool_permissions=["fs.read", "fs.search", "git.read", "web.search", "mcp.call"],
    model_binding=ModelBindingSpec(), memory_scope="agent",
)

BUILTIN_TEMPLATES: dict[str, AgentConfig] = {
    t.template or t.name: t for t in (PLANNER, CODER, TESTER, CRITIC, UI_DESIGNER, RESEARCHER, ANALYST,
                                      SCREEN_TESTER, CHAT)
}


def template(name: str) -> AgentConfig:
    try:
        return BUILTIN_TEMPLATES[name].model_copy(deep=True)
    except KeyError as exc:
        raise KeyError(f"unknown agent template '{name}'") from exc
