"""What gets checked. One dict per scenario — a new ability is a new row here.

  say      the message a user would actually type
  tool     the tool the model is expected to reach for (omit = plain answer)
  confirm  approve the confirm card it returns, so the write really lands
  edits    field edits applied to the card before ✅, as a user would
  adds     expected new rows per tab across the turn

They run in order against one seeded sheet, so a later scenario can act on what
an earlier one logged — but each starts a fresh conversation, so what's tested is
the message alone reaching the tool. Keep this to the essential flows — a write, a
read, a correction — and add a row for the ability you're building, not a variant
of one already here.
"""

SCENARIOS = [
    # Write path: a real message parses, the card commits, rows appear.
    {
        "say": "Squats 3x5 at 60lbs on each side, then 3x10 dumbbell curls with 25s",
        "tool": "log_workout",
        "confirm": True,
        "adds": {"workouts": 1, "sets": 2},
    },
    # Read path: a question routes to a read tool and is answered from the sheet.
    {"say": "what did I squat last time?", "tool": "query_history", "adds": {"workouts": 0}},
    # Correction path: fixing a logged workout edits in place, never duplicates.
    {
        "say": "that squat workout was actually on July 19th, fix it",
        "tool": "edit_workout",
        "confirm": True,
        "edits": {"date": "2026-07-19"},
        "adds": {"workouts": 0},
    },
]
