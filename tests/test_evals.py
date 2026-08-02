"""Eval-harness tests — fixture integrity + scorer logic, no network."""

from __future__ import annotations

import copy

import pytest

from app.evals import build_report, extract_json, load_fixtures, score_fixture
from app.kb_seed import STARTER_KB
from app.models import Equipment, ParseResult, compute_total

KB = {e.canonical: e for e in STARTER_KB}


# A canonical single-set-group expectation reused by scorer tests.
def _exp() -> dict:
    return {
        "workouts": [
            {
                "date": "2026-07-14",
                "type": "strength",
                "sets": [
                    {
                        "exercise": "Back Squat",
                        "exercise_raw": "squats",
                        "grp": 1,
                        "sets": 3,
                        "reps": 7,
                        "per_side_lbs": 60,
                        "total_lbs": 165,
                        "assist_lbs": None,
                    }
                ],
            }
        ]
    }


# --- fixtures are valid, complete, and internally consistent ---


def test_fixtures_load_valid_and_totals_consistent():
    fixtures = load_fixtures()
    assert len(fixtures) >= 12
    for name, fx in fixtures:
        assert "message" in fx and "expected" in fx, name
        ParseResult.model_validate(fx["expected"])  # raises on bad shape
        # Derivable totals must match compute_total (bodyweight/assisted carry
        # none) — guards the golden files against hand-edit drift.
        for w in fx["expected"]["workouts"]:
            for s in w["sets"]:
                kb = KB[s["exercise"]]
                if kb.equipment in (Equipment.barbell, Equipment.smith, Equipment.hex):
                    if s["per_side_lbs"] is not None:
                        assert s["total_lbs"] == compute_total(s["per_side_lbs"], kb), name
                if kb.equipment in (Equipment.bodyweight, Equipment.assisted):
                    assert s["total_lbs"] is None, (name, s["exercise"])
                if s["assist_lbs"] is not None:
                    assert s["total_lbs"] is None, (name, s["exercise"])


def test_golden_quirks_present():
    # The hard parsing cases must actually exist in the corpus, else the eval is soft.
    sets = [s for _n, fx in load_fixtures() for w in fx["expected"]["workouts"] for s in w["sets"]]
    workouts = [w for _n, fx in load_fixtures() for w in fx["expected"]["workouts"]]
    assert any(s["reps"] == 2.5 for s in sets)  # fractional reps
    assert any(s["grp"] == 2 for s in sets)  # a two-set-group exercise
    assert any(s["assist_lbs"] == 95 for s in sets)  # assist weight
    assert any(w["type"] == "basketball" and not w["sets"] for w in workouts)  # sets-less


# --- scorer ---


def test_perfect_prediction_scores_100():
    for name, fx in load_fixtures():
        exp = fx["expected"]
        r = score_fixture(exp, copy.deepcopy(exp), STARTER_KB)
        assert r.pct == 100.0, (name, r.field_hits)


def test_prediction_via_alias_resolves_to_canonical():
    # Model emits a raw alias instead of the canonical name → still correct,
    # because scoring resolves through the KB.
    exp = _exp()
    pred = copy.deepcopy(exp)
    pred["workouts"][0]["sets"][0]["exercise"] = "squats"  # alias, not canonical
    r = score_fixture(exp, pred, STARTER_KB)
    assert r.field_hits["exercise"] == [True]


def test_scorer_penalizes_wrong_and_missing():
    exp = _exp()

    # wrong field values lower the score and flag the specific field
    wrong = copy.deepcopy(exp)
    wrong["workouts"][0]["sets"][0]["sets"] = 5
    wrong["workouts"][0]["type"] = "basketball"
    r = score_fixture(exp, wrong, STARTER_KB)
    assert r.field_hits["sets"] == [False] and r.field_hits["type"] == [False] and r.pct < 100.0

    # dropping a second set-group is penalized
    two_grp = copy.deepcopy(exp)
    two_grp["workouts"][0]["sets"].append(
        {
            **exp["workouts"][0]["sets"][0],
            "grp": 2,
            "sets": 1,
            "reps": 8,
            "per_side_lbs": 30,
            "total_lbs": 105,
        }
    )
    dropped_grp = copy.deepcopy(two_grp)
    dropped_grp["workouts"][0]["sets"].pop()
    assert score_fixture(two_grp, dropped_grp, STARTER_KB).pct < 100.0

    # dropping a whole workout is penalized
    multi = {
        "workouts": [
            {"date": "2026-07-13", "type": "strength", "sets": []},
            {"date": "2026-07-14", "type": "basketball", "sets": []},
        ]
    }
    assert score_fixture(multi, {"workouts": [multi["workouts"][0]]}, STARTER_KB).pct < 100.0


# --- reply extraction (pure function, one row per shape) ---


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"workouts": []}', {"workouts": []}),
        ('```json\n{"workouts": []}\n```', {"workouts": []}),
        ('Here is the parse:\n{"workouts": [{"date": "x"}]}\nDone.', {"workouts": [{"date": "x"}]}),
    ],
)
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_build_report_smoke():
    exp = {"workouts": [{"date": "2026-07-14", "type": "strength", "sets": []}]}
    r = score_fixture(exp, exp, STARTER_KB)
    r.name = "x.json"
    report = build_report("some/model:free", [r], "base rules only")
    assert "Aggregate field accuracy" in report and "some/model:free" in report
