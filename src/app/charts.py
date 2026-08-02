"""Chart rendering: pysvgchart → inline SVG.

Follows skills/charting/SKILL.md: date x-axis, lbs units, one exercise per chart,
a title. No files on disk — the SVG string is returned and rendered inline in the
chat thread (serverless has no shared disk; SVG dodges the 1 MB response limit).
"""

from __future__ import annotations

import datetime

import pysvgchart as psc

from app import analysis

# metric key -> (series function, y-axis label, title suffix)
_METRICS = {
    "volume": (analysis.session_volume, "Volume (lbs)", "session volume"),
    "top_set": (analysis.top_set, "Top set (lbs)", "top-set total"),
}


def _parse_date(d: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(d)
    except (TypeError, ValueError):
        return None


def render_exercise_chart(
    store, exercise: str, metric: str = "volume", title: str | None = None, kb=None
) -> dict:
    """Render a per-session line chart for one exercise. Returns {svg, title, points}."""
    fn, ylabel, suffix = _METRICS.get(metric, _METRICS["volume"])
    rows = analysis.load_joined(store)
    series = fn(rows, exercise, kb)
    points = [(_parse_date(d), v) for d, v in series.items()]
    points = [(d, v) for d, v in points if d is not None]
    if not points:
        return {"error": f"no data for '{exercise}'"}

    chart_title = title or f"{exercise} — {suffix}"
    chart = psc.SimpleLineChart(
        x_values=[d for d, _ in points],
        y_values=[[v for _, v in points]],
        y_names=[ylabel],
        y_zero=True,
        x_axis_title="Date",
        y_axis_title=ylabel,
        width=640,
        height=320,
        left_margin=70,
        right_margin=30,
    )
    return {"svg": chart.render(), "title": chart_title, "points": len(points)}
