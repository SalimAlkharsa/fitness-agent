---
name: parsing-salims-logs
description: Rules for parsing Salim's shorthand workout messages into structured workouts and set-groups — per-side vs total weight math, set-group (grp) grouping, exercise naming against the KB, dates/times, and multi-workout messages. Read before parsing any message that logs a workout.
---

# Parsing Salim's workout logs

Salim writes workouts in shorthand that varies a lot. Your job is to turn a
message into structured workouts + set-groups. Read these rules carefully — they
encode how *he* writes, not generic gym notation.

## Weight math (this is where mistakes cost the most)

- **"on each side" / "on both sides" / "in each side" / a trailing "s" on a number
  ("15s") all mean PER SIDE.** Put that number in `per_side_lbs`. Then
  `total_lbs = per_side_lbs * 2 + the exercise's base_lbs from the knowledge base`.
  - Olympic barbell base is 45, smith machine ~25, hex/trap bar varies — **always
    take the base from the KB, never assume 45**. A hex-bar deadlift at 65/side is
    `65*2 + (hex base from KB)`, not `65*2 + 45`.
- **Stack and cable machines: the number stated IS the total.** Put it in
  `total_lbs`, leave `per_side_lbs` empty. No doubling. "Hip abductor … at 145lbs"
  → total 145. "Cable push downs … 15lbs" → total 15.
- **Bodyweight moves** (australian pull ups, real pull ups, dips without added
  plates): both `per_side_lbs` and `total_lbs` empty.
- **Assisted moves** (assisted dips/pull-ups): the assist number goes in
  `assist_lbs`, and `total_lbs` stays empty. "Dips 4x8 95lbs assist" → assist 95.
- **If no weight is stated, leave the weights empty. Never invent a number.**
  "Tricep push downs 3x10" with no weight → weights empty.

## Sets, reps, and set-groups (the `grp` field)

- `AxB` = A sets of B reps. "3x8" = 3 sets, 8 reps. "5 sets of 6" = 5 sets, 6 reps.
- Salim sometimes puts reps last: "3x60 lbs on each side squats x7" = 3 sets,
  60/side, **7 reps** (the trailing "x7"). "20x85lbs … 3 sts" = **3 sets of 20
  reps** at 85 (the "3 sts" is the set count; 20 is reps).
- Reps can be **fractional**: "3.5 Real Pull Ups" → reps 2.5. Keep the decimal.
- **A `grp` (set-group) is one block of identical sets of one exercise.** Number
  them 1, 2, 3… *within each exercise* in the order written. Start every exercise's
  first block at `grp` 1.
- **A new block of the SAME exercise = a new set-group (grp increments).** Triggers:
  - the word **"then"**: "Bar rows 3x12 20 lbs each side **then** 3x15 20 lbs each
    side" → Bar Row grp 1 (3x12) and grp 2 (3x15).
  - the same exercise repeated on another line with different numbers: "3x60 … squats
    x7" / "1x30 … squats x8" → Back Squat grp 1 and grp 2.
  - prose "1 set … then 3 sets …": soleus "1 set 30 each side 15 reps then 3 sets 60
    each side 15 reps" → Soleus Raise grp 1 (1x15@30/side) and grp 2 (3x15@60/side).
- Two DIFFERENT exercises are never the same set-group — they each start at grp 1.

## Naming: match the knowledge base first

- Before inventing a new exercise, **check the KB aliases** for a canonical match.
  "squats", "squats on both sides" → Back Squat. "calf raises on smith machine
  elevated" → Smith Calf Raise (the "elevated" variant is the same exercise).
- Put the canonical name in `exercise` and Salim's exact words in `exercise_raw`.
- **Negatives are a separate exercise** from the strict movement: "4 negative pull
  ups" is Negative Pull Up, not Pull Up. A line like "3.5 Real Pull Ups 4 negative
  pull ups" is TWO exercises: Pull Up (3.5 reps) and Negative Pull Up (4 reps).
- **"close grip" bench is a distinct exercise** (Close-Grip Bench Press), not Bench
  Press.
- If nothing in the KB matches, use your best canonical name — the confirm card will
  ask Salim whether it's new or an alias of something he already has.

## Dates and times

- Resolve relative dates against the current date/time you are given. "Yesterday 5
  to 7 pm" → the workout `date` is yesterday, `start` 17:00, `end` 19:00. "today
  2:30 to 4:20 pm" → today, 14:30–16:20.
- If no date is stated at all, use the current date.
- Times are 24-hour HH:MM. "5 to 7 pm" → 17:00 / 19:00.

## One message can be several workouts

- A lifting session and a sport are two separate workouts. "Bench … tricep push
  downs … then basketball today 2:30 to 4:20" → workout 1 (strength, with sets) and
  workout 2 (`type` basketball, **no sets**, just start/end).
- Basketball / cardio / sports = a workout with `type` set accordingly and an empty
  sets list. Don't invent set-groups for them.

## Worked example

Message: `Calf raises on smith machine elevated / 20x 75lbs on each side / 3x20x 85
lbs on each side`
→ Smith Calf Raise, grp 1 = 1 set × 20 reps @ 75/side (total 75*2+25 = 175);
grp 2 = 3 sets × 20 reps @ 85/side (total 85*2+25 = 195).
