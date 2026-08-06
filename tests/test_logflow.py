"""Log-flow commit tests: commit, KB questions/learning, undo — fake store."""

from __future__ import annotations

import json
import pathlib

import pytest

from app.kb_seed import STARTER_KB
from app.logflow import (
    commit_workout,
    kb_updates_from_form,
    parsed_workout_from_form,
    unknown_exercises,
    workout_to_parsed,
)
from app.models import Equipment, ParsedSetGroup, ParsedWorkout
from app.prompt import resolve_exercise
from app.store import SheetsStore
from tests.fake_sheets import FakeSpreadsheet

GOLDEN = pathlib.Path("evals/golden")


@pytest.fixture
def store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _squat_workout() -> ParsedWorkout:
    return ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="Squats 3x5 60 each side",
        sets=[
            ParsedSetGroup(
                exercise="Back Squat",
                exercise_raw="Squats",
                grp=1,
                sets=3,
                reps=5,
                per_side_lbs=60,
                total_lbs=165,
            )
        ],
    )


# --- commit: id allocation, append, alias-resolution, and total recompute in one flow ---


def test_commit_allocates_id_appends_and_normalizes(store: SheetsStore):
    pw = _squat_workout()
    pw.sets[0].exercise = "squats"  # an alias, not the canonical name
    pw.sets[0].per_side_lbs = 75  # user edited per-side up...
    pw.sets[0].total_lbs = 165  # ...leaving a now-stale total
    workout, summary = commit_workout(store, pw)

    assert workout.workout_id == "W0001" and "Logged W0001" in summary
    ws = store.read_workouts()
    assert len(ws) == 1 and ws[0].workout_id == "W0001"
    sets = store.read_sets()
    assert len(sets) == 1 and sets[0].workout_id == "W0001"
    assert sets[0].exercise == "Back Squat"  # alias resolved to canonical
    assert sets[0].total_lbs == 75 * 2 + 45  # total recomputed from edited per-side = 195


def test_all_golden_fixtures_commit(store: SheetsStore):
    committed = 0
    for path in sorted(GOLDEN.glob("*.json")):
        fx = json.loads(path.read_text())
        for w in fx["expected"]["workouts"]:
            pw = ParsedWorkout.model_validate(w)
            workout, _ = commit_workout(store, pw)
            got = [s for s in store.read_sets() if s.workout_id == workout.workout_id]
            assert len(got) == len(pw.sets), path.name
            committed += 1
    assert committed >= 13  # at least one workout per fixture (07 has two)
    ids = [w.workout_id for w in store.read_workouts()]
    assert len(ids) == len(set(ids)) == committed  # sequential + unique


# --- KB questions + learning ---


def test_unknown_exercise_detected_with_candidates(store: SheetsStore):
    pw = ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="Leg Curls 3x10 50",
        sets=[
            ParsedSetGroup(
                exercise="Leg Curl", exercise_raw="Leg Curls", grp=1, sets=3, reps=10, total_lbs=50
            )
        ],
    )
    unknowns = unknown_exercises(pw, store.kb_entries())
    assert len(unknowns) == 1 and unknowns[0].index == 0 and unknowns[0].raw == "Leg Curls"


def test_kb_question_same_as_persists_alias(store: SheetsStore):
    # "squatz" is unknown; answering "same as Back Squat" persists the alias so the
    # same word logs without a question next time.
    pw = ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="squatz 3x5 60 each side",
        sets=[
            ParsedSetGroup(
                exercise="squatz",
                exercise_raw="squatz",
                grp=1,
                sets=3,
                reps=5,
                per_side_lbs=60,
                total_lbs=165,
            )
        ],
    )
    unknowns = unknown_exercises(pw, store.kb_entries())
    assert unknowns  # unknown at first
    updates = kb_updates_from_form({"kb0-choice": "Back Squat"}, unknowns, store.kb_entries())
    commit_workout(store, pw, kb_updates=updates)

    assert store.read_sets()[-1].exercise == "Back Squat"  # committed under canonical
    assert resolve_exercise("squatz", store.kb_entries()) == "Back Squat"  # alias persisted
    assert unknown_exercises(pw, store.kb_entries()) == []  # no question next time


def test_kb_question_new_exercise_creates_entry(store: SheetsStore):
    pw = ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="Nordic Curl 3x8",
        sets=[ParsedSetGroup(exercise="Nordic Curl", exercise_raw="Nordic Curl", sets=3, reps=8)],
    )
    unknowns = unknown_exercises(pw, store.kb_entries())
    form = {"kb0-choice": "__new__", "kb0-equipment": "bodyweight", "kb0-base": "0"}
    updates = kb_updates_from_form(form, unknowns, store.kb_entries())
    commit_workout(store, pw, kb_updates=updates)

    entry = next(e for e in store.kb_entries() if e.canonical == "Nordic Curl")
    assert entry.equipment == Equipment.bodyweight
    assert resolve_exercise("Nordic Curl", store.kb_entries()) == "Nordic Curl"


# --- undo ---


def test_undo_removes_workout_and_sets(store: SheetsStore):
    w1, _ = commit_workout(store, _squat_workout())
    w2, _ = commit_workout(store, _squat_workout())
    deleted = store.delete_workout(w1.workout_id)
    assert deleted == 2  # 1 workout row + 1 set row
    assert {w.workout_id for w in store.read_workouts()} == {w2.workout_id}
    assert all(s.workout_id == w2.workout_id for s in store.read_sets())


# --- edit (rehydrate -> correct a field -> replace in place) ---


def test_edit_workout_replaces_in_place(store: SheetsStore):
    other, _ = commit_workout(store, _squat_workout())  # a second workout, untouched
    original, _ = commit_workout(store, _squat_workout())

    # Rehydrate exactly what an edit_workout confirm card would prefill...
    original_sets = [s for s in store.read_sets() if s.workout_id == original.workout_id]
    pw = workout_to_parsed(original, original_sets)
    assert pw.sets[0].total_lbs == 165  # matches the original commit

    # ...the user corrects the per-side weight (as if editing the card's input)...
    pw.sets[0].per_side_lbs = 70
    pw.sets[0].total_lbs = 165  # stale until recomputed at commit, same as a fresh log
    workout, summary = commit_workout(store, pw, workout_id=original.workout_id)

    assert workout.workout_id == original.workout_id and "Updated" in summary
    ws = store.read_workouts()
    assert {w.workout_id for w in ws} == {other.workout_id, original.workout_id}  # no new row
    edited_sets = [s for s in store.read_sets() if s.workout_id == original.workout_id]
    assert len(edited_sets) == 1  # old set row replaced, not duplicated
    assert edited_sets[0].total_lbs == 70 * 2 + 45  # recomputed from the corrected per-side


# --- form parsing (feeds the confirm card) ---


def test_parsed_workout_from_form_roundtrips_and_rejects_bad_numbers():
    form = {
        "n_sets": "1",
        "w-date": "2026-07-14",
        "w-type": "strength",
        "w-raw": "x",
        "s0-exercise": "Back Squat",
        "s0-exercise_raw": "Squats",
        "s0-grp": "1",
        "s0-sets": "3",
        "s0-reps": "5",
        "s0-per_side": "60",
        "s0-total": "165",
    }
    pw = parsed_workout_from_form(form)
    assert pw.date == "2026-07-14"
    assert len(pw.sets) == 1 and pw.sets[0].reps == 5 and pw.sets[0].per_side_lbs == 60
    # a non-numeric cell raises rather than silently coercing to a bogus value
    with pytest.raises(ValueError):
        parsed_workout_from_form({**form, "s0-sets": "abc"})
