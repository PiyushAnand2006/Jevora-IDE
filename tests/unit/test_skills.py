"""Skills: discovery, frontmatter parsing, shadowing, and the use_skill tool registration."""

import pytest

from backend.agents.skills import discover_skills, read_skill
from backend.agents.tools.skills import UseSkill

SKILL_BODY = """---
name: pdf-report
description: Build a polished PDF report from data using reportlab.
---

1. Read the source data.
2. Generate the PDF with reportlab into reports/.
"""


def _write_skill(base, name, body=SKILL_BODY):
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def test_discover_skills_global_and_project(tmp_path) -> None:
    global_dir = tmp_path / "data" / "skills"
    _write_skill(global_dir, "pdf-report")
    project = tmp_path / "ws"
    _write_skill(project / ".jevora" / "skills", "repo-checklist",
                 "---\nname: repo-checklist\ndescription: A repo hygiene checklist.\n---\nCheck things.")

    skills = discover_skills(project, global_dir)
    names = {s.name: s for s in skills}
    assert set(names) == {"pdf-report", "repo-checklist"}
    assert names["pdf-report"].source == "global"
    assert names["repo-checklist"].source == "project"


def test_discover_skills_project_overrides_global(tmp_path) -> None:
    global_dir = tmp_path / "data" / "skills"
    _write_skill(global_dir, "shared", "---\nname: shared\ndescription: global one\n---\nglobal body")
    project = tmp_path / "ws"
    _write_skill(project / ".jevora" / "skills", "shared",
                 "---\nname: shared\ndescription: project one\n---\nproject body")

    skills = discover_skills(project, global_dir)
    assert len(skills) == 1 and skills[0].source == "project" and skills[0].description == "project one"


def test_read_skill_includes_body_and_args(tmp_path) -> None:
    d = _write_skill(tmp_path / "data" / "skills", "pdf-report")
    info = discover_skills(tmp_path / "ws", tmp_path / "data" / "skills")[0]
    text = read_skill(info, "quarterly data")
    assert "Skill 'pdf-report'" in text and "quarterly data" in text
    assert "reportlab" in text and str(d) in text


def test_discover_skills_skips_junk(tmp_path) -> None:
    global_dir = tmp_path / "data" / "skills"
    (global_dir / "not-a-skill").mkdir(parents=True)  # no SKILL.md
    _write_skill(global_dir, "bad name!!", "no frontmatter, invalid name\n")
    assert discover_skills(tmp_path / "ws", global_dir) == []


@pytest.mark.asyncio
async def test_use_skill_registered_and_loads(tmp_path) -> None:
    from backend.agents.tools import build_registry

    registry = build_registry()
    assert "use_skill" in registry.names()
    tool = registry._tools["use_skill"]
    assert isinstance(tool, UseSkill) and tool.permission == "skills.use"
