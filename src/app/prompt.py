"""The production parse path: prompt construction + JSON schema + KB resolution.

Both the eval harness and the agent build parse requests through here, so there
is exactly one prompt/schema definition and never a test-only fork. The guidance
embedded here is deliberately minimal — the detailed rules live in
`skills/parsing-salims-logs/SKILL.md` and arrive via `extra_guidance`.
"""

from __future__ import annotations

import json

from app.models import KBEntry, ParseResult

# JSON Schema the model must emit against — the single source of truth.
PARSE_SCHEMA: dict = ParseResult.model_json_schema()

# Compact, always-on parsing rules: the floor that works with no skill loaded.
BASE_PARSE_RULES = """\
You convert one workout message into structured JSON. Rules:
- "on each side" / "on both sides" / "in each side" / trailing "s" like "10s" \
means the weight is PER SIDE. Put it in per_side_lbs; total_lbs = per_side*2 + the \
exercise's base_lbs from the knowledge base.
- Stack/cable machines: the stated number is the TOTAL (total_lbs), leave \
per_side_lbs null. No per-side doubling.
- Bodyweight moves have no weight (both null). Assisted moves (assisted dips/\
pull-ups): put the assist amount in assist_lbs and leave total_lbs null.
- If no weight is stated for an exercise, leave the weights null — never invent one.
- Reps may be fractional ("3.5 real pull ups" -> reps 3.5).
- "AxB" is A sets of B reps; "A sts"/"A sets of B" likewise. "20x85lbs ... 3 sts" \
= 3 sets of 25 reps at 100. A second block joined by "then" is a new set-group \
(grp increments) of the SAME exercise.
- Match each exercise to a canonical name in the knowledge base by its aliases \
before inventing a new one. Put the canonical name in `exercise` and the user's \
words in `exercise_raw`. Negatives (e.g. "negative pull ups") are a SEPARATE \
exercise from the strict version.
- One message may contain several sessions (e.g. a lifting session plus \
basketball): emit one workout per session. A sport like basketball is a workout \
with type "basketball" and an empty sets list.
- Resolve relative dates ("yesterday", "today") against the message timestamp \
given below; output date as YYYY-MM-DD. If no date is stated at all, use the \
message date. Times like "5 to 7 pm" fill start/end as 24h HH:MM.
- Output ONLY JSON matching the schema. No prose, no markdown fences."""


def render_kb(kb_entries: list[KBEntry]) -> str:
    """A compact KB table for the prompt: canonical, equipment, base, aliases."""
    if not kb_entries:
        return "(knowledge base is empty)"
    lines = ["canonical | equipment | base_lbs | aliases"]
    for e in kb_entries:
        aliases = ", ".join(e.aliases) if e.aliases else "-"
        lines.append(f"{e.canonical} | {e.equipment} | {e.base_lbs:g} | {aliases}")
    return "\n".join(lines)


def build_parse_messages(
    message: str,
    kb_entries: list[KBEntry],
    now: str,
    extra_guidance: str | None = None,
) -> list[dict]:
    """Build the chat messages for a parse request.

    `now` is the message timestamp (ISO), used to resolve relative dates.
    `extra_guidance` is where the parsing skill text is injected.
    """
    system_parts = [
        BASE_PARSE_RULES,
        "",
        "Knowledge base (exercise_kb):",
        render_kb(kb_entries),
        "",
        "JSON schema for your output:",
        json.dumps(PARSE_SCHEMA),
    ]
    if extra_guidance:
        system_parts += ["", "Additional parsing guidance:", extra_guidance]
    system = "\n".join(system_parts)
    user = f"Message timestamp: {now}\n\nMessage:\n{message}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def resolve_exercise(name: str, kb_entries: list[KBEntry]) -> str | None:
    """Resolve a raw or canonical exercise name to its canonical KB name.

    Case-insensitive match on canonical name first, then on any alias. Returns
    None if unknown (the confirm card's KB question handles that at runtime).
    """
    if not name:
        return None
    needle = name.strip().lower()
    for e in kb_entries:
        if e.canonical.lower() == needle:
            return e.canonical
    for e in kb_entries:
        if any(a.strip().lower() == needle for a in e.aliases):
            return e.canonical
    return None
