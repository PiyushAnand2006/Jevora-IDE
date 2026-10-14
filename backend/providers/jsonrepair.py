"""Best-effort repair of malformed JSON emitted by LLMs."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_PY_LITERALS = (("True", "true"), ("False", "false"), ("None", "null"))


def _balanced_candidates(text: str) -> list[str]:
    """Return every balanced {...} / [...] substring, outermost first."""
    found: list[str] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth, in_str, escape = 0, False, False
            for idx in range(start, len(text)):
                ch = text[idx]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        found.append(text[start: idx + 1])
                        break
            else:
                # Unterminated: keep the tail so we can try closing it.
                found.append(text[start:])
            start = text.find(opener, start + 1)
            if found:
                break
    found.sort(key=len, reverse=True)
    return found


def _escape_newlines_in_strings(text: str) -> str:
    out, in_str, escape = [], False, False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            elif ch == "\n":
                out.append("\\n")
                continue
            elif ch == "\t":
                out.append("\\t")
                continue
        elif ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _close_unterminated(text: str) -> str:
    stack: list[str] = []
    in_str, escape = False, False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        text += '"'
    return text + "".join(reversed(stack))


def _fixups(candidate: str) -> list[str]:
    variants = [candidate]
    fixed = candidate.replace("“", '"').replace("”", '"').replace("’", "'")
    fixed = _TRAILING_COMMA_RE.sub(r"\1", fixed)
    fixed = _escape_newlines_in_strings(fixed)
    variants.append(fixed)
    py = fixed
    for src, dst in _PY_LITERALS:
        py = re.sub(rf"\b{src}\b", dst, py)
    variants.append(py)
    if "'" in py and '"' not in py:
        variants.append(py.replace("'", '"'))
    variants.append(_TRAILING_COMMA_RE.sub(r"\1", _close_unterminated(py)))
    return variants


def repair_json(text: str) -> Any:
    """Parse JSON from LLM output, tolerating fences, prose, and common syntax slips.

    Raises ValueError if nothing parseable can be recovered.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    sources = [m.group(1).strip() for m in _FENCE_RE.finditer(text)] + [text]
    for source in sources:
        for candidate in [source, *_balanced_candidates(source)]:
            for variant in _fixups(candidate):
                try:
                    return json.loads(variant)
                except (json.JSONDecodeError, TypeError):
                    continue
    raise ValueError("could not recover JSON from model output")
