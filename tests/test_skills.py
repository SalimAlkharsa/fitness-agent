"""Skill loading + the read_skill tool.

Skills are folders: `skills/<name>/SKILL.md` with `name`/`description` frontmatter.
"""

from __future__ import annotations

from pathlib import Path

from app.skills import SKILLS_DIR, list_skills, parse_frontmatter, read_skill, skill_index
from app.tools import READ_SKILL, ReadSkillArgs, ToolContext


def _make_skill(root: Path, name: str, description: str, body: str = "body") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"
    )


def test_skill_index_lists_shipped_skills_one_line_each():
    skills = list_skills()
    names = {n for n, _ in skills}
    assert {"parsing-salims-logs", "analysis", "charting"} <= names
    assert all(desc for _n, desc in skills)  # every skill exposes an index blurb

    lines = [ln for ln in skill_index().splitlines() if ln.strip()]
    assert len(lines) == len(skills) and all(ln.startswith("- ") for ln in lines)


def test_skill_files_contain_no_code():
    # Skills are guidance, not code — no fenced blocks in the shipped ones.
    for path in SKILLS_DIR.glob("*/SKILL.md"):
        assert "```" not in path.read_text(), path.parent.name


def test_parse_frontmatter():
    fm = parse_frontmatter("---\nname: foo\ndescription: bar baz\n---\n\n# Foo\n")
    assert fm == {"name": "foo", "description": "bar baz"}
    assert parse_frontmatter("# no frontmatter") == {}


def test_read_skill_returns_content_and_none():
    text = read_skill("parsing-salims-logs")
    assert text and "per side" in text.lower()
    assert read_skill("parsing-salims-logs.md")  # legacy ".md" suffix tolerated
    assert read_skill("does-not-exist") is None


def test_adding_a_fourth_skill_needs_no_code(tmp_path: Path):
    # The tree is globbed, so a new skill folder is picked up with zero code change.
    for name, desc in [("aaa", "first"), ("bbb", "second"), ("ccc", "third"), ("ddd", "fourth")]:
        _make_skill(tmp_path, name, desc)
    got = list_skills(tmp_path)
    assert len(got) == 4 and dict(got)["ddd"] == "fourth"


def test_read_skill_tool_is_registered_read_only_and_returns_content():
    assert READ_SKILL.name == "read_skill" and READ_SKILL.confirm_required is False
    out = READ_SKILL.handler(
        ReadSkillArgs(name="parsing-salims-logs"),
        ToolContext(store=None, kb=[], user_text=""),
    )
    assert out["skill"] == "parsing-salims-logs" and "set-group" in out["content"].lower()


def test_read_skill_blocks_path_traversal(tmp_path: Path):
    _make_skill(tmp_path, "real", "a real skill", body="ok")
    (tmp_path.parent / "secret").mkdir(parents=True, exist_ok=True)
    (tmp_path.parent / "secret" / "SKILL.md").write_text("# secret outside skills")
    assert read_skill("../secret", tmp_path) is None  # traversal must not escape
    assert "ok" in read_skill("real", tmp_path)
