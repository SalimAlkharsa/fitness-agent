"""Pure-Python analysis helpers (no pandas — 48 MB serverless bundle limit).

Loaded set/workout rows → joined dicts and the derived series the agent charts and
reasons over. Definitions follow skills/analysis/SKILL.md: volume = sets·reps·total_lbs.
These back the closed menu of typed read tools (describe_data, exercise_stats,
personal_records, query_history) and make_chart — the agent never runs code.

Injection-safe boundary: every filter here is a typed comparison (equality / <= on
plain values) or a KB dict-lookup via resolve_exercise. No caller-supplied string
is ever eval'd or formatted into a query — that is what makes having no code-exec
tool an actual security win, so keep it that way.
"""

from __future__ import annotations

from collections import defaultdict

from app.prompt import resolve_exercise


def load_joined(store) -> list[dict]:
    """One dict per set-group, joined to its workout's date and type, + volume."""
    workouts = {w.workout_id: w for w in store.read_workouts()}
    rows: list[dict] = []
    for s in store.read_sets():
        row = s.model_dump()
        w = workouts.get(s.workout_id)
        row["date"] = w.date if w else None
        row["type"] = w.type if w else None
        row["volume"] = s.sets * s.reps * (s.total_lbs or 0)
        rows.append(row)
    return rows


def _canon(rows: list[dict], exercise: str, kb) -> list[dict]:
    """Filter to one exercise, matched case-insensitively via the KB canonical."""
    canonical = resolve_exercise(exercise, kb) if kb else None
    target = (canonical or exercise).lower()
    return [r for r in rows if (r["exercise"] or "").lower() == target]


def _by_date(rows: list[dict], value, combine) -> dict[str, float]:
    """Per-date aggregate over rows (sorted by date). value(row) may return None."""
    acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = value(r)
        if r["date"] is not None and v is not None:
            acc[r["date"]].append(v)
    return {d: combine(vs) for d, vs in sorted(acc.items())}


def session_volume(rows: list[dict], exercise: str, kb=None) -> dict[str, float]:
    """Total volume per session date for one exercise."""
    return _by_date(_canon(rows, exercise, kb), lambda r: r["volume"], sum)


def top_set(rows: list[dict], exercise: str, kb=None) -> dict[str, float]:
    """Heaviest total_lbs per session date for one exercise."""
    return _by_date(_canon(rows, exercise, kb), lambda r: r["total_lbs"], max)


def _pr_of(rows: list[dict]) -> dict | None:
    """Max total_lbs row → {total_lbs, reps, date}, or None if nothing weighted."""
    weighted = [r for r in rows if r["total_lbs"] is not None]
    if not weighted:
        return None
    r = max(weighted, key=lambda x: x["total_lbs"])
    return {"total_lbs": float(r["total_lbs"]), "reps": float(r["reps"]), "date": r["date"]}


def personal_record(rows: list[dict], exercise: str, kb=None) -> dict | None:
    """Max total_lbs ever for an exercise, with the rep count it happened at."""
    return _pr_of(_canon(rows, exercise, kb))


def count_sessions(store, type_: str | None = None, month: str | None = None) -> int:
    """Count workouts, optionally filtered by type and/or YYYY-MM month prefix."""
    n = 0
    for w in store.read_workouts():
        if type_ and w.type.lower() != type_.lower():
            continue
        if month and not w.date.startswith(month):
            continue
        n += 1
    return n


# Cap describe()'s exercise list so the overview stays a summary, not a full dump.
DESCRIBE_MAX_EXERCISES = 40


def describe(store, since: str | None = None) -> dict:
    """A compact overview so the agent can orient before it queries.

    Counts, date range, the most-trained exercises (by distinct sessions) with
    their last date, session types, and the latest workout. `since` (YYYY-MM-DD,
    inclusive) is a typed lower-bound comparison on the date — never eval'd.
    """
    rows = load_joined(store)
    workouts = [w.model_dump() for w in store.read_workouts()]
    if since:
        rows = [r for r in rows if r["date"] and r["date"] >= since]
        workouts = [w for w in workouts if w["date"] >= since]

    dates = sorted(w["date"] for w in workouts if w["date"])
    per_ex: dict[str, dict] = {}
    for r in rows:
        e = per_ex.setdefault(r["exercise"], {"wids": set(), "last_date": ""})
        e["wids"].add(r["workout_id"])
        e["last_date"] = max(e["last_date"], r["date"] or "")
    exercises = [
        {"name": name, "n_sessions": len(e["wids"]), "last_date": e["last_date"]}
        for name, e in sorted(
            per_ex.items(), key=lambda kv: (len(kv[1]["wids"]), kv[1]["last_date"]), reverse=True
        )[:DESCRIBE_MAX_EXERCISES]
    ]

    type_counts: dict[str, int] = defaultdict(int)
    for w in workouts:
        type_counts[w["type"]] += 1
    types = [
        {"type": t, "count": c}
        for t, c in sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]

    last_workout = None
    if workouts:
        lw = max(workouts, key=lambda w: w["date"])
        last_workout = {"workout_id": lw["workout_id"], "date": lw["date"], "type": lw["type"]}

    return {
        "n_workouts": len(workouts),
        "n_sets": len(rows),
        "date_range": {"first": dates[0], "last": dates[-1]} if dates else None,
        "exercises": exercises,
        "types": types,
        "last_workout": last_workout,
    }


def _apply_dates(rows: list[dict], date_from: str | None, date_to: str | None) -> list[dict]:
    """Inclusive typed date-window filter (string comparison on YYYY-MM-DD)."""
    if date_from:
        rows = [r for r in rows if r["date"] and r["date"] >= date_from]
    if date_to:
        rows = [r for r in rows if r["date"] and r["date"] <= date_to]
    return rows


def exercise_stats(
    rows: list[dict],
    exercise: str,
    kb=None,
    metric: str = "volume",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Per-exercise analytics for ONE exercise. metric picks the per-date series:
    'volume' (sets·reps·total), 'top_set'/'pr' (heaviest total_lbs per date), or
    'frequency' (sessions/month). Always returns the all-time PR and a first→last
    trend delta over the series.
    """
    canonical = (resolve_exercise(exercise, kb) if kb else None) or exercise
    sub = _apply_dates(_canon(rows, exercise, kb), date_from, date_to)
    if not sub:
        return {
            "exercise": canonical,
            "metric": metric,
            "n_sessions": 0,
            "series": [],
            "pr": None,
            "trend": None,
        }

    pr = _pr_of(sub)

    if metric == "frequency":
        by_month: dict[str, set] = defaultdict(set)
        for r in sub:
            by_month[r["date"][:7]].add(r["workout_id"])
        s = {m: len(wids) for m, wids in sorted(by_month.items())}
    elif metric in ("top_set", "pr"):
        s = _by_date(sub, lambda r: r["total_lbs"], max)
    else:  # volume (default)
        s = _by_date(sub, lambda r: r["volume"], sum)
    series = [{"date": d, "value": float(v)} for d, v in s.items()]

    trend = None
    if len(series) >= 2:
        first, last = series[0]["value"], series[-1]["value"]
        trend = {
            "first": first,
            "last": last,
            "delta": last - first,
            "pct": None if first == 0 else round((last - first) / first * 100, 1),
        }

    return {
        "exercise": canonical,
        "metric": metric,
        "n_sessions": len({r["workout_id"] for r in sub}),
        "series": series,
        "pr": pr,
        "trend": trend,
    }


def all_personal_records(
    rows: list[dict], kb=None, exercise: str | None = None, reps: float | None = None
) -> list[dict]:
    """PRs (max total_lbs, with the rep count) across every tracked exercise, or a
    single exercise if given, sorted heaviest-first. `reps` filters to a rep-specific
    PR. Exists so the model can sweep all exercises without blind-looping
    exercise_stats over every name.
    """
    if not rows:
        return []
    if exercise:
        names = [(resolve_exercise(exercise, kb) if kb else None) or exercise]
    else:
        names = sorted({r["exercise"] for r in rows if r["exercise"]})

    out: list[dict] = []
    for name in names:
        sub = [
            r
            for r in rows
            if (r["exercise"] or "").lower() == name.lower()
            and r["total_lbs"] is not None
            and (reps is None or r["reps"] == reps)
        ]
        pr = _pr_of(sub)
        if pr is not None:
            out.append({"exercise": name, **pr})
    out.sort(key=lambda d: d["total_lbs"], reverse=True)
    return out
