"""Shared test guards.

The suite must never touch the network. `load_dotenv()` means a developer's real
`.env` is live during tests, and the confirm path now pushes to Strava — so
Strava is forced off for every test, and a test that wants it opts in explicitly.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import strava


@pytest.fixture(autouse=True)
def strava_off(monkeypatch):
    monkeypatch.setattr(
        strava,
        "settings",
        replace(
            strava.settings,
            strava_client_id=None,
            strava_client_secret=None,
            strava_refresh_token=None,
        ),
    )
