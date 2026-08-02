"""Backfill tests — split/dedup/parse/commit with a fake parser (no network)."""

from __future__ import annotations

import json
import pathlib

import pytest

from app.backfill import (
    BackfillItem,
    committed_hashes,
    normalize_raw,
    parse_dump,
    raw_hash,
    split_dump,
)
from app.kb_seed import STARTER_KB
from app.logflow import autolearn_kb_updates, commit_workout, unknown_exercises
from app.models import ParsedSetGroup, ParsedWorkout, ParseResult
from app.store import SheetsStore
from tests.fake_sheets import FakeSpreadsheet

GOLDEN = pathlib.Path("evals/golden")


@pytest.fixture
def store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _fixture_parser():
    """A parse_fn backed by the golden fixtures (message → expected ParseResult)."""
    table = {}
    dump_parts = []
    for path in sorted(GOLDEN.glob("*.json")):
        fx = json.loads(path.read_text())
        table[normalize_raw(fx["message"])] = ParseResult.model_validate(fx["expected"])
        dump_parts.append(fx["message"])
    dump = "\n\n\n".join(dump_parts)  # messages separated by a 2-blank-line gap

    def parse_fn(message: str) -> ParseResult:
        return table[normalize_raw(message)]

    return parse_fn, dump, table


# --- splitting + hashing ---


def test_split_keeps_internal_blank_lines_together():
    dump = "Squats 3x5\n\ntempo work\n\n\nDeadlift 3x10"  # 1 blank inside, 2 between
    parts = split_dump(dump)
    assert len(parts) == 2
    assert "tempo work" in parts[0]
    assert parts[1] == "Deadlift 3x10"


def test_normalize_and_hash_stable():
    assert normalize_raw("  Squats   3x5\nSTUFF ") == "squats 3x5 stuff"
    assert raw_hash("Squats 3x5") == raw_hash("squats   3x5")


# --- dedup: against already-committed rows, and within a single run ---


def test_parse_dump_skips_already_committed(store: SheetsStore):
    parse_fn, dump, _ = _fixture_parser()
    first = split_dump(dump)[0]
    pw = ParsedWorkout.model_validate(
        json.loads((GOLDEN / "01_mixed_session.json").read_text())["expected"]["workouts"][0]
    )
    commit_workout(store, pw)
    assert raw_hash(first) in committed_hashes(store)

    items = parse_dump(dump, parse_fn, store)
    assert any(i.duplicate and raw_hash(i.message) == raw_hash(first) for i in items)


def test_parse_dump_dedups_within_run(store: SheetsStore):
    msg = "Squats 3x5 60 each side"
    dump = f"{msg}\n\n\n{msg}"  # same message twice
    calls = []

    def parse_fn(m: str) -> ParseResult:
        calls.append(m)
        return ParseResult(workouts=[ParsedWorkout(date="2026-07-14", raw_text=m)])

    items = parse_dump(dump, parse_fn, store)
    assert len(items) == 2 and items[1].duplicate is True
    assert len(calls) == 1  # the duplicate was not parsed again


# --- full history commits, correct dates, and re-running is a no-op ---


def test_full_golden_history_backfills_then_is_idempotent(store: SheetsStore):
    parse_fn, dump, table = _fixture_parser()
    items = parse_dump(dump, parse_fn, store)
    assert all(not i.error for i in items) and all(not i.duplicate for i in items)  # fresh store

    for item in items:
        for pw in item.workouts:
            commit_workout(store, pw)

    expected_total = sum(len(pr.workouts) for pr in table.values())  # 07 contributes two
    assert len(store.read_workouts()) == expected_total
    dates = {w.date for w in store.read_workouts()}
    assert "2026-07-13" in dates and "2026-07-14" in dates

    # re-running the identical dump adds nothing
    second = parse_dump(dump, parse_fn, store)
    assert all(i.duplicate for i in second)
    assert [pw for i in second for pw in i.workouts] == []
    assert len(store.read_workouts()) == expected_total


# --- KB grows during backfill; later messages benefit ---


def _pw_with(exercise_raw: str, per_side: float | None = 40) -> ParsedWorkout:
    return ParsedWorkout(
        date="2026-07-14",
        raw_text=f"{exercise_raw} 3x10",
        sets=[
            ParsedSetGroup(
                exercise=exercise_raw,
                exercise_raw=exercise_raw,
                grp=1,
                sets=3,
                reps=10,
                per_side_lbs=per_side,
                total_lbs=per_side,
            )
        ],
    )


def test_autolearn_registers_unknowns_once_so_later_messages_ask_nothing(store: SheetsStore):
    kb = store.kb_entries()
    # "Back Squat" is in STARTER_KB → nothing to learn.
    assert autolearn_kb_updates(_pw_with("Back Squat"), kb) == []

    first, second = _pw_with("Zercher Carry"), _pw_with("Zercher Carry")
    assert len(unknown_exercises(first, kb)) == 1  # novel → asked once
    updates = autolearn_kb_updates(first, kb)
    assert len(updates) == 1 and updates[0].canonical == "Zercher Carry"
    commit_workout(store, first, updates)
    kb = store.kb_entries()  # refresh as the CLI --commit loop does

    # message 2: same exercise now resolves silently, no duplicate KB row
    assert unknown_exercises(second, kb) == []
    assert autolearn_kb_updates(second, kb) == []
    assert sum(e.canonical == "Zercher Carry" for e in kb) == 1


def test_unknown_date_flagged():
    unknown = BackfillItem(
        message="squats 3x5",
        workouts=[ParsedWorkout(date="unknown", raw_text="squats 3x5")],
    )
    ok = BackfillItem(message="x", workouts=[ParsedWorkout(date="2026-07-14", raw_text="x")])
    assert unknown.needs_date is True and ok.needs_date is False
