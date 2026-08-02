"""Skill loading.

Skills follow the Anthropic skill convention: one folder per skill under
`skills/<name>/SKILL.md`, each with YAML frontmatter (`name`, `description`).
They load on demand via the `read_skill` tool so the system prompt carries only a
one-line index (`name: description`), keeping context small on a free model. The
tree is globbed, so adding a skill is just dropping in a new SKILL.md.
"""

from __future__ import annotations

import re
from pathlib import Path

# skills/ sits at the repo root in the dev tree (src/app → up 3), but next to the
# app package in the deployed bundle (app → up 2), so try both. The skills are
# load-bearing — the free-tier model parses poorly without the parsing skill.
_SKILLS_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "skills",
    Path(__file__).resolve().parents[1] / "skills",
]
SKILLS_DIR = next((p for p in _SKILLS_CANDIDATES if p.is_dir()), _SKILLS_CANDIDATES[0])
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the leading `--- ... ---` YAML block into a flat str→str dict.

    Handles only single-line `key: value` pairs, which is all a SKILL.md header
    needs — and avoids a YAML dependency.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def _skill_paths(skills_dir: Path) -> list[Path]:
    if not skills_dir.is_dir():
        return []
    return sorted(skills_dir.glob("*/SKILL.md"))


def _meta(path: Path) -> tuple[str, str]:
    """(name, description) for a SKILL.md — frontmatter first, folder name as
    the name fallback, first prose line as the description fallback."""
    text = path.read_text()
    fm = parse_frontmatter(text)
    name = fm.get("name") or path.parent.name
    desc = fm.get("description") or _first_prose_line(text)
    return name, " ".join(desc.split())


def _first_prose_line(text: str) -> str:
    body = _FRONTMATTER_RE.sub("", text, count=1)
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ">")):
            return line
    return ""


def list_skills(skills_dir: Path = SKILLS_DIR) -> list[tuple[str, str]]:
    """(name, description) for every skill in the directory."""
    return [_meta(path) for path in _skill_paths(skills_dir)]


def skill_index(skills_dir: Path = SKILLS_DIR) -> str:
    """One line per skill for the system prompt."""
    return "\n".join(f"- {name}: {desc}" for name, desc in list_skills(skills_dir))


def read_skill(name: str, skills_dir: Path = SKILLS_DIR) -> str | None:
    """Full text of a skill by name → `skills/<name>/SKILL.md`.

    Only a bare skill name resolves: path separators are stripped so a
    model-supplied name can't traverse out of the skills directory.
    """
    stem = name[:-3] if name.endswith(".md") else name  # tolerate a trailing ".md"
    stem = Path(stem).name  # no directory components — no traversal
    if not stem:
        return None
    path = skills_dir / stem / "SKILL.md"
    if path.is_file():
        return path.read_text()
    return None
