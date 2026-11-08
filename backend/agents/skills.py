"""Agent skills: SKILL.md packages the agent loads on demand (ZCode/Claude convention).

Locations:
  * ``<data_dir>/skills/<name>/SKILL.md``            — installed globally
  * ``<workspace>/.jevora/skills/<name>/SKILL.md``   — project-local (overrides global)

A skill is YAML frontmatter (``name``, ``description``) plus a markdown body of
instructions. The body may reference helper scripts next to SKILL.md; the agent
runs those with its terminal tool. Loading a skill injects its instructions so
the agent follows them for the current task.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.core.logging import get_logger

log = get_logger(__name__)

SKILL_FILE = "SKILL.md"
PROJECT_SKILLS_DIR = Path(".jevora") / "skills"
MAX_SKILL_BODY_CHARS = 24_000


@dataclass
class SkillInfo:
    name: str
    description: str
    source: str  # "global" | "project"
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal frontmatter: lines between the first two ``---`` fences, ``key: value``."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        key, _, value = lines[index].partition(":")
        if key.strip().lower() in ("name", "description"):
            meta[key.strip().lower()] = value.strip().strip("\"'")
        index += 1
    if index >= len(lines):
        return {}, text  # no closing fence — treat as no frontmatter
    return meta, "\n".join(lines[index + 1:]).strip()


def _parse_skill(path: Path, source: str) -> SkillInfo | None:
    meta, body = _parse_skill_text(path)
    name = (meta.get("name") or path.parent.name).strip().lower().replace(" ", "-")
    description = meta.get("description") or ""
    if not description:
        for line in body.splitlines():
            if line.strip() and not line.startswith("#"):
                description = line.strip()
                break
    if not name.replace("-", "").replace("_", "").isalnum():
        return None
    return SkillInfo(name=name[:64], description=description[:300], source=source, path=path)


def discover_skills(workspace: Path, global_dir: Path) -> list[SkillInfo]:
    """Installed skills; project skills shadow global ones with the same name."""
    found: dict[str, SkillInfo] = {}
    for source, base in (("global", global_dir), ("project", workspace / PROJECT_SKILLS_DIR)):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            skill_md = entry / SKILL_FILE
            if not skill_md.is_file():
                continue
            info = _parse_skill(skill_md, source)
            if info is not None:
                found[info.name] = info
    return sorted(found.values(), key=lambda s: s.name)


def read_skill(info: SkillInfo, args: str | None = None) -> str:
    """The skill's instructions, ready to follow; scripts run from its folder."""
    _, body = _parse_skill_text(info.path)
    body = body[:MAX_SKILL_BODY_CHARS]
    header = f"Skill '{info.name}' ({info.source})"
    if args:
        header += f" — arguments: {args}"
    return (f"{header}\n\nFollow these instructions exactly for the current task:\n\n{body}\n\n"
            f"(Skill files live in {info.path.parent} — run any helper scripts from there "
            f"with your terminal tool.)")


def _parse_skill_text(path: Path) -> tuple[dict[str, str], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    return _parse_frontmatter(text)
