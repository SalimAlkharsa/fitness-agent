"""Framework-free web layer.

A pure `handle(Request) -> Response` router: no server, no ASGI, no threads —
each request is one synchronous call, which is exactly the serverless execution
model. Entry points wrap it:

- `app.serverless.main`  — the DigitalOcean Functions (OpenWhisk) adapter
- `app.devserver`        — a stdlib HTTP server for local dev (`make dev`)
- tests call `handle()` directly.

Every route except /login, /healthz and static assets requires the signed auth
cookie.

State is stateless-by-construction: auth is a signed cookie, pending cards are
signed blobs (app.cards), chat history is client-held. The only per-process bits
are lazy singletons (store/agent) and a healthz cache — warm-start optimizations
any instance can rebuild.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

from app import SCHEMA_VERSION, dedup, dofn, logflow, strava
from app.agent import MAX_HISTORY_MESSAGES, Agent, ChatSession
from app.auth import COOKIE_MAX_AGE, COOKIE_NAME, cookie_valid, make_cookie, passphrase_ok
from app.cards import (
    PendingCard,
    load_activation,
    load_card,
    load_job,
    sign_activation,
    sign_card,
    sign_job,
)
from app.config import settings
from app.kb_seed import STARTER_KB
from app.mdrender import render_markdown
from app.models import Equipment, InboxItem

log = logging.getLogger("fitness-agent")

BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = BASE_DIR / "static"

# Stateless brute-force throttle: a fixed delay on every failed login. Serverless
# instances share no attempt-counter state, and with a high-entropy passphrase a
# per-attempt delay is enough to make online guessing impractical.
LOGIN_FAIL_DELAY = 1.0  # seconds


# --- request / response ---


@dataclass
class Request:
    method: str = "GET"
    path: str = "/"
    form: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)  # keys lowercased
    # Set only on a background self-invocation (app/dofn.async_invoke): a signed
    # job blob the worker path verifies before running the agent synchronously.
    worker_job: str | None = None

    def is_htmx(self) -> bool:
        return self.headers.get("hx-request") == "true"


@dataclass
class Response:
    status: int = 200
    body: str | bytes = ""
    content_type: str = "text/html; charset=utf-8"
    headers: dict = field(default_factory=dict)

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def text(self) -> str:
        return self.body.decode() if isinstance(self.body, bytes) else self.body

    def json(self):
        return json.loads(self.text)


def _redirect(path: str, status: int = 303) -> Response:
    return Response(status=status, headers={"Location": settings.base_path + path})


def _set_cookie(resp: Response, value: str, max_age: int) -> Response:
    resp.headers["Set-Cookie"] = (
        f"{COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax"
    )
    return resp


# --- templates ---

_env = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates")), autoescape=True)
# `| md` renders an assistant reply's Markdown. app/mdrender.py escapes first and
# converts second, so the `| safe` at the call site publishes escaped-then-rendered
# output, never raw model HTML.
_env.filters["md"] = render_markdown

# Cache-buster appended to static asset URLs (?v=…). Static responses carry a long
# Cache-Control behind a CDN, so a changed asset only reaches browsers under a new
# URL — BUMP THIS whenever a file in static/ changes.
STATIC_VERSION = "2"


def render(name: str, ctx: dict | None = None, status: int = 200) -> Response:
    context = {"base": settings.base_path, "v": STATIC_VERSION, **(ctx or {})}
    return Response(status=status, body=_env.get_template(name).render(context))


# --- lazy singletons (warm-start caches; any instance can rebuild them) ---

_AGENT: Agent | None = None
_STORE = None
_SCHEMA_CHECKED = False


def get_store():
    """Open the sheet on first use; verify the schema version once per process.

    A hard schema mismatch raises (the request 500s) rather than letting the app
    run against an unexpected tab layout; transient verification errors just log.
    """
    global _STORE, _SCHEMA_CHECKED
    if _STORE is None and settings.sheet_id and settings.google_sa_json:
        from app.store import SheetsStore

        _STORE = SheetsStore.open()
    if _STORE is not None and not _SCHEMA_CHECKED:
        from app.migrate import SchemaVersionError, check_schema_version

        try:
            check_schema_version(_STORE)
        except SchemaVersionError:
            _STORE = None
            raise
        except Exception as e:  # noqa: BLE001 — transient, don't wedge the instance
            log.warning("Could not verify sheet schema version: %s", e)
        _SCHEMA_CHECKED = True
    return _STORE


def get_agent() -> Agent:
    global _AGENT
    if _AGENT is None:
        _AGENT = Agent(store=get_store())
    return _AGENT


def _authed(request: Request) -> bool:
    return cookie_valid(request.cookies.get(COOKIE_NAME))


def _redirect_login(request: Request) -> Response:
    """Send unauthenticated callers to /login — via HX-Redirect for htmx POSTs."""
    if request.is_htmx():
        return Response(status=204, headers={"HX-Redirect": settings.base_path + "/login"})
    return _redirect("/login")


# --- auth routes ---


def login_form(request: Request) -> Response:
    if _authed(request):
        return _redirect("/")
    return render("login.html", {"error": None})


def login_submit(request: Request) -> Response:
    if passphrase_ok(request.form.get("passphrase", "")):
        return _set_cookie(_redirect("/"), make_cookie(), COOKIE_MAX_AGE)
    time.sleep(LOGIN_FAIL_DELAY)
    return render("login.html", {"error": "Wrong passphrase."}, status=401)


def logout(request: Request) -> Response:
    return _set_cookie(_redirect("/login"), "", 0)


# --- chat routes ---


def _kb():
    store = get_store()
    if store is not None:
        try:
            return store.kb_entries()
        except Exception:  # noqa: BLE001
            pass
    return list(STARTER_KB)


def _wid_num(wid: str) -> int:
    try:
        return int(wid.lstrip("W") or 0)
    except ValueError:
        return 0


def _build_cards(result) -> list[dict]:
    """Turn an agent pending into one or more render-ready confirm cards."""
    pending = result.pending
    cards: list[dict] = []
    if pending.tool == "log_workout":
        kb = _kb()
        store = get_store()
        equipment = [e.value for e in Equipment]
        for pw in pending.args.workouts:
            unknowns = logflow.unknown_exercises(pw, kb)
            # Deterministic dedup check, surfaced as an overridable card warning.
            duplicate = dedup.find_duplicate(pw, store, kb)
            token = sign_card(
                PendingCard(kind="log", workout=pw, unknowns=unknowns, duplicate=duplicate)
            )
            cards.append(
                {
                    "kind": "log",
                    "token": token,
                    "workout": pw,
                    "unknowns": {u.index: u for u in unknowns},
                    "duplicate": duplicate,
                    "equipment": equipment,
                }
            )
    elif pending.tool == "undo":
        store = get_store()
        wid = pending.args.workout_id
        label = ""
        if store is not None:
            workouts = store.read_workouts()
            target = None
            if wid:
                target = next((w for w in workouts if w.workout_id == wid), None)
            elif workouts:
                # Most recent = highest allocated id (robust to row reordering).
                target = max(workouts, key=lambda w: _wid_num(w.workout_id))
            if target is not None:
                wid = target.workout_id
                exs = [s.exercise for s in store.read_sets() if s.workout_id == wid]
                label = f"{target.type} {target.date}" + (f" — {', '.join(exs)}" if exs else "")
        token = sign_card(PendingCard(kind="undo", workout_id=wid, label=label))
        cards.append({"kind": "undo", "token": token, "workout_id": wid, "label": label})
    return cards


def index(request: Request) -> Response:
    return render("chat.html", {})


def _session_from_history(raw: str | None) -> ChatSession:
    """Rebuild the conversation from the client-held history JSON.

    Only plain user/assistant text turns are accepted: the history is context for
    the model, never a write path, so a malformed blob degrades to an empty
    conversation rather than erroring.
    """
    session = ChatSession("default")
    try:
        items = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        items = []
    if isinstance(items, list):
        for m in items[-MAX_HISTORY_MESSAGES:]:
            if (
                isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
                and m["content"]
            ):
                session.add(m["role"], m["content"])
    return session


def _history_json(session: ChatSession) -> str:
    """The updated history the client should hold — plain text turns only.

    `<` is escaped so the blob can sit inside a <script> tag without closing it
    early (the client round-trips the raw string; it never renders as HTML).
    """
    msgs = [
        {"role": m["role"], "content": m["content"]}
        for m in session.messages
        if m.get("role") in ("user", "assistant") and m.get("content") and not m.get("tool_calls")
    ]
    return json.dumps(msgs[-MAX_HISTORY_MESSAGES:]).replace("<", "\\u003c")


def _run_agent_turn(message: str, history: str, *, user_message: str | None) -> Response:
    """Run the agent and render the turn. `user_message` set → include the user
    bubble (synchronous path); None → omit it (the async path already showed it in
    the thinking placeholder)."""
    session = _session_from_history(history)
    result = get_agent().run(session, message)
    ctx: dict = {
        "user_message": user_message,
        "result": result,
        "cards": [],
        "history_json": _history_json(session),
    }
    if result.pending is not None:
        ctx["cards"] = _build_cards(result)
    return render("_turn.html", ctx)


def chat(request: Request) -> Response:
    # Worker path: a valid signed job blob (from dofn.async_invoke) is what
    # authorizes running the slow agent inside this background activation.
    if request.worker_job is not None:
        job = load_job(request.worker_job)
        if job is None:
            return Response(status=403, body="forbidden", content_type="text/plain")
        return _run_agent_turn(job["message"], job.get("history", ""), user_message=None)

    message = request.form.get("message", "")
    history = request.form.get("history", "")

    # Deployed, the agent turn (30-50s) outlives DigitalOcean's ~40s synchronous
    # HTTP cap, so dispatch it to a background activation and return a thinking
    # placeholder that polls for the result. Local/tests: run synchronously.
    if settings.async_dispatch:
        activation_id = dofn.async_invoke(
            {
                "__ow_method": "post",
                "__ow_path": "/chat",
                "__ow_headers": {},
                "_job": sign_job(message, history),
            }
        )
        if activation_id is not None:
            return render(
                "_thinking.html",
                {
                    "user_message": message,
                    "poll_token": sign_activation(activation_id),
                    "poll_n": 0,
                },
            )
        log.warning("async dispatch failed; running /chat synchronously")

    return _run_agent_turn(message, history, user_message=message)


# Client-side poll cap (2.5s × 48 ≈ 120s): a tab left open on a wedged background
# job stops polling instead of billing invocations forever.
MAX_POLLS = 48


def chat_poll(request: Request) -> Response:
    """Return the finished turn once the background activation completes, a
    thinking placeholder to keep polling, or a give-up message past the poll cap."""
    token = request.form.get("a", "")
    activation_id = load_activation(token)
    if activation_id is None:
        return Response(status=403, body="forbidden", content_type="text/plain")
    result = dofn.get_activation_result(activation_id)
    if result is None:  # not ready yet
        n = _int(request.form.get("n"), 0) + 1
        if n >= MAX_POLLS:
            msg = (
                "⚠️ This is taking longer than expected — the model may be overloaded. "
                "Your message wasn’t logged; please try again in a minute."
            )
            return Response(body=f'<div class="bubble assistant err">{msg}</div>')
        return render("_thinking.html", {"poll_token": token, "poll_n": n})
    body = result.get("body") or ""
    if isinstance(body, str) and body.startswith("{"):
        # The worker hit an internal error (JSON error envelope, not turn HTML).
        msg = "⚠️ That didn’t go through. Please try again."
        return Response(body=f'<div class="bubble assistant err">{msg}</div>')
    return Response(body=body)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def confirm(request: Request) -> Response:
    form = request.form
    card = load_card(form.get("token", ""))
    if card is None:
        return Response(body='<div class="note">This card has expired.</div>')
    if card.kind == "undo":
        return _confirm_undo(card, form.get("decision", ""))
    return _confirm_log(card, form, form.get("decision", ""))


def _confirm_log(card: PendingCard, form: dict, decision: str) -> Response:
    store = get_store()
    if decision != "confirm":
        # Discarding still saves the raw text to the inbox so nothing is lost.
        if store is not None and card.workout is not None:
            store.to_inbox(
                InboxItem(
                    timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
                    raw_text=card.workout.raw_text,
                    error="discarded at confirm card",
                )
            )
        return render("_confirmed.html", {"message": "❌ Discarded — saved to inbox."})
    kb = _kb()
    try:
        pw = logflow.parsed_workout_from_form(form)
        kb_updates = logflow.kb_updates_from_form(form, card.unknowns, kb)
    except (ValueError, ValidationError) as e:
        # Invalid edit → re-render the card with an inline error so the user can
        # retry (edits reset to the original parse).
        render_card = {
            "kind": "log",
            "token": sign_card(card),
            "workout": card.workout,
            "unknowns": {u.index: u for u in card.unknowns},
            "duplicate": card.duplicate,
            "equipment": [eq.value for eq in Equipment],
        }
        return render("_log_card.html", {"card": render_card, "error": f"Couldn't save: {e}"}, 422)
    if store is None:
        return Response(body='<div class="note">No sheet configured.</div>')
    # Replay guard: signed tokens aren't consumed, so a re-submitted ✅ would
    # double-log. An exact duplicate the card did NOT warn about when rendered
    # means this text was committed after the card was issued — i.e. a replay. A
    # deliberate override (warning shown, user confirmed anyway) still goes
    # through, since its duplicate id matches the one on the card.
    match = dedup.find_duplicate(pw, store, kb)
    if (
        match is not None
        and match.kind == "exact"
        and (card.duplicate is None or card.duplicate.workout_id != match.workout_id)
    ):
        return render("_confirmed.html", {"message": f"Already logged as {match.workout_id}."})
    workout, summary = logflow.commit_workout(store, pw, kb_updates)
    mirrored = strava.push_quietly(workout, summary, store) if card.source == "chat" else ""
    return render("_confirmed.html", {"message": f"✅ {summary}{mirrored}"})


def _confirm_undo(card: PendingCard, decision: str) -> Response:
    store = get_store()
    if decision != "confirm" or not card.workout_id:
        return render("_confirmed.html", {"message": "Cancelled."})
    deleted = store.delete_workout(card.workout_id) if store is not None else 0
    msg = f"🗑️ Deleted {card.workout_id} ({deleted} rows)." if deleted else "Nothing to delete."
    return render("_confirmed.html", {"message": msg})


# --- backfill review ---

_BACKFILL_PARSER = None  # test seam: set to a parse_fn to bypass the real LLM


def get_backfill_parser():
    """A parse_fn(message)->ParseResult for backfill."""
    if _BACKFILL_PARSER is not None:
        return _BACKFILL_PARSER
    from app.llm import make_client
    from app.parse import parse_workouts

    client = make_client()
    kb = _kb()

    def fn(message: str):
        return parse_workouts(message, kb, client, backfill=True)

    return fn


def backfill_page(request: Request) -> Response:
    return render("backfill.html", {})


def backfill_run(request: Request) -> Response:
    from app import backfill as backfill_mod

    store = get_store()
    items = backfill_mod.parse_dump(request.form.get("dump", ""), get_backfill_parser(), store)
    kb = _kb()
    equipment = [e.value for e in Equipment]
    cards, tokens = [], []
    for item in items:
        for pw in item.workouts:
            unknowns = logflow.unknown_exercises(pw, kb)
            token = sign_card(
                PendingCard(kind="log", workout=pw, unknowns=unknowns, source="backfill")
            )
            tokens.append(token)
            cards.append(
                {
                    "kind": "log",
                    "token": token,
                    "workout": pw,
                    "unknowns": {u.index: u for u in unknowns},
                    "equipment": equipment,
                }
            )
    summary = {
        "new": sum(1 for i in items if not i.duplicate and not i.error),
        "duplicates": sum(1 for i in items if i.duplicate),
        "errors": sum(1 for i in items if i.error),
    }
    return render(
        "_backfill_review.html", {"cards": cards, "tokens": ",".join(tokens), "summary": summary}
    )


def backfill_confirm_all(request: Request) -> Response:
    store = get_store()
    done = []
    for token in filter(None, request.form.get("tokens", "").split(",")):
        card = load_card(token)
        if card and card.kind == "log" and card.workout is not None and store is not None:
            # Replay guard (see /confirm): a re-submitted confirm-all must not
            # double-log — backfill cards never carry an override warning, so
            # any exact duplicate means this card already committed.
            match = dedup.find_duplicate(card.workout, store, store.kb_entries())
            if match is not None and match.kind == "exact":
                continue
            # Bulk-confirm has no per-card KB answers, so auto-learn unknown
            # exercises — that keeps the KB growing over the run.
            updates = logflow.autolearn_kb_updates(card.workout, store.kb_entries())
            _w, summary = logflow.commit_workout(store, card.workout, updates)
            done.append(summary)
    return render("_confirmed.html", {"message": f"✅ Logged {len(done)} workouts."})


# --- PWA + health + static ---


def manifest(request: Request) -> Response:
    base = settings.base_path
    return Response(
        content_type="application/manifest+json",
        body=json.dumps(
            {
                "name": "Fitness Agent",
                "short_name": "Fitness",
                "start_url": f"{base}/",
                "display": "standalone",
                "background_color": "#0d0d13",
                "theme_color": "#0d0d13",
                "icons": [
                    {
                        "src": f"{base}/static/icons/icon-192.png",
                        "sizes": "192x192",
                        "type": "image/png",
                    },
                    {
                        "src": f"{base}/static/icons/icon-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                    },
                ],
            }
        ),
    )


_HEALTH_TTL = 60.0  # seconds — don't hit Sheets on every probe
_health_cache: dict = {"at": None, "sheet": None}


def _probe_sheet() -> dict:
    """Light read of the _meta tab — proves the sheet is reachable and readable."""
    try:
        store = get_store()  # may itself raise if opening the sheet fails
        if store is None:
            return {"reachable": None, "detail": "no credentials configured"}
        return {"reachable": True, "schema_version": store.schema_version}
    except Exception as e:  # noqa: BLE001 — report, never raise from a health probe
        # /healthz is unauthenticated and gspread errors quote the sheet ID and the
        # service-account email, so the detail is logged and never served.
        log.warning("health probe failed: %s", e)
        return {"reachable": False, "detail": "sheet unreachable"}


def healthz(request: Request) -> Response:
    now = time.monotonic()
    cached_at = _health_cache["at"]
    if cached_at is None or now - cached_at > _HEALTH_TTL:
        _health_cache["sheet"] = _probe_sheet()
        _health_cache["at"] = now
    sheet = _health_cache["sheet"]
    ok = sheet.get("reachable") is not False  # True or None(=not configured) → ok
    return Response(
        status=200 if ok else 503,
        content_type="application/json",
        body=json.dumps({"ok": ok, "schema_version": SCHEMA_VERSION, "sheet": sheet}),
    )


_STATIC_TYPES = {
    ".js": "application/javascript",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def _static(path: str) -> Response:
    file = (_STATIC_DIR / path.removeprefix("/static/")).resolve()
    ext = file.suffix
    if not file.is_file() or not file.is_relative_to(_STATIC_DIR) or ext not in _STATIC_TYPES:
        return Response(status=404, body="Not found", content_type="text/plain")
    return Response(
        body=file.read_bytes(),
        content_type=_STATIC_TYPES[ext],
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --- router ---

# (method, path) -> (handler, needs_auth)
_ROUTES = {
    ("GET", "/login"): (login_form, False),
    ("POST", "/login"): (login_submit, False),
    ("POST", "/logout"): (logout, False),
    ("GET", "/"): (index, True),
    ("POST", "/chat"): (chat, True),
    ("POST", "/chat/poll"): (chat_poll, True),
    ("POST", "/confirm"): (confirm, True),
    ("GET", "/backfill"): (backfill_page, True),
    ("POST", "/backfill"): (backfill_run, True),
    ("POST", "/backfill/confirm-all"): (backfill_confirm_all, True),
    ("GET", "/manifest.webmanifest"): (manifest, False),
    ("GET", "/healthz"): (healthz, False),
}


def handle(request: Request) -> Response:
    path = request.path or "/"
    method = request.method.upper()
    if method == "GET" and path.startswith("/static/"):
        return _static(path)
    route = _ROUTES.get((method, path))
    if route is None:
        return Response(status=404, body="Not found", content_type="text/plain")
    handler, needs_auth = route
    # A background worker invocation carries no browser cookie — its signed job
    # blob (verified inside chat()) is the authorization, so skip the cookie gate.
    if needs_auth and request.worker_job is None and not _authed(request):
        return _redirect_login(request)
    try:
        return handler(request)
    except Exception:  # noqa: BLE001 — one request must never crash the instance
        log.exception("unhandled error on %s %s", method, path)
        return Response(status=500, body="Something went wrong.", content_type="text/plain")
