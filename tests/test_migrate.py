"""Migration framework tests.

Runner logic, the demo 002_add_rpe end-to-end, the startup version gate, and the
up-to-date no-op — all against the in-memory fake (no creds).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import SCHEMA_VERSION
from app.kb_seed import STARTER_KB
from app.migrate import (
    SchemaVersionError,
    check_schema_version,
    discover,
    run,
    status,
)
from app.models import SetGroup, Workout
from app.store import SheetsStore
from tests.fake_sheets import FakeSpreadsheet

DEMO_DIR = Path(__file__).parent / "fixtures" / "migrations"
REAL_DIR = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _seed_one_set(store: SheetsStore) -> None:
    store.append_workout(
        Workout(workout_id="W0001", date="2026-07-14", type="strength", raw_text="x", logged_at="t")
    )
    store.append_sets(
        [
            SetGroup(
                workout_id="W0001",
                exercise="Back Squat",
                exercise_raw="squats",
                grp=1,
                sets=5,
                reps=6,
                per_side_lbs=70,
                total_lbs=165,
            )
        ]
    )


# --- discovery ---


def test_discover_real_migrations_sorted():
    migs = discover(REAL_DIR)
    assert [m.number for m in migs] == sorted(m.number for m in migs)
    assert any(m.number == 1 for m in migs)  # 001_initial present


def test_discover_ignores_non_migration_files(tmp_path: Path):
    (tmp_path / "001_ok.py").write_text("def apply(store): pass")
    (tmp_path / "README.md").write_text("nope")
    (tmp_path / "helpers.py").write_text("x = 1")
    assert [m.name for m in discover(tmp_path)] == ["001_ok.py"]


# --- version gate ---


def test_check_schema_version_matches_and_mismatch_raises(store: SheetsStore):
    check_schema_version(store, SCHEMA_VERSION)  # matching → no raise
    store.schema_version = 99
    with pytest.raises(SchemaVersionError) as ei:
        check_schema_version(store, code_version=SCHEMA_VERSION)
    assert "python -m app.migrate" in str(ei.value)  # error is actionable


# --- demo migration end-to-end ---


def test_demo_migration_adds_column_snapshots_and_bumps(store: SheetsStore):
    _seed_one_set(store)
    assert store.schema_version == 1

    applied = run(store, today="2026-07-15", migrations_dir=DEMO_DIR, do_snapshot=True)

    # 001 already applied (version 1); only 002 is pending
    assert applied == [2]
    # snapshot copy was requested with the dated pre-migration name
    assert store.spreadsheet.client.copies == ["2026-07-15 pre-migration-002"]
    # new column present and backfilled empty
    assert store._ws("sets").get_all_values()[0][-1] == "rpe"
    assert store._ws("sets").get_all_values()[1][-1] == ""
    assert store.schema_version == 2


def test_demo_migration_idempotent(store: SheetsStore):
    run(store, today="2026-07-15", migrations_dir=DEMO_DIR, do_snapshot=False)
    header_after_first = store._ws("sets").get_all_values()[0]
    # force a re-run by resetting version, then re-apply: column not duplicated
    store.schema_version = 1
    run(store, today="2026-07-16", migrations_dir=DEMO_DIR, do_snapshot=False)
    header_after_second = store._ws("sets").get_all_values()[0]
    assert header_after_first == header_after_second
    assert header_after_second.count("rpe") == 1


def test_run_is_noop_when_up_to_date_or_ahead(store: SheetsStore):
    # already at the real repo's latest → nothing runs, no snapshot taken
    _current, latest = status(store, REAL_DIR)
    store.schema_version = latest
    assert run(store, today="2026-07-15", migrations_dir=REAL_DIR, do_snapshot=True) == []
    assert store.spreadsheet.client.copies == []
    # version ahead of the demo migration → the lower-numbered migration is skipped
    store.schema_version = 5
    assert run(store, today="2026-07-15", migrations_dir=DEMO_DIR, do_snapshot=False) == []
