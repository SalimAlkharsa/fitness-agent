"""Starter exercise_kb seeded at bootstrap.

The three entries in DESIGN.md's schema table are included verbatim; the rest are
the exercises appearing across evals/golden/README.md, so live logging and backfill
start with sensible canonical names and base weights. The KB is editable at runtime
(update_kb / the confirm card) — this is a head start, not a fixed list.

Base weights: olympic bar 45, smith machine ~25, hex/trap bar ~55; stack, cable,
bodyweight and assisted movements carry no base (0).
"""

from __future__ import annotations

from app.models import Equipment, KBEntry

STARTER_KB: list[KBEntry] = [
    # --- from DESIGN.md ---
    KBEntry(
        canonical="Back Squat",
        aliases=["squats", "squats both sides", "squats on both sides"],
        equipment=Equipment.barbell,
        base_lbs=45,
    ),
    KBEntry(
        canonical="Smith Calf Raise",
        aliases=[
            "calf raises",
            "calf raises on smith machine",
            "calf raises smith elevated",
            "calf raises on smith machine elevated",
        ],
        equipment=Equipment.smith,
        base_lbs=25,
        notes="elevated variant = same exercise",
    ),
    KBEntry(
        canonical="Hip Abduction (Out)",
        aliases=["hip abductor outer"],
        equipment=Equipment.stack,
        base_lbs=0,
        notes="total = stated number",
    ),
    # --- golden-set exercises ---
    KBEntry(
        canonical="Roman Glute Swing",
        aliases=["roman glute swing"],
        equipment=Equipment.stack,
        base_lbs=0,
    ),
    KBEntry(
        canonical="Australian Pull Up",
        aliases=["australian pull ups", "australian pull up"],
        equipment=Equipment.bodyweight,
        base_lbs=0,
    ),
    KBEntry(
        canonical="Bar Row",
        aliases=["bar row", "bar rows"],
        equipment=Equipment.barbell,
        base_lbs=45,
    ),
    KBEntry(
        canonical="Pull Up",
        aliases=["real pull ups", "real pull up", "pull ups"],
        equipment=Equipment.bodyweight,
        base_lbs=0,
        notes="fractional reps allowed (e.g. 2.5)",
    ),
    KBEntry(
        canonical="Negative Pull Up",
        aliases=["negative pull ups", "negative pull up"],
        equipment=Equipment.bodyweight,
        base_lbs=0,
        notes="separate exercise from Pull Up",
    ),
    KBEntry(
        canonical="Hex Deadlift",
        aliases=["deadlift hex bar", "hex bar deadlift", "trap bar deadlift"],
        equipment=Equipment.hex,
        base_lbs=55,
    ),
    KBEntry(
        canonical="Soleus Raise",
        aliases=["soleus raise on smith machine", "soleus raise"],
        equipment=Equipment.smith,
        base_lbs=25,
    ),
    KBEntry(
        canonical="Bench Press",
        aliases=["bench"],
        equipment=Equipment.barbell,
        base_lbs=45,
    ),
    KBEntry(
        canonical="Close-Grip Bench Press",
        aliases=["close grip bench", "bench close grip"],
        equipment=Equipment.barbell,
        base_lbs=45,
        notes="distinct from Bench Press",
    ),
    KBEntry(
        canonical="Assisted Elevated Leg Dips",
        aliases=["bench assisted elevated leg dips"],
        equipment=Equipment.bodyweight,
        base_lbs=0,
    ),
    KBEntry(
        canonical="Tricep Push Down",
        aliases=["tricep push downs", "tricep pushdowns"],
        equipment=Equipment.cable,
        base_lbs=0,
    ),
    KBEntry(
        canonical="Cable Push Down",
        aliases=["cable push downs", "cable pushdowns"],
        equipment=Equipment.cable,
        base_lbs=0,
    ),
    KBEntry(
        canonical="Shoulder Press",
        aliases=["shoulder press"],
        equipment=Equipment.barbell,
        base_lbs=45,
    ),
    KBEntry(
        canonical="Dips",
        aliases=["dips"],
        equipment=Equipment.assisted,
        base_lbs=0,
        notes="assist weight → assist_lbs",
    ),
]
