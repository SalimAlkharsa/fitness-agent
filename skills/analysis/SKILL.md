---
name: analysis
description: Definitions for analysing training history — volume, progress, PRs, and session frequency. Read before answering any question about progress, volume, personal records, or how often Salim did something, so the numbers are computed consistently.
---

# Analysing training history

Use these definitions so answers are consistent. The data you get from
`query_history` is set-group rows joined to their workout (date, type).

## Volume

- **Set-group volume** = `sets × reps × total_lbs`. Skip set-groups with no
  `total_lbs` (bodyweight/assisted) from weighted-volume sums, or note them
  separately as rep volume (`sets × reps`).
- **Session volume for an exercise** = sum of set-group volume for that exercise
  within one workout (`workout_id`).
- **Session volume overall** = sum across all exercises in the workout.

## Progress

- **Progress on an exercise** = the trend over time in either the top-set total
  (heaviest `total_lbs` in the session) or the session volume for that exercise.
  Prefer top-set total for strength questions, session volume for work-capacity
  questions. Order by workout `date`.
- State the direction plainly ("up ~15 lbs on your top squat over 6 weeks") and, if
  useful, cite the first and most recent data points.

## Personal records

- **PR for an exercise** = the maximum `total_lbs` ever logged, and note the rep
  count it happened at (a 1-rep max and a 10-rep max are different achievements).
  If asked for a rep-specific PR, filter to that rep count first.

## Frequency

- **Session frequency** = count of workouts (rows in the workouts tab) matching a
  `type` and/or date window. "How many times did I play basketball this month?" =
  count workouts with `type` = basketball whose `date` is in the current month.
  **Sport/cardio sessions have no set rows**, so count them from the sessions list
  (`query_history` returns `session_count`/`sessions`) or from `describe_data`'s
  session-type counts — never from set-group rows, which will show zero.

## Honesty

- If there isn't enough data (e.g. only one session for an exercise), say so rather
  than inventing a trend.
