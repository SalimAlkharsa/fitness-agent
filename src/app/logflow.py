"""Log-flow commit logic: turn a confirmed ParsedWorkout into sheet rows.

Kept separate from the web layer so it's unit-testable without HTTP. Handles:
- exercise resolution against the KB (with learned aliases / new entries),
- total-weight (re)computation at commit time,
- workout_id allocation + append of workouts + sets,
- detecting unknown exercises so the card can ask the KB question,
- undo (delete a workout + its sets).
"""

from __future__ import annotations

import datetime
import difflib
from dataclasses import dataclass

from app.models import (
    Equipment,
    KBEntry,
    ParsedSetGroup,
    ParsedWorkout,
    SetGroup,
    Workout,
    compute_total,
)
from app.prompt import resolve_exercise

_PER_SIDE_EQUIP = {Equipment.barbell, Equipment.smith, Equipment.hex}


@dataclass
class UnknownExercise:
    """A set-group whose exercise isn't in the KB — the card asks about it."""

    index: int  # position in the workout's sets list
    raw: str  # what the user typed
    candidates: list[str]  # top fuzzy KB canonical matches to offer


def kb_candidates(name: str, kb: list[KBEntry], n: int = 3) -> list[str]:
    """Top-n canonical KB names closest to `name` (fuzzy over names + aliases)."""
    if not name:
        return []
    pool: dict[str, str] = {}  # lowercased key -> canonical
    for e in kb:
        pool[e.canonical.lower()] = e.canonical
        for a in e.aliases:
            pool[a.lower()] = e.canonical
    matches = difflib.get_close_matches(name.lower(), list(pool), n=n * 2, cutoff=0.4)
    out: list[str] = []
    for m in matches:
        canon = pool[m]
        if canon not in out:
            out.append(canon)
        if len(out) >= n:
            break
    return out


def unknown_exercises(pw: ParsedWorkout, kb: list[KBEntry]) -> list[UnknownExercise]:
    """Set-groups whose exercise resolves to nothing in the KB."""
    out: list[UnknownExercise] = []
    for i, sg in enumerate(pw.sets):
        if resolve_exercise(sg.exercise, kb) or resolve_exercise(sg.exercise_raw, kb):
            continue
        out.append(
            UnknownExercise(
                index=i,
                raw=sg.exercise_raw or sg.exercise,
                candidates=kb_candidates(sg.exercise_raw or sg.exercise, kb),
            )
        )
    return out


def _kb_map(kb: list[KBEntry]) -> dict[str, KBEntry]:
    return {e.canonical: e for e in kb}


def autolearn_kb_updates(pw: ParsedWorkout, kb: list[KBEntry]) -> list[KBEntry]:
    """KB entries to register a backfill workout's unknown exercises.

    The interactive card asks 'same as X, or new?'; the CLI `--commit` path has no
    human in the loop, so it auto-registers each unknown exercise by name so later
    messages in the same run resolve it.

    Equipment is a conservative guess the user can refine by editing the KB: a
    weighted movement → `cable` (its stated number passes through verbatim, so
    `_final_total` never fabricates a base weight or doubles a value), otherwise
    `bodyweight`. The point here is *name resolution*, not weight math.
    """
    updates: list[KBEntry] = []
    grown = list(kb)  # so two unknowns in one workout don't get added twice
    for sg in pw.sets:
        if resolve_exercise(sg.exercise, grown) or resolve_exercise(sg.exercise_raw, grown):
            continue
        name = sg.exercise or sg.exercise_raw
        if not name:
            continue
        raw = sg.exercise_raw or sg.exercise
        equip = (
            Equipment.cable
            if sg.per_side_lbs is not None or sg.total_lbs is not None
            else Equipment.bodyweight
        )
        entry = KBEntry(
            canonical=name,
            aliases=[raw] if raw and raw != name else [],
            equipment=equip,
        )
        updates.append(entry)
        grown.append(entry)
    return updates


def _final_total(sg: ParsedSetGroup, entry: KBEntry | None) -> float | None:
    """Recompute total at commit so an edited per_side stays consistent with the KB.

    Per-side implements: total = per_side*2 + base. Stack/cable/bodyweight: trust the
    stated total (which the parser already put in total_lbs)."""
    if entry is not None and entry.equipment in _PER_SIDE_EQUIP and sg.per_side_lbs is not None:
        return compute_total(sg.per_side_lbs, entry)
    return sg.total_lbs


def commit_workout(
    store,
    pw: ParsedWorkout,
    kb_updates: list[KBEntry] | None = None,
) -> tuple[Workout, str]:
    """Apply any KB learning, allocate an id, append the workout + its sets.

    Returns the committed Workout and a human summary line. Raises nothing beyond
    what the store raises; callers validate `pw` (Pydantic) before calling.
    """
    if kb_updates:
        for entry in kb_updates:
            store.upsert_kb(entry)

    kb = store.kb_entries()
    kbm = _kb_map(kb)
    wid = store.next_workout_id()
    now = datetime.datetime.now().isoformat(timespec="minutes")

    workout = Workout(
        workout_id=wid,
        date=pw.date,
        type=pw.type,
        start=pw.start,
        end=pw.end,
        notes=pw.notes,
        raw_text=pw.raw_text,
        logged_at=now,
    )

    sets: list[SetGroup] = []
    for sg in pw.sets:
        canonical = (
            resolve_exercise(sg.exercise, kb)
            or resolve_exercise(sg.exercise_raw, kb)
            or sg.exercise
        )
        entry = kbm.get(canonical)
        sets.append(
            SetGroup(
                workout_id=wid,
                exercise=canonical,
                exercise_raw=sg.exercise_raw,
                grp=sg.grp,
                sets=sg.sets,
                reps=sg.reps,
                per_side_lbs=sg.per_side_lbs,
                total_lbs=_final_total(sg, entry),
                assist_lbs=sg.assist_lbs,
                notes=sg.notes,
            )
        )

    store.append_workout(workout)
    store.append_sets(sets)
    return workout, summarize_committed(workout, sets)


def _fs(form, key: str) -> str | None:
    v = form.get(key)
    v = v.strip() if isinstance(v, str) else v
    return v or None


def _ff(form, key: str) -> float | None:
    v = _fs(form, key)
    return float(v) if v else None


def parsed_workout_from_form(form) -> ParsedWorkout:
    """Rebuild a ParsedWorkout from the (possibly edited) confirm-card form.

    Raises ValueError / pydantic ValidationError on bad numeric or missing fields;
    the caller renders that as an inline card error.
    """
    n = int(form.get("n_sets") or 0)
    sets: list[ParsedSetGroup] = []
    for i in range(n):
        p = f"s{i}-"
        ex = _fs(form, p + "exercise")
        if ex is None and _fs(form, p + "exercise_raw") is None:
            continue  # row cleared out entirely
        sets.append(
            ParsedSetGroup(
                exercise=ex or _fs(form, p + "exercise_raw") or "",
                exercise_raw=_fs(form, p + "exercise_raw") or ex or "",
                grp=int(form.get(p + "grp") or 1),
                sets=int(form.get(p + "sets") or 0),
                reps=float(form.get(p + "reps") or 0),
                per_side_lbs=_ff(form, p + "per_side"),
                total_lbs=_ff(form, p + "total"),
                assist_lbs=_ff(form, p + "assist"),
                notes=_fs(form, p + "notes"),
            )
        )
    return ParsedWorkout(
        date=_fs(form, "w-date") or "",
        type=_fs(form, "w-type") or "strength",
        start=_fs(form, "w-start"),
        end=_fs(form, "w-end"),
        notes=_fs(form, "w-notes"),
        raw_text=_fs(form, "w-raw") or "",
        sets=sets,
    )


def kb_updates_from_form(form, unknowns: list[UnknownExercise], kb: list[KBEntry]) -> list[KBEntry]:
    """Turn the card's KB-question answers into KBEntry upserts.

    'same as X' merges the raw name into X's aliases (preserving equipment/base);
    'new exercise' creates a fresh entry with the chosen equipment + base_lbs.
    """
    kbm = _kb_map(kb)
    updates: list[KBEntry] = []
    for u in unknowns:
        choice = form.get(f"kb{u.index}-choice")
        if not choice:
            continue
        if choice == "__new__":
            name = _fs(form, f"s{u.index}-exercise") or u.raw
            equip = form.get(f"kb{u.index}-equipment") or "bodyweight"
            updates.append(
                KBEntry(
                    canonical=name,
                    aliases=[u.raw],
                    equipment=Equipment(equip),
                    base_lbs=_ff(form, f"kb{u.index}-base") or 0.0,
                )
            )
        else:
            entry = kbm.get(choice)
            if entry:
                aliases = list(dict.fromkeys([*entry.aliases, u.raw]))
                updates.append(
                    KBEntry(
                        canonical=entry.canonical,
                        aliases=aliases,
                        equipment=entry.equipment,
                        base_lbs=entry.base_lbs,
                        notes=entry.notes,
                    )
                )
    return updates


def summarize_committed(workout: Workout, sets: list[SetGroup]) -> str:
    if not sets:
        return f"Logged {workout.workout_id}: {workout.type} on {workout.date}."
    parts = []
    for sg in sets:
        load = f"@{sg.total_lbs:g}" if sg.total_lbs is not None else ""
        parts.append(f"{sg.exercise} {sg.sets}x{sg.reps:g}{load}")
    return f"Logged {workout.workout_id}: " + "; ".join(parts)
