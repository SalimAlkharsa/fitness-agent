"""Analysis + query/chart tool tests — fake store, no network."""

from __future__ import annotations

import pytest

from app import analysis
from app.charts import render_exercise_chart
from app.kb_seed import STARTER_KB
from app.models import SetGroup, Workout
from app.store import SheetsStore
from app.tools import (
    ChartSpec,
    DescribeDataArgs,
    ExerciseStatsArgs,
    PersonalRecordsArgs,
    QueryHistoryArgs,
    ToolContext,
    _describe_data_handler,
    _exercise_stats_handler,
    _make_chart_handler,
    _personal_records_handler,
    _query_history_handler,
)
from tests.fake_sheets import FakeSpreadsheet


def _store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _seed(s: SheetsStore) -> None:
    # three squat sessions (progressing), plus two basketball games this month
    for i, (date, per_side, total) in enumerate(
        [("2026-06-01", 50, 145), ("2026-06-15", 55, 155), ("2026-07-01", 60, 165)], start=1
    ):
        wid = f"W000{i}"
        s.append_workout(
            Workout(workout_id=wid, date=date, type="strength", raw_text="x", logged_at="t")
        )
        s.append_sets(
            [
                SetGroup(
                    workout_id=wid,
                    exercise="Back Squat",
                    exercise_raw="squats",
                    grp=1,
                    sets=3,
                    reps=5,
                    per_side_lbs=per_side,
                    total_lbs=total,
                )
            ]
        )
    for i, date in enumerate([("2026-07-05"), ("2026-07-12")], start=4):
        s.append_workout(
            Workout(
                workout_id=f"W000{i}", date=date, type="basketball", raw_text="bball", logged_at="t"
            )
        )


def _ctx(s: SheetsStore) -> ToolContext:
    return ToolContext(store=s, kb=s.kb_entries(), user_text="")


def test_query_history_joins_date_resolves_alias_and_filters_type():
    s = _store()
    _seed(s)
    out = _query_history_handler(QueryHistoryArgs(exercise="squats"), _ctx(s))
    assert out["count"] == 3
    last = out["rows"][-1]  # sorted by date
    assert last["date"] == "2026-07-01" and last["per_side_lbs"] == 60 and last["total_lbs"] == 165
    # an alias resolves to the same canonical rows
    aliased = _query_history_handler(QueryHistoryArgs(exercise="squats both sides"), _ctx(s))
    assert aliased["count"] == 3
    # basketball sessions have no set rows, but session_count still finds them
    bball = _query_history_handler(QueryHistoryArgs(type="basketball"), _ctx(s))
    assert bball["count"] == 0 and bball["session_count"] == 2


def test_analysis_metrics_volume_top_set_pr_and_session_count():
    s = _store()
    _seed(s)
    rows = analysis.load_joined(s)
    vol = analysis.session_volume(rows, "Back Squat", s.kb_entries())
    assert list(vol) == ["2026-06-01", "2026-06-15", "2026-07-01"]
    assert vol["2026-07-01"] == 3 * 5 * 165  # progressing volume
    assert analysis.top_set(rows, "squats", s.kb_entries())["2026-07-01"] == 165
    pr = analysis.personal_record(rows, "Back Squat", s.kb_entries())
    assert pr["total_lbs"] == 165 and pr["reps"] == 5
    assert analysis.count_sessions(s, type_="basketball", month="2026-07") == 2
    assert analysis.count_sessions(s, type_="basketball", month="2026-06") == 0


def test_describe_data_overview_orients_the_agent():
    s = _store()
    _seed(s)
    out = _describe_data_handler(DescribeDataArgs(), _ctx(s))
    assert out["n_workouts"] == 5 and out["n_sets"] == 3
    assert out["date_range"] == {"first": "2026-06-01", "last": "2026-07-12"}
    squat = next(e for e in out["exercises"] if e["name"] == "Back Squat")
    assert squat["n_sessions"] == 3 and squat["last_date"] == "2026-07-01"
    assert {t["type"]: t["count"] for t in out["types"]} == {"strength": 3, "basketball": 2}
    assert out["last_workout"]["type"] == "basketball"
    assert out["last_workout"]["date"] == "2026-07-12"
    # `since` is a typed lower bound, never eval'd → only the July squat session remains
    recent = _describe_data_handler(DescribeDataArgs(since="2026-07-01"), _ctx(s))
    assert recent["n_sets"] == 1 and recent["n_workouts"] == 3


@pytest.mark.parametrize(
    "metric, expected_last",
    [
        ("volume", 3 * 5 * 165),  # sets·reps·total on the latest (heaviest) session
        ("top_set", 165),  # heaviest total_lbs on the latest session
        ("pr", 165),
        ("frequency", 1),  # one squat session in 2026-07 (two were in 2026-06)
    ],
)
def test_exercise_stats_routes_metric_and_reports_pr_and_trend(metric, expected_last):
    s = _store()
    _seed(s)
    out = _exercise_stats_handler(ExerciseStatsArgs(exercise="squats", metric=metric), _ctx(s))
    assert out["exercise"] == "Back Squat" and out["n_sessions"] == 3
    assert out["series"][-1]["value"] == expected_last
    assert out["pr"] == {"total_lbs": 165.0, "reps": 5.0, "date": "2026-07-01"}
    # progressing squats → a positive first→last delta on the weight-based metrics
    if metric in ("volume", "top_set", "pr"):
        assert out["trend"]["delta"] > 0


def test_personal_records_all_single_and_rep_specific():
    s = _store()
    _seed(s)
    # all tracked exercises, heaviest-first (only Back Squat has weighted sets here)
    allpr = _personal_records_handler(PersonalRecordsArgs(), _ctx(s))["records"]
    assert allpr and allpr[0]["exercise"] == "Back Squat" and allpr[0]["total_lbs"] == 165.0
    # one exercise via alias
    one = _personal_records_handler(PersonalRecordsArgs(exercise="squats"), _ctx(s))["records"]
    assert len(one) == 1 and one[0]["reps"] == 5.0
    # rep-specific: 5-rep PR exists, a 99-rep PR does not
    assert _personal_records_handler(PersonalRecordsArgs(exercise="squats", reps=5), _ctx(s))[
        "records"
    ]
    assert (
        _personal_records_handler(PersonalRecordsArgs(exercise="squats", reps=99), _ctx(s))[
            "records"
        ]
        == []
    )


def test_query_history_caps_rows_but_keeps_honest_counts():
    s = _store()
    _seed(s)
    out = _query_history_handler(QueryHistoryArgs(exercise="squats", limit=2), _ctx(s))
    assert out["count"] == 3 and len(out["rows"]) == 2  # count honest, rows capped
    assert out.get("truncated") is True and "narrow" in out["note"]


def test_make_chart_renders_inline_svg_for_both_metrics():
    s = _store()
    _seed(s)
    # volume metric via the tool handler → returns an inline SVG, no files on disk
    out = _make_chart_handler(ChartSpec(exercise="squats", metric="volume"), _ctx(s))
    assert out["svg"].startswith("<svg") and out["points"] == 3
    assert out["title"] == "squats — session volume"
    # top_set metric via the underlying renderer
    top = render_exercise_chart(s, "Back Squat", metric="top_set", kb=s.kb_entries())
    assert top["points"] == 3 and "<svg" in top["svg"]


def test_make_chart_no_data_reports_error():
    s = _store()
    out = _make_chart_handler(ChartSpec(exercise="squats"), _ctx(s))
    assert "error" in out
