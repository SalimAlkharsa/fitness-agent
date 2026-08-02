"""Duplicate detection for logged workouts.

Deterministic, always-on, enforced in the publish path — never a model-invoked
tool and never a skill (a weak free-tier model would just skip it). The single
source of truth for both signals; backfill imports its hash helpers from here so
the logic can't drift between the two paths.

Two signals, both derived at read time from columns that already exist, so there
is no schema change:

- **exact**    — sha1 of normalized `raw_text`: a literal re-send / double-tap /
  re-paste of the same message.
- **semantic** — sha1 of `date|type|sorted(exercise:setsxreps)`: order- and
  wording-independent. Weight is deliberately EXCLUDED so a re-log that only
  corrected a weight still flags as the same session.

`find_duplicate` surfaces the match; the confirm card renders it as an overridable
warning (see `main._build_cards` + `_log_card.html`). Nothing is ever silently
blocked or dropped — confirm-first means the ✅ always wins.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.models import KBEntry, ParsedWorkout, SetGroup, Workout
from app.prompt import resolve_exercise


def normalize_raw(text: str) -> str:
    return " ".join((text or "").lower().split())


def raw_hash(text: str) -> str:
    return hashlib.sha1(normalize_raw(text).encode()).hexdigest()


def committed_hashes(store) -> set[str]:
    """Exact-hashes of normalized raw_text for every already-logged workout."""
    return {raw_hash(w.raw_text) for w in store.read_workouts()}


def _signature(date: str, type_: str, items: list[tuple[str, int, float]]) -> str:
    """date | type | sorted(exercise:setsxreps) → sha1. Weight is excluded on purpose."""
    parts = sorted(f"{name.strip().lower()}:{int(sets)}x{reps:g}" for name, sets, reps in items)
    return hashlib.sha1((f"{date}|{type_.strip().lower()}|" + ";".join(parts)).encode()).hexdigest()


def workout_signature(pw: ParsedWorkout, kb: list[KBEntry] | None = None) -> str:
    """Semantic signature of a parsed workout (exercises resolved to KB canonical)."""
    items: list[tuple[str, int, float]] = []
    for sg in pw.sets:
        canonical = None
        if kb:
            canonical = resolve_exercise(sg.exercise, kb) or resolve_exercise(sg.exercise_raw, kb)
        name = canonical or sg.exercise or sg.exercise_raw or ""
        items.append((name, sg.sets, sg.reps))
    return _signature(pw.date, pw.type, items)


def _stored_signature(workout: Workout, sets: list[SetGroup]) -> str:
    """Semantic signature of an already-committed workout (sets carry canonical names)."""
    items = [(s.exercise or s.exercise_raw or "", s.sets, s.reps) for s in sets]
    return _signature(workout.date, workout.type, items)


@dataclass
class DuplicateMatch:
    """An already-logged workout the incoming one duplicates."""

    workout_id: str
    date: str
    type: str
    kind: str  # "exact" | "semantic"


def find_duplicate(
    pw: ParsedWorkout, store, kb: list[KBEntry] | None = None
) -> DuplicateMatch | None:
    """The first already-logged workout `pw` duplicates, or None.

    Exact (raw_text) always wins over semantic. Built on the fly from stored rows —
    the same technique backfill uses — so nothing is persisted or migrated.
    """
    if store is None:
        return None
    workouts = store.read_workouts()
    if not workouts:
        return None

    sets_by_wid: dict[str, list[SetGroup]] = {}
    for s in store.read_sets():
        sets_by_wid.setdefault(s.workout_id, []).append(s)

    incoming_hash = raw_hash(pw.raw_text)
    incoming_sig = workout_signature(pw, kb)
    semantic: DuplicateMatch | None = None
    for w in workouts:
        if raw_hash(w.raw_text) == incoming_hash:
            return DuplicateMatch(w.workout_id, w.date, w.type, "exact")
        if semantic is None:
            if _stored_signature(w, sets_by_wid.get(w.workout_id, [])) == incoming_sig:
                semantic = DuplicateMatch(w.workout_id, w.date, w.type, "semantic")
    return semantic
