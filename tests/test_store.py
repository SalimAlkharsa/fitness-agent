"""SheetsStore tests.

Unit tests run against an in-memory fake (no creds). The integration test runs
against a live scratch spreadsheet only when GOOGLE_SA_JSON + SHEET_ID are set.

Deletion, inbox writes, and id formatting are exercised end-to-end by the undo
flow (test_logflow), the retry→inbox path (test_agent), and the confirm flow
(test_web), so they're not re-asserted at the store level here.
"""

from __future__ import annotations

import os

import pytest

from app import SCHEMA_VERSION
from app.kb_seed import STARTER_KB
from app.models import Equipment, KBEntry, SetGroup, Workout
from app.store import SheetsStore, _opt_float, _s
from tests.fake_sheets import FakeSpreadsheet


@pytest.fixture
def store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _workout(wid: str = "W0001", date: str = "2026-07-10") -> Workout:
    return Workout(
        workout_id=wid,
        date=date,
        type="strength",
        start="17:00",
        end="19:00",
        notes="lower body",
        raw_text="4x8 60LBs Squats on Both Sides",
        logged_at="2026-07-10T19:42",
    )


def _sets(wid: str = "W0001") -> list[SetGroup]:
    return [
        SetGroup(
            workout_id=wid,
            exercise="Back Squat",
            exercise_raw="Squats",
            grp=1,
            sets=4,
            reps=8,
            per_side_lbs=60,
            total_lbs=165,
        ),
        SetGroup(
            workout_id=wid,
            exercise="Smith Calf Raise",
            exercise_raw="Calf Raises",
            grp=1,
            sets=3,
            reps=25,
            per_side_lbs=90,
            total_lbs=175,
        ),
    ]


def test_cell_helpers():
    # _s renders sheet cells: blank for None, ints without ".0", decimals kept.
    assert (_s(None), _s(60.0), _s(3.5)) == ("", "60", "3.5")
    # _opt_float parses back: blank/whitespace → None, numeric → float.
    assert (_opt_float(""), _opt_float("  "), _opt_float("60")) == (None, None, 60.0)


def test_bootstrap_creates_tabs_seeds_and_is_idempotent(store: SheetsStore):
    titles = {ws.title for ws in store.spreadsheet.worksheets()}
    assert {"workouts", "sets", "exercise_kb", "inbox", "_meta"} <= titles
    assert "Sheet1" not in titles  # default sheet cleaned up
    assert store.schema_version == SCHEMA_VERSION
    assert store.next_workout_id() == "W0001"  # counter started at 0, formats W####
    assert len(store.kb_entries()) == len(STARTER_KB)

    # re-running bootstrap leaves data, KB, and the id counter untouched
    store.append_workout(_workout())
    before_workouts = store.read_workouts()
    before_counter = store._meta_dict()["workout_id_counter"]
    store.bootstrap(kb_seed=STARTER_KB)
    assert store.read_workouts() == before_workouts
    assert len(store.kb_entries()) == len(STARTER_KB)  # no duplicate KB rows
    assert store._meta_dict()["workout_id_counter"] == before_counter


def test_workout_and_sets_roundtrip(store: SheetsStore):
    store.append_workout(_workout())
    store.append_sets(_sets())
    assert store.read_workouts() == [_workout()]
    assert store.read_sets() == _sets()

    # optional fields (start/end/notes) round-trip as None, not "".
    store.append_workout(
        Workout(
            workout_id="W0005",
            date="2026-07-10",
            type="basketball",
            start=None,
            end=None,
            notes=None,
            raw_text="basketball",
            logged_at="x",
        )
    )
    assert store.read_workouts()[-1].start is None


def test_read_sets_filters_and_excludes_non_iso_dates(store: SheetsStore):
    store.append_workout(_workout(wid="W0001", date="2026-07-01"))
    store.append_workout(_workout(wid="W0002", date="2026-07-10"))
    store.append_sets(_sets("W0001"))
    store.append_sets(_sets("W0002"))

    squats = store.read_sets(exercise="back squat")  # case-insensitive
    assert len(squats) == 2 and all(sg.exercise == "Back Squat" for sg in squats)
    assert {sg.workout_id for sg in store.read_sets(date_from="2026-07-05")} == {"W0002"}

    # Backfill assigns date="unknown"; such rows must not leak into a date-bounded
    # query via lexicographic comparison — but must still appear unfiltered.
    store.append_workout(_workout(wid="W0009", date="unknown"))
    store.append_sets(_sets("W0009"))
    assert "W0009" not in {sg.workout_id for sg in store.read_sets(date_from="2000-01-01")}
    assert "W0009" in {sg.workout_id for sg in store.read_sets()}


def test_kb_upsert_replaces_appends_and_splits_aliases(store: SheetsStore):
    n = len(store.kb_entries())
    # seeded aliases are split on ';'
    back_squat = next(e for e in store.kb_entries() if e.canonical == "Back Squat")
    assert "squats" in back_squat.aliases

    # upsert on an existing canonical replaces the row (no dup) and merges a new alias
    store.upsert_kb(
        KBEntry(
            canonical="Back Squat",
            aliases=["squats", "new alias"],
            equipment=Equipment.barbell,
            base_lbs=45,
        )
    )
    entries = {e.canonical: e for e in store.kb_entries()}
    assert len(store.kb_entries()) == n and "new alias" in entries["Back Squat"].aliases

    # upsert on a novel canonical appends
    store.upsert_kb(KBEntry(canonical="Leg Press", equipment=Equipment.stack, base_lbs=0))
    assert len(store.kb_entries()) == n + 1


# --- live integration (skipped unless creds present) ---


@pytest.mark.skipif(
    not (os.getenv("RUN_LIVE_SHEETS") and os.getenv("GOOGLE_SA_JSON") and os.getenv("SHEET_ID")),
    reason="live test — set RUN_LIVE_SHEETS=1 (with creds) to run against a scratch sheet",
)
def test_store_integration():
    store = SheetsStore.open()
    store.bootstrap(kb_seed=STARTER_KB)
    store.bootstrap(kb_seed=STARTER_KB)  # idempotent

    wid = store.next_workout_id()
    w = _workout(wid)
    store.append_workout(w)
    store.append_sets(_sets(wid))

    assert w in store.read_workouts()
    assert len([s for s in store.read_sets() if s.workout_id == wid]) == 2

    deleted = store.delete_workout(wid)
    assert deleted == 3
    assert wid not in {x.workout_id for x in store.read_workouts()}
    assert wid not in {x.workout_id for x in store.read_sets()}
