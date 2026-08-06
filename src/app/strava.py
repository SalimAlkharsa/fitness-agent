"""Strava sync — push confirmed workouts out, pull runs back in.

Strava's MCP connector is read-only, so this uses the v3 REST API. `python -m
app.strava auth` yields a refresh token; every call swaps it for a short-lived
access token (serverless instances cache nothing).
"""

from __future__ import annotations

import datetime
import logging
import sys

import httpx

from app.config import settings
from app.models import ParsedWorkout, Workout

log = logging.getLogger("fitness-agent")

API = "https://www.strava.com/api/v3"
TOKEN_URL = f"{API}/oauth/token"
AUTH_URL = "https://www.strava.com/oauth/authorize"
REDIRECT_URI = "http://localhost/exchange_token"
SCOPE = "activity:write,activity:read_all"
META_TOKEN_KEY = "strava_refresh_token"

SPORT_TYPES = {  # our workout.type -> Strava sport_type
    "strength": "WeightTraining",
    "basketball": "Basketball",
    "run": "Run",
    "walk": "Walk",
    "hike": "Hike",
    "ride": "Ride",
    "swim": "Swim",
    "yoga": "Yoga",
}
RUN_TYPES = ("Run", "TrailRun", "VirtualRun")
DEFAULT_ELAPSED = 3600  # no start/end on the card → call it an hour
METERS_PER_MILE = 1609.344
_TIMEOUT = 20.0
_PAGE = 200  # Strava's max per_page


class StravaError(RuntimeError):
    """Strava rejected a call, or credentials are missing."""


def configured() -> bool:
    return all(
        (settings.strava_client_id, settings.strava_client_secret, settings.strava_refresh_token)
    )


def _call(method: str, url: str, token: str | None = None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.request(method, url, headers=headers, timeout=_TIMEOUT, **kw)
    if r.status_code >= 300:
        raise StravaError(f"{method} {url} → {r.status_code}: {r.text[:150]}")
    return r.json()


def _access_token(store=None) -> str:
    """Strava may rotate the refresh token and kills the old one at once, so the
    live one is kept in the sheet once it has rotated; .env only seeds it."""
    if not configured():
        raise StravaError("STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN are not set.")
    current = (store and store.meta_get(META_TOKEN_KEY)) or settings.strava_refresh_token
    body = _call(
        "POST",
        TOKEN_URL,
        data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": current,
        },
    )
    rotated = body.get("refresh_token")
    if rotated and rotated != current:
        if store is not None:
            store.meta_set(META_TOKEN_KEY, rotated)
        else:
            log.warning("Strava rotated the refresh token — re-run `python -m app.strava auth`")
    return body["access_token"]


# --- push: workout -> Strava activity ---


def _clock(value: str | None) -> str | None:
    """HH:MM, or None — the card's time fields are free text and unvalidated."""
    value = (value or "").strip()
    if len(value) == 4 and value[1] == ":":  # '7:30'
        value = "0" + value
    try:
        return datetime.time.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return None


def _elapsed(date: str, start: str | None, end: str | None) -> int:
    start, end = _clock(start), _clock(end)
    if not (start and end):
        return DEFAULT_ELAPSED
    a = datetime.datetime.fromisoformat(f"{date}T{start}")
    b = datetime.datetime.fromisoformat(f"{date}T{end}")
    if b < a:
        b += datetime.timedelta(days=1)  # crossed midnight
    return int((b - a).total_seconds()) or DEFAULT_ELAPSED


def push_workout(workout: Workout, description: str = "", store=None) -> int:
    """Create the workout as a manual Strava activity; returns the activity id."""
    payload = {
        "name": workout.notes or f"{workout.type.title()} session",
        "sport_type": SPORT_TYPES.get(workout.type.lower(), "Workout"),
        # Strava reads these digits as local wall-clock time; any offset is ignored.
        "start_date_local": f"{workout.date}T{_clock(workout.start) or '12:00'}:00",
        "elapsed_time": _elapsed(workout.date, workout.start, workout.end),
        "description": description,
    }
    return int(_call("POST", f"{API}/activities", _access_token(store), data=payload)["id"])


def push_quietly(workout: Workout, description: str = "", store=None) -> str:
    """Confirm-message suffix. Never raises — the sheet row is already written."""
    if not configured():
        return ""
    try:
        push_workout(workout, description, store)
        return " · Strava ✓"
    except Exception as e:  # noqa: BLE001
        log.warning("Strava push failed: %s", e)
        return " · Strava ✗ (sheet saved)"


# --- pull: Strava runs -> ParsedWorkout ---


def _dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _to_workout(a: dict) -> ParsedWorkout:
    miles = (a.get("distance") or 0) / METERS_PER_MILE
    elapsed = a.get("elapsed_time") or a.get("moving_time") or 0
    moving = a.get("moving_time") or elapsed
    start = datetime.datetime.fromisoformat(a["start_date_local"].rstrip("Z"))
    pace = f", {_dur(moving / miles)}/mi" if miles else ""
    summary = f"{miles:.2f} mi in {_dur(moving)}{pace}"
    return ParsedWorkout(
        date=start.date().isoformat(),
        type="run",
        start=start.strftime("%H:%M"),
        end=(start + datetime.timedelta(seconds=elapsed)).strftime("%H:%M"),
        notes=summary,
        raw_text=f"strava:{a['id']} {a.get('name', 'Run')} — {summary}",  # stable → dedups
    )


def fetch_runs(store=None) -> list[ParsedWorkout]:
    """Every run in the athlete's history, oldest first. Manual activities are
    skipped — the ones we push are manual, and re-importing one would duplicate it."""
    token, runs, page = _access_token(store), [], 1
    while True:
        batch = _call(
            "GET",
            f"{API}/athlete/activities",
            token,
            params={"per_page": _PAGE, "page": page},
        )
        runs += [
            _to_workout(a)
            for a in batch
            if a.get("sport_type") in RUN_TYPES and not a.get("manual")
        ]
        if len(batch) < _PAGE:
            return sorted(runs, key=lambda w: (w.date, w.start or ""))
        page += 1


# --- CLI ---


def _auth() -> None:
    cid = settings.strava_client_id
    if not cid:
        print("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")
        return
    print(
        f"1. Open:\n   {AUTH_URL}?client_id={cid}&redirect_uri={REDIRECT_URI}"
        f"&response_type=code&approval_prompt=force&scope={SCOPE}\n"
        "2. Approve, then copy the single-use `code` param out of the address bar\n"
        "   of the page that fails to load.\n"
    )
    body = _call(
        "POST",
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": settings.strava_client_secret,
            "code": input("code: ").strip(),
            "grant_type": "authorization_code",
        },
    )
    print(f"\nAdd to .env:\nSTRAVA_REFRESH_TOKEN={body['refresh_token']}")


def _backfill(do_commit: bool) -> None:
    from app.dedup import committed_hashes, raw_hash
    from app.logflow import commit_workout
    from app.store import SheetsStore

    store = SheetsStore.open()
    seen = committed_hashes(store)
    runs = fetch_runs(store)
    new = [w for w in runs if raw_hash(w.raw_text) not in seen]
    for pw in new:
        print(f"  {pw.date} {pw.notes}")
        if do_commit:
            commit_workout(store, pw)
    print(f"\n{len(runs)} runs on Strava: {len(new)} new, {len(runs) - len(new)} already logged.")
    if not do_commit:
        print("Dry run. Re-run with --commit to write.")


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else ""
    if cmd == "auth":
        _auth()
    elif cmd == "backfill":
        _backfill("--commit" in argv)
    else:
        print("Usage: python -m app.strava auth | backfill [--commit]")


if __name__ == "__main__":
    main()
