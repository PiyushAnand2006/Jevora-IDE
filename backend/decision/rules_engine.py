"""RulesEngine: deterministic, evidence-based decisions. Always available; the cold-start engine.

It reads exit codes, test counts, diffs, lint errors, screenshot assertions and timeouts from
signed evidence plus structured run features. It never reads agent narrative.
"""

from __future__ import annotations

import re
from typing import Any

from backend.decision.interface import DecisionEngine
from backend.decision.schemas import (
    DecisionQuestion,
    DecisionResult,
    DiffPayload,
    Evidence,
    ExecutionLogPayload,
    LintPayload,
    ScreenshotPayload,
    TestResultPayload,
)

_ROUTING_KEYWORDS: dict[str, list[str]] = {
    "creative_ui": [r"\bui\b", r"\bux\b", "design", "layout", "css", "style", "landing page", "frontend",
                    "component", "responsive", "theme", "animation", "figma", "page", "button", "color"],
    "research_retrieval": ["research", "find out", "compare", "summar", "sources", "cite", "literature", "docs",
                           "documentation", "what is", "who", "latest", "survey", "explain the difference"],
    "fast_classification": ["classify", "categor", "label", "yes or no", "true or false", "triage", "route",
                            "sentiment", "detect language", "which one", "tag"],
    "heavy_reasoning": ["architect", "analy", "plan", "prove", "reason", "complex", "optimi", "trade-off",
                        "tradeoff", "strategy", "algorithm", "performance", "bottleneck", "security review",
                        "root cause", "debug"],
    "code_generation": ["implement", "code", "function", "class", "bug", "fix", "test", "build", "script", "api",
                        "endpoint", "refactor", "flask", "fastapi", "python", "javascript", "typescript", "cli",
                        "write a", "create a", "app", "program", "module", "pytest", "hello-world"],
}
_MODE_PRIOR = {"build": "code_generation", "chat": "fast_classification", "research": "research_retrieval",
               "analysis": "heavy_reasoning"}


def distribution(options: list[str], choice: str, confidence: float,
                 weights: dict[str, float] | None = None) -> dict[str, float]:
    rest = max(0.0, 1.0 - confidence)
    others = [o for o in options if o != choice]
    weights = {o: (weights or {}).get(o, 1.0) for o in others}
    total = sum(weights.values()) or 1.0
    scores = {o: round(rest * w / total, 4) for o, w in weights.items()}
    scores[choice] = round(confidence, 4)
    return {o: scores[o] for o in options}


def _result(question: DecisionQuestion, choice: str, confidence: float, reason: str,
            weights: dict[str, float] | None = None) -> DecisionResult:
    return DecisionResult(type=question.type, choice=choice, confidence=round(confidence, 4),
                          scores_per_option=distribution(question.options, choice, confidence, weights),
                          engine="rules", reason=reason)


class RulesEngine(DecisionEngine):
    name = "rules"

    async def _decide(self, question: DecisionQuestion, evidence: Evidence | None) -> DecisionResult:
        handler = {
            "verification": self._verify, "routing": self._route, "next_action": self._next_action,
            "assignment": self._assign, "retry": self._retry, "completion": self._completion,
        }[question.type]
        return handler(question, evidence or Evidence())

    # ------------------------------------------------------------------ verification
    def _verify(self, q: DecisionQuestion, evidence: Evidence) -> DecisionResult:
        s = q.state
        iteration = int(s.get("iteration", 0))
        ceiling = int(s.get("max_iterations", 6))
        same_failures = int(s.get("same_agent_failures", 0))
        requires_tests = bool(s.get("requires_tests", True))
        requires_changes = bool(s.get("requires_changes", True))
        at_ceiling = iteration + 1 >= ceiling

        def not_done(reason: str, confidence: float) -> DecisionResult:
            if at_ceiling:
                return _result(q, "escalate", 0.9, f"{reason}; iteration ceiling reached ({iteration + 1}/{ceiling})")
            if same_failures >= 2:
                return _result(q, "reassign_new_agent", max(0.7, confidence - 0.05),
                               f"{reason}; same agent failed {same_failures} times")
            return _result(q, "restart_same_agent", confidence, reason,
                           {"reassign_new_agent": 2.0, "escalate": 1.0, "confirm_done": 0.2})

        if s.get("agent_status") == "failed":
            return not_done("agent run failed (runtime error, budget or provider failure)", 0.8)

        tests: TestResultPayload | None = evidence.latest("test_result")
        diff: DiffPayload | None = evidence.latest("diff")
        lint: LintPayload | None = evidence.latest("lint")
        screen: ScreenshotPayload | None = evidence.latest("screenshot")
        last_exec: ExecutionLogPayload | None = evidence.latest("execution_log")

        if screen is not None:
            failed = [a.name for a in screen.assertions if not a.passed]
            if failed or screen.console_errors:
                detail = f"failed assertions: {', '.join(failed)}" if failed else "console errors on page"
                return not_done(f"screen test failed ({detail})", 0.85)
            if not screen.assertions and not screen.screenshots:
                return not_done("screen test produced no screenshots or assertions", 0.75)

        if requires_tests:
            if tests is None:
                return not_done("no test evidence: tests were not run", 0.78)
            if tests.timed_out:
                return not_done("test run timed out", 0.8)
            if tests.failed or tests.errors or tests.exit_code not in (0,):
                if tests.exit_code == 5 and not tests.failed and not tests.errors:
                    return not_done("no tests were collected", 0.82)
                return not_done(f"tests failing: {tests.failed} failed, {tests.errors} errors "
                                f"(exit {tests.exit_code})", 0.88)
            if tests.passed == 0:
                return not_done("no passing tests", 0.82)
        if requires_changes and (diff is None or diff.files_changed == 0):
            return not_done("empty diff: no workspace changes were produced", 0.8)
        if not requires_tests and last_exec is not None and last_exec.exit_code not in (0, None):
            return not_done(f"last command failed (exit {last_exec.exit_code})", 0.72)

        confidence = 0.93 if (tests is not None or screen is not None) else 0.78
        reason = "evidence consistent with completion"
        if tests is not None:
            reason = f"{tests.passed} tests passed, 0 failed"
        if lint is not None and lint.errors:
            confidence -= 0.12
            reason += f"; {lint.errors} lint errors"
        if screen is not None:
            reason += f"; {len(screen.assertions)} screen assertions passed"
        return _result(q, "confirm_done", confidence, reason,
                       {"restart_same_agent": 2.0, "reassign_new_agent": 1.0, "escalate": 1.0})

    # ------------------------------------------------------------------ routing
    def _route(self, q: DecisionQuestion, _: Evidence) -> DecisionResult:
        text = " ".join(str(q.state.get(k, "")) for k in ("task", "title", "intent", "keywords")).lower()
        scores = dict.fromkeys(q.options, 0.0)
        for category, words in _ROUTING_KEYWORDS.items():
            for word in words:
                if re.search(word, text):
                    scores[category] = scores.get(category, 0.0) + 1.0
        prior = _MODE_PRIOR.get(str(q.state.get("mode", "")), "code_generation")
        scores[prior] = scores.get(prior, 0.0) + 1.5
        if q.state.get("has_images"):
            scores["creative_ui"] += 0.5
        if q.state.get("command") in ("/design",):
            scores["creative_ui"] += 3
        if q.state.get("command") in ("/research",):
            scores["research_retrieval"] += 3
        total = sum(scores.values()) or 1.0
        probs = {k: v / total for k, v in scores.items()}
        choice = max(probs, key=lambda k: (probs[k], k == prior))
        top = probs[choice]
        second = sorted(probs.values(), reverse=True)[1] if len(probs) > 1 else 0.0
        confidence = min(0.95, 0.5 + (top - second) * 1.5 + top * 0.3)
        return DecisionResult(type="routing", choice=choice, confidence=round(confidence, 4),
                              scores_per_option={k: round(v, 4) for k, v in probs.items()}, engine="rules",
                              reason=f"keyword scores {({k: round(v, 1) for k, v in scores.items()})}")

    # ------------------------------------------------------------------ next action / assignment / retry
    def _next_action(self, q: DecisionQuestion, _: Evidence) -> DecisionResult:
        s = q.state
        mode = s.get("mode", "build")
        if s.get("iteration", 0) >= s.get("max_iterations", 6):
            return _result(q, "escalate", 0.9, "iteration ceiling reached")
        if mode == "build":
            if not s.get("has_plan"):
                return _result(q, "plan", 0.92, "no plan yet")
            if not s.get("has_changes"):
                return _result(q, "design_ui" if s.get("category") == "creative_ui" else "code", 0.9,
                               "plan exists, no changes yet")
            if not s.get("tests_run") and s.get("tester_enabled", True):
                return _result(q, "test", 0.88, "changes not tested yet")
            if not s.get("verified"):
                return _result(q, "verify", 0.9, "evidence collected; verify")
            return _result(q, "finalize", 0.9, "verified")
        if mode == "research":
            return _result(q, "verify" if s.get("has_output") else "research", 0.88, "research flow")
        if mode == "analysis":
            return _result(q, "verify" if s.get("has_output") else "analyze", 0.88, "analysis flow")
        return _result(q, "finalize" if s.get("has_output") else "code", 0.85, "chat flow")

    def _assign(self, q: DecisionQuestion, _: Evidence) -> DecisionResult:
        s = q.state
        command = s.get("command")
        by_command = {"/test": "tester", "/review": "critic", "/design": "ui-designer", "/plan": "planner",
                      "/research": "researcher", "/explain": "chat", "/analyze": "analyst", "/fix": "coder"}
        if command in by_command:
            return _result(q, by_command[command], 0.95, f"command {command}")
        mode = s.get("mode")
        if mode == "research":
            return _result(q, "researcher", 0.93, "research mode")
        if mode == "analysis":
            return _result(q, "analyst", 0.93, "analysis mode")
        if mode == "chat":
            return _result(q, "chat", 0.93, "chat mode")
        category = s.get("category")
        if category == "creative_ui":
            return _result(q, "ui-designer", 0.85, "UI task in build mode")
        return _result(q, "coder", 0.88, "build mode")

    def _retry(self, q: DecisionQuestion, _: Evidence) -> DecisionResult:
        s = q.state
        kind = s.get("error_kind", "unknown")
        attempts = int(s.get("attempts", 0))
        cap = int(s.get("retry_cap", 3))
        if kind in ("sandbox_unavailable", "no_provider", "budget"):
            return _result(q, "give_up", 0.9, f"{kind} cannot be fixed by retrying")
        if attempts >= cap:
            return _result(q, "escalate", 0.88, f"retry cap {cap} reached")
        if kind in ("tool_loop", "invalid_output"):
            return _result(q, "retry_stronger_model", 0.8, f"{kind}: try a stronger model")
        return _result(q, "retry_same", 0.8, f"transient {kind}; retry")

    # ------------------------------------------------------------------ completion (research/analysis/chat)
    def _completion(self, q: DecisionQuestion, evidence: Evidence) -> DecisionResult:
        s = q.state
        if not s.get("has_output"):
            return _result(q, "incomplete", 0.9, "no output produced")
        mode = s.get("mode")
        if mode == "research":
            cites = evidence.latest("citations")
            if cites is None:
                return _result(q, "incomplete", 0.7, "no citation evidence")
            if cites.sources_available and cites.sources_cited == 0:
                return _result(q, "incomplete", 0.8, "sources were available but none were cited")
            if cites.unresolved_citations:
                return _result(q, "incomplete", 0.78, f"{cites.unresolved_citations} citations do not resolve")
            return _result(q, "complete", 0.88, f"{cites.sources_cited}/{cites.sources_available} sources cited")
        if mode == "analysis":
            if evidence.latest("static_analysis") is None:
                return _result(q, "incomplete", 0.7, "no static-analysis evidence")
            return _result(q, "complete", 0.86, "report grounded in static analysis")
        return _result(q, "complete", 0.85, "response produced")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": "deterministic", "validated": True}
