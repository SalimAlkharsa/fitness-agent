---
name: charting
description: House conventions for workout charts (axes, labels, default metric, phone-legible styling). Read before calling make_chart so charts follow the standard look and each is captioned with a one-sentence trend.
---

# Charting

Charts are viewed on a phone, so keep them clean and legible.

## Default chart

- The default chart is a **line of session volume over time for one exercise**:
  x-axis = workout `date`, y-axis = session volume (`sum of sets × reps × total_lbs`
  per session for that exercise).
- **One exercise per chart** unless Salim explicitly asks to compare several.

## Conventions

- **X-axis is the date**, in chronological order. Format ticks as short dates
  (e.g. `Jul 10`); rotate/thin them so they don't overlap on a narrow screen.
- **Label the y-axis with units** — "Volume (lbs)" for volume, "lbs" for a weight
  trend like top-set total.
- Give every chart a title naming the exercise and the metric, e.g. "Back Squat —
  session volume".
- Prefer a simple line with visible markers at each data point. Avoid heavy grids,
  legends for single-series charts, and 3-D anything.

## Other useful charts (when asked)

- **Top-set total over time**: y-axis = heaviest `total_lbs` per session for the
  exercise. Good for "am I getting stronger?".
- **Weekly frequency**: bar chart of workout count per week by `type`.

## After making a chart

- Return the chart and add a one-sentence interpretation of the trend (up / flat /
  down, and roughly by how much).
