"""Strava sync — push confirmed workouts out, pull runs back in.

Strava's MCP connector is read-only, so this talks to the v3 REST API directly.
Auth is a one-off `python -m app.strava auth` that yields a refresh token; every
call swaps that for a short-lived access token (nothing is cached — the
serverless instances share no state).
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
TOKEN_URL = "https://www.strava.com/oauth/token"
AUTH_URL = "https://www.strava.com/oauth/authorize"
SCOPE = "activity:write,activity:read_all"

# our workout.type -> Strava sport_type
SPORT_TYPES = {
    "strength": "WeightTraining",
    "basketball": "Basketball",
    "run": "Run",
    "walk": "Walk",
    "hike": "Hike",
    "ride": "Ride",
    "swim": "Swim",
    "yoga": "Yoga",
}
DEFAULT_SPORT = "Workout"
DEFAULT_ELAPSED = 3600  # no start/end on the card → call it an hour
METERS_PER_MILE = 1609.344
_TIMEOUT = 20.0
_PAGE = 200  # Strava's max per_page


class StravaError(RuntimeError):
    """Strava rejected a call, or credentials are missing."""


def configured() -> bool:
    return bool(
        settings.strava_client_id
        and settings.strava_client_secret
        and settings.strava_refresh_token
    )


def _access_token() -> str:
    if not configured():
        raise StravaError("STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN are not set.")
    body = _post(
        TOKEN_URL,
        {
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": settings.strava_refresh_token,
        },
    )
    if body.get("refresh_token") != settings.strava_refresh_token:
        log.warning("Strava rotated the refresh token — update STRAVA_REFRESH_TOKEN")
    return body["access_token"]


def _post(url: str, data: dict, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.post(url, data=data, headers=headers, timeout=_TIMEOUT)
    if r.status_code >= 300:
        raise StravaError(f"POST {url} → {r.status_code}: {r.text[:150]}")
    return r.json()


# --- push: workout -> Strava activity ---


def _elapsed(date: str, start: str | None, end: str | None) -> int:
    if not (start and end):
        return DEFAULT_ELAPSED
    try:
        span = datetime.datetime.fromisoformat(f"{date}T{end}") - datetime.datetime.fromisoformat(
            f"{date}T{start}"
        )
    except ValueError:
        return DEFAULT_ELAPSED
    return int(span.total_seconds()) or DEFAULT_ELAPSED


def push_workout(workout: Workout, description: str = "") -> int:
    """Create the workout as a manual Strava activity. Returns the activity id."""
    payload = {
        "name": workout.notes or f"{workout.type.title()} session",
        "sport_type": SPORT_TYPES.get(workout.type.lower(), DEFAULT_SPORT),
        "start_date_local": f"{workout.date}T{workout.start or '12:00'}:00",
        "elapsed_time": _elapsed(workout.date, workout.start, workout.end),
        "description": description,
    }
    body = _post(f"{API}/activities", payload, _access_token())
    return int(body["id"])


def push_quietly(workout: Workout, description: str = "") -> str:
    """Push if configured, never raise — Strava being down must not fail the log.

    Returns a suffix for the confirm message ('' when Strava isn't set up).
    """
    if not configured():
        return ""
    try:
        push_workout(workout, description)
        return " · Strava ✓"
    except (StravaError, httpx.HTTPError, KeyError, ValueError) as e:
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
        # The activity id keeps raw_text stable, so re-running dedups exactly.
        raw_text=f"strava:{a['id']} {a.get('name', 'Run')} — {summary}",
    )


def fetch_runs(sport: str = "Run") -> list[ParsedWorkout]:
    """Every run in the athlete's history, oldest first."""
    token = _access_token()
    headers = {"Authorization": f"Bearer {token}"}
    runs, page = [], 1
    while True:
        r = httpx.get(
            f"{API}/athlete/activities",
            params={"per_page": _PAGE, "page": page},
            headers=headers,
            timeout=_TIMEOUT,
        )
        if r.status_code >= 300:
            raise StravaError(f"list activities → {r.status_code}: {r.text[:150]}")
        batch = r.json()
        runs += [_to_workout(a) for a in batch if a.get("sport_type") == sport]
        if len(batch) < _PAGE:
            return sorted(runs, key=lambda w: (w.date, w.start or ""))
        page += 1


# --- CLI ---


def _auth() -> None:
    """One-off browser authorization → the refresh token for .env."""
    cid = settings.strava_client_id
    if not cid:
        print("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")
        return
    print(
        f"1. Open:\n   {AUTH_URL}?client_id={cid}&redirect_uri=http://localhost"
        f"&response_type=code&approval_prompt=force&scope={SCOPE}\n"
        "2. Approve, then copy the `code` query param from the failed redirect.\n"
    )
    code = input("code: ").strip()
    body = _post(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": settings.strava_client_secret,
            "code": code,
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
    runs = fetch_runs()
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
