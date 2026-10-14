"""Deterministic repository analysis for Analysis mode (feeds the analyst agent and is evidence)."""

from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from backend.agents.workspace_state import iter_files
from backend.core.paths import detect_language, is_binary

_BRANCH_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.BoolOp,
                 ast.IfExp, ast.ExceptHandler, ast.comprehension, ast.Match)


def cyclomatic(node: ast.AST) -> int:
    return 1 + sum(1 for child in ast.walk(node) if isinstance(child, _BRANCH_NODES))


def analyze_python(rel: str, text: str) -> dict[str, Any]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {"path": rel, "syntax_error": f"line {exc.lineno}: {exc.msg}", "functions": [], "imports": []}
    functions: list[dict[str, Any]] = []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1
            functions.append({"name": node.name, "line": node.lineno, "complexity": cyclomatic(node),
                              "length": length})
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            imports.add("." + (node.module or ""))
    classes = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    return {"path": rel, "functions": functions, "classes": classes, "imports": sorted(imports)}


def analyze_workspace(root: Path, max_files: int = 3000) -> dict[str, Any]:
    languages: Counter[str] = Counter()
    lines_by_lang: Counter[str] = Counter()
    largest: list[tuple[int, str]] = []
    todo = 0
    py_reports: list[dict[str, Any]] = []
    total_lines = 0
    files = 0
    for rel, st in iter_files(root, max_files):
        files += 1
        lang = detect_language(rel)
        languages[lang] += 1
        if st.st_size > 1_500_000:
            continue
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        if is_binary(data[:8192]):
            continue
        text = data.decode("utf-8", "replace")
        n = text.count("\n") + 1
        total_lines += n
        lines_by_lang[lang] += n
        largest.append((n, rel))
        todo += len(re.findall(r"\b(TODO|FIXME|HACK|XXX)\b", text))
        if lang == "python":
            py_reports.append(analyze_python(rel, text))
    functions = [{**f, "path": r["path"]} for r in py_reports for f in r.get("functions", [])]
    hotspots = sorted(functions, key=lambda f: (f["complexity"], f["length"]), reverse=True)[:15]
    local_modules = {Path(r["path"]).stem for r in py_reports}
    graph: dict[str, list[str]] = defaultdict(list)
    external: Counter[str] = Counter()
    for report in py_reports:
        for imp in report.get("imports", []):
            if imp.lstrip(".") in local_modules or imp.startswith("."):
                graph[report["path"]].append(imp)
            else:
                external[imp] += 1
    syntax_errors = [{"path": r["path"], "error": r["syntax_error"]} for r in py_reports if r.get("syntax_error")]
    avg_complexity = round(sum(f["complexity"] for f in functions) / len(functions), 2) if functions else 0.0
    findings = len([f for f in functions if f["complexity"] >= 10]) + len(syntax_errors)
    return {
        "files": files, "lines": total_lines, "languages": dict(languages.most_common()),
        "lines_by_language": dict(lines_by_lang.most_common()),
        "largest_files": [{"path": p, "lines": n} for n, p in sorted(largest, reverse=True)[:10]],
        "python": {"modules": len(py_reports), "functions": len(functions), "avg_complexity": avg_complexity,
                   "complexity_hotspots": hotspots, "syntax_errors": syntax_errors,
                   "internal_imports": {k: sorted(set(v)) for k, v in sorted(graph.items())[:100]},
                   "external_dependencies": dict(external.most_common(30))},
        "todo_markers": todo, "findings": findings,
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = [f"Files: {report['files']} · Lines: {report['lines']} · TODO/FIXME markers: {report['todo_markers']}",
             "Languages: " + ", ".join(f"{k} ({v})" for k, v in list(report["languages"].items())[:8])]
    py = report["python"]
    if py["modules"]:
        lines.append(f"Python: {py['modules']} modules, {py['functions']} functions, average cyclomatic "
                     f"complexity {py['avg_complexity']}")
        if py["complexity_hotspots"]:
            lines.append("Complexity hot spots: " + "; ".join(
                f"{h['path']}:{h['line']} {h['name']} (cc={h['complexity']}, {h['length']} lines)"
                for h in py["complexity_hotspots"][:8]))
        if py["external_dependencies"]:
            lines.append("External dependencies: " + ", ".join(f"{k}×{v}" for k, v in
                                                               list(py["external_dependencies"].items())[:15]))
        if py["syntax_errors"]:
            lines.append("Syntax errors: " + "; ".join(f"{e['path']} {e['error']}" for e in py["syntax_errors"]))
    if report["largest_files"]:
        lines.append("Largest files: " + ", ".join(f"{f['path']} ({f['lines']})" for f in report["largest_files"][:6]))
    return "\n".join(lines)
