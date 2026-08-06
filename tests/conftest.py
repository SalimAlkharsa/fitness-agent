"""Shared test guards."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import strava

_OFF = dict(strava_client_id=None, strava_client_secret=None, strava_refresh_token=None)


@pytest.fixture(autouse=True)
def strava_off(monkeypatch):
    """`load_dotenv()` makes a real .env live in tests and /confirm pushes to
    Strava, so the suite would post to the real feed. Opt in explicitly instead."""
    monkeypatch.setattr(strava, "settings", replace(strava.settings, **_OFF))
