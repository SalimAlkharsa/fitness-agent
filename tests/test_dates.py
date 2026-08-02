"""Deterministic date resolution (app/dates.py).

The model returns training-era dates even when the prompt carries the right one,
so the code decides. One table covers the branches: undated, relative cues, and
an explicitly stated date (which stays with the parse).
"""

from __future__ import annotations

import datetime

import pytest

from app.dates import resolve_date

TODAY = datetime.date(2026, 7, 24)  # a Friday


@pytest.mark.parametrize(
    "text, parsed, expected",
    [
        # No date cue at all → today, NOT the model's guess (the reported bug:
        # an undated log came back as 2025-08-20).
        ("Squats 4x7 60lbs on each side\n3.5 real pull ups", "2025-08-20", "2026-07-24"),
        ("bench 3x5 135", None, "2026-07-24"),
        # Relative cues are computed here, ignoring whatever the model said.
        ("did legs yesterday", "2025-01-01", "2026-07-23"),
        ("squats today 3x5", "2025-01-01", "2026-07-24"),
        ("basketball tonight", None, "2026-07-24"),
        ("legs 3 days ago", "2025-01-01", "2026-07-21"),
        ("chest 2 weeks ago", None, "2026-07-10"),
        # Weekday → most recent past occurrence (Wed before this Friday).
        ("squats wednesday 4x7", "2025-08-04", "2026-07-22"),
        # Said on a Friday, "friday" means today; "last friday" means the previous one.
        ("deadlifts friday", None, "2026-07-24"),
        ("deadlifts last friday", None, "2026-07-17"),
        # An explicitly stated date is the one thing the model actually read off
        # the text, so its parse is kept.
        ("workout on 2026-06-01: squats 3x5", "2026-06-01", "2026-06-01"),
        ("aug 4 chest day", "2026-08-04", "2026-08-04"),
    ],
)
def test_resolve_date(text, parsed, expected):
    assert resolve_date(text, TODAY, parsed) == expected
