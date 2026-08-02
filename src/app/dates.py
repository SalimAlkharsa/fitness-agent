"""Deterministic workout-date resolution.

The model cannot be trusted with dates: given a correct "current date" in the
prompt AND the parsing rule "if no date is stated, use the current date", the
free-tier model still answers from its training era (observed: an undated message
parsed to 2025-08-20 while the prompt said 2026-07-24). Dates are cheap to compute
and expensive to get wrong — one filed under the wrong year corrupts every trend —
so the code decides, not the LLM:

- no date cue in the message  → today (the overwhelmingly common live-logging case)
- a relative cue we understand → computed here ("yesterday", "monday", "3 days ago")
- an explicitly stated date    → the model's parse is kept (it read the actual text)

Only the live agent path uses this. Backfill keeps its own handling, where an
unrecoverable date stays flagged rather than silently becoming today.
"""

from __future__ import annotations

import datetime
import re

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTHS = (
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|"
    r"aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?"
)

# Anything that could plausibly be the user stating WHEN the workout happened.
# Deliberately broad: a false positive just means we trust the model's date (the
# old behaviour), while a false negative would silently overwrite a stated date.
_CUE_RE = re.compile(
    r"\b(" + _MONTHS + r")\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"  # ISO
    r"|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"  # 8/4, 8/4/25
    r"|\b(today|tonight|yesterday|this\s+morning|this\s+afternoon|this\s+evening)\b"
    r"|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b(last|this|past)\s+(week|weekend|night)\b"
    r"|\b\d+\s*(day|week)s?\s+ago\b",
    re.IGNORECASE,
)

_AGO_RE = re.compile(r"\b(\d+)\s*(day|week)s?\s+ago\b", re.IGNORECASE)
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.IGNORECASE)


def has_date_cue(text: str) -> bool:
    """True if the message says anything about when the workout happened."""
    return bool(_CUE_RE.search(text or ""))


def resolve_relative(text: str, today: datetime.date) -> datetime.date | None:
    """A relative date we can compute exactly, or None to leave it to the parse.

    Weekday names resolve to the most recent past occurrence ("did legs monday"
    on a Friday → this week's Monday; on a Monday → today, not a week ago).
    """
    t = (text or "").lower()
    if re.search(r"\b(today|tonight|this\s+morning|this\s+afternoon|this\s+evening)\b", t):
        return today
    if re.search(r"\byesterday\b", t):
        return today - datetime.timedelta(days=1)
    ago = _AGO_RE.search(t)
    if ago:
        n = int(ago.group(1))
        days = n * 7 if ago.group(2).lower().startswith("week") else n
        return today - datetime.timedelta(days=days)
    weekday = _WEEKDAY_RE.search(t)
    if weekday:
        target = WEEKDAYS[weekday.group(1).lower()]
        delta = (today.weekday() - target) % 7
        if "last" in t and delta == 0:
            delta = 7  # "last monday" said ON a Monday means the previous one
        return today - datetime.timedelta(days=delta)
    return None


def resolve_date(text: str, today: datetime.date, parsed: str | None = None) -> str:
    """The date a workout from `text` should carry, as YYYY-MM-DD."""
    if not has_date_cue(text):
        return today.isoformat()  # undated live log → now, never a model guess
    relative = resolve_relative(text, today)
    if relative is not None:
        return relative.isoformat()
    return parsed or today.isoformat()  # an explicit date the model read off the text


def apply_to_workouts(workouts, text: str, today: datetime.date) -> None:
    """Correct each parsed workout's date in place (live logging path)."""
    for w in workouts:
        w.date = resolve_date(text, today, w.date)
