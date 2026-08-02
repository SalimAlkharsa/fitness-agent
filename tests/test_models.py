"""Domain-model tests: the compute_total math and the schema surface.

The structural parsing quirks (fractional reps, a second set-group, assist weight,
a basketball session with no sets) are proven end-to-end by the golden-fixture
commit flows in test_logflow / test_backfill, so they aren't re-asserted here. This
file guards the two things nothing else does: the pure compute_total branches (one
row per equipment type) and the JSON schema the LLM is handed.
"""

import json

import pytest

from app import SCHEMA_VERSION as PKG_SCHEMA_VERSION
from app.models import (
    SCHEMA_VERSION,
    Equipment,
    KBEntry,
    ParsedSetGroup,
    ParsedWorkout,
    compute_total,
)

# KB fixtures mirroring DESIGN.md's starter exercise_kb — one per equipment type.
BACK_SQUAT = KBEntry(canonical="Back Squat", equipment=Equipment.barbell, base_lbs=45)
SMITH_CALF = KBEntry(canonical="Smith Calf Raise", equipment=Equipment.smith, base_lbs=25)
HEX_DEADLIFT = KBEntry(canonical="Hex Deadlift", equipment=Equipment.hex, base_lbs=55)
HIP_ABDUCTION = KBEntry(canonical="Hip Abduction (Out)", equipment=Equipment.stack, base_lbs=0)
CABLE_PUSHDOWN = KBEntry(canonical="Cable Push Down", equipment=Equipment.cable, base_lbs=0)
AUSSIE_PULLUP = KBEntry(canonical="Australian Pull Up", equipment=Equipment.bodyweight, base_lbs=0)
DIPS = KBEntry(canonical="Dips", equipment=Equipment.assisted, base_lbs=0)


@pytest.mark.parametrize(
    "per_side, kb, expected",
    [
        (60, BACK_SQUAT, 165),  # barbell: 60/side + 45 bar
        (75, SMITH_CALF, 175),  # smith: 75/side + 25 base
        (65, HEX_DEADLIFT, 185),  # hex: base 55 from KB, not olympic 45
        (145, HIP_ABDUCTION, 145),  # stack: total verbatim, no per-side math
        (10, CABLE_PUSHDOWN, 10),  # cable: passthrough
        (None, AUSSIE_PULLUP, None),  # bodyweight: no total
        (None, DIPS, None),  # assisted: weight lives on assist_lbs, not total
        (None, BACK_SQUAT, None),  # barbell with no stated weight → don't invent
    ],
)
def test_compute_total(per_side, kb, expected):
    assert compute_total(per_side, kb) == expected


def test_reps_accept_fractional_via_string_coercion():
    # The LLM may emit "2.5" for "2.5 Real Pull Ups"; pydantic coerces string→float.
    sg = ParsedSetGroup(exercise="Pull Up", exercise_raw="Real Pull Ups", sets=1, reps="2.5")
    assert sg.reps == pytest.approx(2.5)


def test_parsed_workout_json_schema_is_valid():
    # This schema is handed to the model in json/native mode — it must serialize
    # and carry the nested set-group definition.
    schema = ParsedWorkout.model_json_schema()
    json.dumps(schema)
    assert schema["type"] == "object"
    assert "sets" in schema["properties"]
    assert "$defs" in schema and "ParsedSetGroup" in schema["$defs"]


def test_schema_constants_and_equipment_enum():
    assert SCHEMA_VERSION == 1 == PKG_SCHEMA_VERSION  # migrations key off this
    assert {e.value for e in Equipment} == {
        "barbell",
        "smith",
        "hex",
        "stack",
        "cable",
        "bodyweight",
        "assisted",
    }
