"""Pydantic domain models — the single source of truth.

`.model_json_schema()` is embedded in the agent prompt (so the LLM emits matching
JSON) *and* validates every tool call. One definition, both jobs.

Schema spec: DESIGN.md ("Sheet schema"). Parsing quirks: evals/golden/README.md.
"""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app import SCHEMA_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "Equipment",
    "Workout",
    "SetGroup",
    "KBEntry",
    "InboxItem",
    "ParsedWorkout",
    "ParsedSetGroup",
    "ParseResult",
    "compute_total",
]


class Equipment(StrEnum):
    """How an exercise is loaded — a property of the exercise (so it lives in the
    KB, not on an individual set). Drives `compute_total`."""

    barbell = "barbell"
    smith = "smith"
    hex = "hex"
    stack = "stack"
    cable = "cable"
    bodyweight = "bodyweight"
    assisted = "assisted"


# Equipment whose stated weight is *per side* and adds a base implement weight.
_PER_SIDE_PLUS_BASE = {Equipment.barbell, Equipment.smith, Equipment.hex}
# Equipment whose stated number is the total, verbatim (stack/cable machines).
_STACK_LIKE = {Equipment.stack, Equipment.cable}


class Workout(BaseModel):
    """One session. Basketball etc. is a Workout with type=basketball and no sets."""

    workout_id: str = Field(description="e.g. W0031, allocated at commit time")
    date: str = Field(description="workout date (YYYY-MM-DD), not the message date")
    type: str = Field(description="strength / basketball / …")
    start: str | None = Field(default=None, description="HH:MM, optional")
    end: str | None = Field(default=None, description="HH:MM, optional")
    notes: str | None = None
    raw_text: str = Field(description="full original message — audit trail")
    logged_at: str = Field(description="ISO timestamp the row was written")


class SetGroup(BaseModel):
    """One set-group. '3x12 then 3x15' of one exercise = two rows, grp 1 and 2."""

    workout_id: str = Field(description="FK to workouts")
    exercise: str = Field(description="canonical name from the KB")
    exercise_raw: str = Field(description="as typed by the user")
    grp: int = Field(description="set-group ordinal within workout+exercise (1-based)")
    sets: int
    reps: float = Field(description="float — '2.5 real pull ups' is reps=2.5")
    per_side_lbs: float | None = Field(default=None, description="blank for stack/bodyweight")
    total_lbs: float | None = Field(
        default=None,
        description="per_side*2 + KB base_lbs; stack value verbatim; None for bodyweight",
    )
    assist_lbs: float | None = Field(default=None, description="assisted dips/pull-ups")
    notes: str | None = None


class KBEntry(BaseModel):
    """A row of the exercise_kb tab. Learning an alias needs no redeploy."""

    canonical: str
    aliases: list[str] = Field(default_factory=list)
    equipment: Equipment
    base_lbs: float = Field(
        default=0.0, description="implement base weight (olympic bar 45, smith ~25…)"
    )
    notes: str | None = None


class InboxItem(BaseModel):
    """A failed parse. Nothing is ever lost — raw_text always survives here."""

    timestamp: str
    raw_text: str
    error: str


# --- What the LLM emits (no workout_id — IDs are allocated at commit time) ---


class ParsedSetGroup(BaseModel):
    """A set-group as parsed from a message, before workout_id assignment.

    `exercise` may be blank/uncertain: the confirm card resolves it against the
    KB (the 'same as X, or new?' question) before commit.
    """

    exercise: str = Field(description="best-guess canonical name; may need KB confirm")
    exercise_raw: str = Field(description="exactly as the user typed it")
    grp: int = 1
    sets: int
    reps: float
    per_side_lbs: float | None = None
    total_lbs: float | None = None
    assist_lbs: float | None = None
    notes: str | None = None


class ParsedWorkout(BaseModel):
    """One Workout + its set-groups, as produced by the LLM for `log_workout`.

    Excludes workout_id and logged_at (assigned at commit). A message can yield
    several of these (bench day + basketball → two ParsedWorkouts).
    """

    date: str = Field(description="resolved workout date, YYYY-MM-DD")
    type: str = Field(default="strength", description="strength / basketball / …")
    start: str | None = None
    end: str | None = None
    notes: str | None = None
    raw_text: str = Field(default="", description="original message text; the server fills this in")
    sets: list[ParsedSetGroup] = Field(
        default_factory=list, description="empty for non-strength sessions"
    )


class ParseResult(BaseModel):
    """Everything parsed from a single chat message.

    One message can describe several sessions, so the output is always a list.
    This is the schema the LLM emits and that the eval and agent validate against.
    """

    workouts: list[ParsedWorkout] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_shape(cls, data: object) -> object:
        """Be liberal in what we accept from weaker models.

        The correct shape is `{"workouts": [{...}, ...]}`, but free-tier models
        sometimes send a singular `{"workout": {...}}`, a bare object instead of a
        list, or a JSON-encoded string. Without normalising, a sloppy envelope
        silently collapses to zero workouts and renders an empty confirm card.
        """
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return data
        data = dict(data)
        workouts = data.get("workouts")
        if workouts is None and "workout" in data:  # singular → plural
            workouts = data.pop("workout")
        if isinstance(workouts, str):
            workouts = json.loads(workouts)
        if isinstance(workouts, dict):  # single object → list of one
            workouts = [workouts]
        if isinstance(workouts, list):
            workouts = [json.loads(w) if isinstance(w, str) else w for w in workouts]
            data["workouts"] = workouts
        return data


def compute_total(per_side: float | None, kb_entry: KBEntry) -> float | None:
    """Total weight moved for one set-group, per DESIGN.md.

    - barbell/smith/hex: per_side*2 + base_lbs  (e.g. 60/side + 45 bar → 165)
    - stack/cable:       the stated number passes through verbatim as the total
    - bodyweight/assisted: None (assist_lbs is preserved separately on the row)

    For stack/cable the "per_side" argument carries the stated total (the parser
    puts the machine number there); per-side has no meaning for a stack.
    """
    eq = kb_entry.equipment
    if eq in _PER_SIDE_PLUS_BASE:
        if per_side is None:
            return None
        return per_side * 2 + kb_entry.base_lbs
    if eq in _STACK_LIKE:
        return per_side
    # bodyweight / assisted → no external load total
    return None
