"""Strava sync: the push flow and the run-backfill flow, with httpx stubbed."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app import strava
from app.kb_seed import STARTER_KB
from app.logflow import commit_workout
from app.models import Workout
from app.store import SheetsStore
from tests.fake_sheets import FakeSpreadsheet


def _settings(monkeypatch, **overrides):
    monkeypatch.setattr(strava, "settings", replace(strava.settings, **overrides))


@pytest.fixture
def creds(monkeypatch, strava_off):
    """Opt back in to Strava (conftest forces it off for every test)."""
    _settings(
        monkeypatch, strava_client_id="1", strava_client_secret="s", strava_refresh_token="rt"
    )


def _stub(monkeypatch, calls: list, activities=(), status: int = 201, rotate_to: str = "rt"):
    """Record every call; answer token / create / list from canned data."""

    def request(method, url, headers=None, timeout=None, data=None, params=None):
        calls.append((url, data, headers))
        if url == strava.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "at", "refresh_token": rotate_to})
        if method == "GET":
            page = (params or {}).get("page", 1)
            return httpx.Response(200, json=list(activities)[(page - 1) * 2 : page * 2])
        return httpx.Response(status, json={"id": 987})

    monkeypatch.setattr(strava, "_PAGE", 2)  # so two short pages exercise pagination
    monkeypatch.setattr(httpx, "request", request)


def _workout(**kw) -> Workout:
    return Workout(
        workout_id="W0001", date="2026-07-14", type="strength", raw_text="x", logged_at="x", **kw
    )


def test_push_sends_a_manual_activity(monkeypatch, creds):
    calls: list = []
    _stub(monkeypatch, calls)
    workout = _workout(start="17:00", end="18:30", notes="lower body")

    assert strava.push_workout(workout, "Logged W0001: Back Squat 3x5@165") == 987

    (token_url, token_body, _), (create_url, payload, headers) = calls
    assert token_url == strava.TOKEN_URL and token_body["grant_type"] == "refresh_token"
    assert create_url == f"{strava.API}/activities"
    assert headers["Authorization"] == "Bearer at"
    assert payload["sport_type"] == "WeightTraining"
    assert payload["start_date_local"] == "2026-07-14T17:00:00"
    assert payload["elapsed_time"] == 90 * 60
    assert payload["name"] == "lower body"
    assert "Back Squat" in payload["description"]


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("17:00", "18:30", 90 * 60),
        ("23:00", "00:30", 90 * 60),  # rolls past midnight
        ("7:30", "9:00", 90 * 60),  # free-text card, no leading zero
        (None, "18:30", strava.DEFAULT_ELAPSED),  # nothing validates these upstream
        ("5pm", "18:30", strava.DEFAULT_ELAPSED),
        ("17:00", "17:00", strava.DEFAULT_ELAPSED),
    ],
)
def test_elapsed_time(start, end, expected):
    assert strava._elapsed("2026-07-14", start, end) == expected


def test_push_quietly_reports_failure_and_stays_silent_when_off(monkeypatch, creds):
    _stub(monkeypatch, [], status=500)
    assert "✗" in strava.push_quietly(_workout())
    _settings(monkeypatch, strava_client_id=None)
    assert strava.push_quietly(_workout()) == ""


def test_rotated_refresh_token_is_persisted_and_reused(monkeypatch, creds):
    """Strava invalidates the old token at once, so the new one must outlive the call."""
    calls: list = []
    _stub(monkeypatch, calls, rotate_to="rt2")
    store = SheetsStore(FakeSpreadsheet())
    store.bootstrap(kb_seed=STARTER_KB)

    strava._access_token(store)
    assert store.meta_get(strava.META_TOKEN_KEY) == "rt2"

    strava._access_token(store)  # the sheet's copy wins over the stale .env one
    assert calls[-1][1]["refresh_token"] == "rt2"


def _activity(id_, sport="Run", **kw) -> dict:
    return {
        "id": id_,
        "name": "Morning Run",
        "sport_type": sport,
        "distance": 8046.72,  # 5 miles
        "moving_time": 2400,
        "elapsed_time": 2500,
        "start_date_local": "2026-07-04T06:30:00Z",
        **kw,
    }


def test_backfill_maps_runs_and_commits_them_once(monkeypatch, creds):
    activities = [
        _activity(555),
        _activity(556, sport="Ride"),
        # A run we pushed ourselves — re-importing it would duplicate the session.
        _activity(557, manual=True),
        _activity(558, sport="TrailRun", start_date_local="2026-07-07T06:30:00Z"),
    ]
    _stub(monkeypatch, [], activities=activities)  # _PAGE=2 → two pages

    runs = strava.fetch_runs()
    # the ride and our own manual echo are out; a trail run counts as a run
    assert [r.date for r in runs] == ["2026-07-04", "2026-07-07"]
    run = runs[0]
    assert (run.type, run.start, run.end) == ("run", "06:30", "07:11")
    assert run.notes == "5.00 mi in 40:00, 8:00/mi"
    assert run.raw_text.startswith("strava:555 Morning Run")

    store = SheetsStore(FakeSpreadsheet())
    store.bootstrap(kb_seed=STARTER_KB)
    workout, _summary = commit_workout(store, run)
    row = store.read_workouts()[0]
    assert (row.workout_id, row.type, row.date) == (workout.workout_id, "run", "2026-07-04")
    assert row.raw_text == run.raw_text

    # Re-running backfill sees the same raw_text and skips it.
    from app.dedup import committed_hashes, raw_hash

    assert raw_hash(strava.fetch_runs()[0].raw_text) in committed_hashes(store)
