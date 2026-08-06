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

TOKEN = {"access_token": "at", "refresh_token": "rt"}


def _settings(monkeypatch, **overrides):
    monkeypatch.setattr(strava, "settings", replace(strava.settings, **overrides))


@pytest.fixture
def creds(monkeypatch):
    _settings(
        monkeypatch,
        strava_client_id="1",
        strava_client_secret="s",
        strava_refresh_token="rt",
    )


def _stub(monkeypatch, posts: list, activities: list | None = None, create_status: int = 201):
    """Record POSTs; answer token/create/list from canned data."""

    def post(url, data=None, headers=None, timeout=None):
        posts.append((url, data, headers))
        if url == strava.TOKEN_URL:
            return httpx.Response(200, json=TOKEN)
        return httpx.Response(create_status, json={"id": 987} if create_status < 300 else {})

    def get(url, params=None, headers=None, timeout=None):
        page = (params or {}).get("page", 1)
        return httpx.Response(200, json=activities if page == 1 else [])

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)


def test_push_sends_a_manual_activity(monkeypatch, creds):
    posts: list = []
    _stub(monkeypatch, posts)
    workout = Workout(
        workout_id="W0001",
        date="2026-07-14",
        type="strength",
        start="17:00",
        end="18:30",
        notes="lower body",
        raw_text="Squats 3x5",
        logged_at="2026-07-14T18:40",
    )

    assert strava.push_workout(workout, "Logged W0001: Back Squat 3x5@165") == 987

    (token_url, token_body, _), (create_url, payload, headers) = posts
    assert token_url == strava.TOKEN_URL and token_body["grant_type"] == "refresh_token"
    assert create_url == f"{strava.API}/activities"
    assert headers["Authorization"] == "Bearer at"
    assert payload["sport_type"] == "WeightTraining"
    assert payload["start_date_local"] == "2026-07-14T17:00:00"
    assert payload["elapsed_time"] == 90 * 60
    assert payload["name"] == "lower body"
    assert "Back Squat" in payload["description"]


def test_push_quietly_survives_a_strava_failure(monkeypatch, creds):
    _stub(monkeypatch, [], create_status=500)
    workout = Workout(
        workout_id="W0002",
        date="2026-07-14",
        type="basketball",
        raw_text="hoops",
        logged_at="2026-07-14T20:00",
    )
    assert "✗" in strava.push_quietly(workout)


def test_push_quietly_is_a_no_op_when_unconfigured(monkeypatch):
    _settings(monkeypatch, strava_client_id=None)
    workout = Workout(
        workout_id="W0003", date="2026-07-14", type="run", raw_text="run", logged_at="x"
    )
    assert strava.push_quietly(workout) == ""


def test_backfill_maps_runs_and_commits_them_once(monkeypatch, creds):
    activities = [
        {
            "id": 555,
            "name": "Morning Run",
            "sport_type": "Run",
            "distance": 8046.72,  # 5 miles
            "moving_time": 2400,
            "elapsed_time": 2500,
            "start_date_local": "2026-07-04T06:30:00Z",
        },
        {"id": 556, "sport_type": "Ride", "start_date_local": "2026-07-05T06:00:00Z"},
    ]
    _stub(monkeypatch, [], activities=activities)

    runs = strava.fetch_runs()
    assert len(runs) == 1  # the ride is filtered out
    run = runs[0]
    assert (run.date, run.type, run.start, run.end) == ("2026-07-04", "run", "06:30", "07:11")
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
