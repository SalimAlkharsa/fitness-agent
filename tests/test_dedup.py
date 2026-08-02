"""Live-logging dedup tests — fake store, no network.

Flow-first: commit a real workout, then check the publish-path detector against a
literal re-send, a re-typed variant (different wording + corrected weight), and a
genuinely different session. Plus a table-driven check that the semantic signature
ignores weight and wording but not the session's identity.
"""

from __future__ import annotations

import pytest

from app import dedup
from app.dedup import workout_signature
from app.kb_seed import STARTER_KB
from app.logflow import commit_workout
from app.models import ParsedSetGroup, ParsedWorkout
from app.store import SheetsStore
from tests.fake_sheets import FakeSpreadsheet


def _store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _pw(
    date="2026-07-14",
    type_="strength",
    groups=(("Back Squat", 3, 5, 165.0),),
    raw="squats 3x5",
) -> ParsedWorkout:
    return ParsedWorkout(
        date=date,
        type=type_,
        raw_text=raw,
        sets=[
            ParsedSetGroup(exercise=e, exercise_raw=e, grp=i + 1, sets=s, reps=r, total_lbs=t)
            for i, (e, s, r, t) in enumerate(groups)
        ],
    )


def test_find_duplicate_catches_resend_and_retype_not_a_distinct_session():
    s = _store()
    kb = s.kb_entries()
    original = ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="Squats 3x5 60 each side",
        sets=[
            ParsedSetGroup(
                exercise="Back Squat",
                exercise_raw="squats",
                grp=1,
                sets=3,
                reps=5,
                per_side_lbs=60,
                total_lbs=165,
            )
        ],
    )
    commit_workout(s, original)

    # 1) literal re-send (whitespace/case variant of the same text) → EXACT
    resend = ParsedWorkout(
        date="2026-07-14", type="strength", raw_text="squats  3X5  60 EACH side", sets=original.sets
    )
    exact = dedup.find_duplicate(resend, s, kb)
    assert exact is not None and exact.kind == "exact" and exact.workout_id == "W0001"

    # 2) re-typed, weight corrected, alias name → SEMANTIC (weight is excluded)
    retype = ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="did back squats, three sets of five",
        sets=[
            ParsedSetGroup(
                exercise="squats",  # alias resolves to Back Squat
                exercise_raw="squats",
                grp=1,
                sets=3,
                reps=5,
                per_side_lbs=75,
                total_lbs=195,  # different weight — still flagged
            )
        ],
    )
    semantic = dedup.find_duplicate(retype, s, kb)
    assert semantic is not None and semantic.kind == "semantic" and semantic.workout_id == "W0001"

    # 3) genuinely different session (another day) → no match, nothing blocked
    other = ParsedWorkout(
        date="2026-07-20",
        type="strength",
        raw_text="squats again next week",
        sets=[
            ParsedSetGroup(
                exercise="Back Squat", exercise_raw="squats", grp=1, sets=3, reps=5, total_lbs=165
            )
        ],
    )
    assert dedup.find_duplicate(other, s, kb) is None


@pytest.mark.parametrize(
    "a, b, equal",
    [
        # same session, different weight AND wording → SAME signature (weight excluded)
        (
            _pw(groups=(("Back Squat", 3, 5, 165.0),), raw="squats 3x5 60 each side"),
            _pw(groups=(("Back Squat", 3, 5, 175.0),), raw="did 3 sets of 5 back squats"),
            True,
        ),
        (_pw(groups=(("Back Squat", 3, 5, 165),)), _pw(groups=(("Back Squat", 3, 6, 165),)), False),
        (_pw(date="2026-07-14"), _pw(date="2026-07-15"), False),
        (_pw(type_="strength"), _pw(type_="basketball"), False),
        (
            _pw(groups=(("Back Squat", 3, 5, 165),)),
            _pw(groups=(("Bench Press", 3, 5, 165),)),
            False,
        ),
    ],
)
def test_workout_signature_ignores_weight_and_wording_only(a, b, equal):
    assert (workout_signature(a) == workout_signature(b)) is equal
