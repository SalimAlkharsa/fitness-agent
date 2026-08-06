"""Web layer tests — auth gate, login delay, chat fragment, confirm card.

The agent is replaced with a fake via the module seams (web._AGENT etc.) so no
network is hit. Passphrase/secret are set for the test process before importing
the app modules.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace

# Force the test passphrase/secret regardless of any .env already loaded into the
# process by an earlier test module (load_dotenv may have set an empty PASSPHRASE).
os.environ["PASSPHRASE"] = "test-pass"
os.environ["SECRET_KEY"] = "test-secret-key-for-cookies-000000"
os.environ["BASE_PATH"] = ""  # a deploy-configured .env must not skew URL asserts

import pytest  # noqa: E402

# Ensure config picks up the env we just set (config caches settings at import).
import app.config as config  # noqa: E402

config.settings = config.Settings.from_env()
import app.auth as auth  # noqa: E402
import app.web as web  # noqa: E402

auth.settings = config.settings
web.settings = config.settings

from app.agent import AgentResult, ChatSession, PendingConfirmation  # noqa: E402
from app.models import ParsedSetGroup, ParsedWorkout, ParseResult  # noqa: E402
from tests.webclient import MiniClient  # noqa: E402


class FakeAgent:
    def __init__(self, result: AgentResult) -> None:
        self.result = result

    def run(self, session: ChatSession, message: str, now=None) -> AgentResult:
        session.add("user", message)
        return self.result


def _client(agent_result: AgentResult) -> MiniClient:
    web._AGENT = FakeAgent(agent_result)
    return MiniClient()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    web._AGENT = None
    web._STORE = None
    web._SCHEMA_CHECKED = False
    web._BACKFILL_PARSER = None
    web._health_cache["at"] = None
    web._health_cache["sheet"] = None
    # Don't actually sleep on failed-login tests.
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    yield
    web._AGENT = None
    web._STORE = None
    web._SCHEMA_CHECKED = False
    web._BACKFILL_PARSER = None
    web._health_cache["at"] = None


def _login(client: MiniClient) -> None:
    r = client.post("/login", data={"passphrase": "test-pass"})
    assert r.status_code == 303


# --- auth gate (the unauthenticated / → /login redirect is covered by test_smoke) ---


def test_login_sets_cookie_and_index_loads():
    client = _client(AgentResult(text="x"))
    _login(client)
    assert client.cookies.get(auth.COOKIE_NAME)  # persistent cookie set
    r = client.get("/")
    assert r.status_code == 200
    assert "Fitness Agent" in r.text


def test_wrong_passphrase_is_401_after_fixed_delay(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(web.time, "sleep", lambda s: slept.append(s))
    client = _client(AgentResult(text="x"))
    r = client.post("/login", data={"passphrase": "nope"})
    assert r.status_code == 401
    assert "Wrong passphrase" in r.text
    # Stateless brute-force throttle: every failure pays the fixed delay.
    assert slept == [web.LOGIN_FAIL_DELAY]


def test_healthz_open_without_auth():
    # Inject the in-memory fake store so the probe stays hermetic (no network).
    store = _inject_store()
    r = MiniClient().get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["schema_version"] == store.schema_version
    assert body["sheet"]["reachable"] is True


def test_healthz_reports_unreachable_sheet_without_leaking_internals():
    # /healthz is unauthenticated, so a failure must say "down" and nothing more:
    # real gspread errors quote the sheet ID and the service-account email.
    class Boom:
        @property
        def schema_version(self):
            raise RuntimeError(
                "SpreadsheetNotFound: 1a2bSECRETSHEETID for sa@proj.iam.gserviceaccount.com"
            )

    web._STORE = Boom()
    web._SCHEMA_CHECKED = True
    r = MiniClient().get("/healthz")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    detail = r.json()["sheet"]["detail"]
    assert "unreachable" in detail
    assert "SECRETSHEETID" not in detail and "gserviceaccount" not in detail


# --- chat ---


def test_chat_requires_auth():
    client = _client(AgentResult(text="hi"))
    r = client.post("/chat", data={"message": "hello"})
    assert r.status_code == 303  # no HX-Request header → plain redirect to login
    r_htmx = client.post("/chat", data={"message": "hello"}, headers={"HX-Request": "true"})
    assert r_htmx.status_code == 204
    assert r_htmx.headers["HX-Redirect"] == "/login"


def test_chat_renders_assistant_bubble():
    client = _client(AgentResult(text="You squatted 3x5 @ 165."))
    _login(client)
    r = client.post("/chat", data={"message": "what did I squat?"})
    assert r.status_code == 200
    assert "You squatted 3x5 @ 165." in r.text
    assert "what did I squat?" in r.text  # user bubble echoed
    # Stateless server: the updated history rides along for the client to hold,
    # and a posted history is fed back into the agent's session.
    assert 'data-history>[{"role": "user", "content": "what did I squat?"}]' in r.text
    history = '[{"role": "user", "content": "earlier message"}]'
    r2 = client.post("/chat", data={"message": "and bench?", "history": history})
    assert '"earlier message"' in r2.text and '"and bench?"' in r2.text


# --- async dispatch + polling (deployed path; the sync path above is local/tests) ---


def _async_settings():
    """A settings copy with self-invoke creds set → web.chat takes the async branch."""
    import dataclasses

    return dataclasses.replace(config.settings, do_fn_host="h", do_fn_namespace="n", do_fn_auth="a")


def test_async_chat_dispatches_and_poll_returns_result(monkeypatch):
    # Deployed path: /chat fires a background activation and returns a poller;
    # /chat/poll returns the finished turn once the activation completes.
    client = _client(AgentResult(text="done"))
    _login(client)
    monkeypatch.setattr(web, "settings", _async_settings())

    captured = {}

    def fake_invoke(params):
        captured.update(params)
        return "ACT123"

    monkeypatch.setattr(web.dofn, "async_invoke", fake_invoke)

    r = client.post("/chat", data={"message": "log squats 3x5"})
    assert r.status_code == 200
    assert "poll-turn" in r.text and "/chat/poll" in r.text
    assert 'class="bubble user"' in r.text  # user message shown immediately
    # the slow work was dispatched with a signed job carrying the message
    assert captured["__ow_path"] == "/chat"
    assert web.load_job(captured["_job"])["message"] == "log squats 3x5"

    token = re.search(r'"a": "([^"]+)"', r.text).group(1)

    # not ready yet → keep polling, with an incremented poll counter
    monkeypatch.setattr(web.dofn, "get_activation_result", lambda a: None)
    r_wait = client.post("/chat/poll", data={"a": token, "n": "0"})
    assert "poll-turn" in r_wait.text and '"n": "1"' in r_wait.text

    # ready → the worker's rendered turn HTML comes back
    monkeypatch.setattr(
        web.dofn, "get_activation_result", lambda a: {"body": "<div>the card</div>"}
    )
    r_done = client.post("/chat/poll", data={"a": token, "n": "5"})
    assert r_done.text == "<div>the card</div>"


def test_poll_gives_up_after_cap(monkeypatch):
    # A wedged background job must stop polling (and billing) after ~2 min.
    client = _client(AgentResult(text="x"))
    _login(client)
    monkeypatch.setattr(web, "settings", _async_settings())
    monkeypatch.setattr(web.dofn, "get_activation_result", lambda a: None)  # never ready
    token = web.sign_activation("ACT")
    r = client.post("/chat/poll", data={"a": token, "n": str(web.MAX_POLLS - 1)})
    assert "poll-turn" not in r.text  # stops polling
    assert "taking longer than expected" in r.text


def test_poll_rejects_forged_token(monkeypatch):
    client = _client(AgentResult(text="x"))
    _login(client)
    monkeypatch.setattr(web, "settings", _async_settings())
    r = client.post("/chat/poll", data={"a": "not-a-signed-token"})
    assert r.status_code == 403


def test_worker_path_runs_agent_only_with_valid_signed_job():
    # The background worker authorizes itself with a signed job (no browser cookie).
    web._AGENT = FakeAgent(AgentResult(text="worker ran"))
    good = web.sign_job("log squats 3x5", "")
    req = web.Request(method="POST", path="/chat", worker_job=good)
    resp = web.handle(req)  # no auth cookie, but a valid job → runs
    assert resp.status_code == 200 and "worker ran" in resp.text
    assert 'class="bubble user"' not in resp.text  # async path already showed it

    # a forged/garbage job is rejected, even though it bypassed the cookie gate
    bad = web.Request(method="POST", path="/chat", worker_job="forged")
    assert web.handle(bad).status_code == 403


def _pending_result() -> AgentResult:
    pw = ParsedWorkout(
        date="2026-07-14",
        type="strength",
        raw_text="Squats 3x5 60 each side",
        sets=[
            ParsedSetGroup(
                exercise="Back Squat",
                exercise_raw="Squats",
                grp=1,
                sets=3,
                reps=5,
                per_side_lbs=60,
                total_lbs=165,
            )
        ],
    )
    return AgentResult(
        pending=PendingConfirmation(
            tool="log_workout",
            args=ParseResult(workouts=[pw]),
            summary="Back Squat 3x5 @ 165",
        )
    )


def _inject_store():
    """Point the web layer at an in-memory fake store; returns it."""
    from app.kb_seed import STARTER_KB
    from app.store import SheetsStore
    from tests.fake_sheets import FakeSpreadsheet

    store = SheetsStore(FakeSpreadsheet())
    store.bootstrap(kb_seed=STARTER_KB)
    web._STORE = store
    web._SCHEMA_CHECKED = True
    return store


def test_chat_renders_editable_confirm_card():
    client = _client(_pending_result())
    _login(client)
    r = client.post("/chat", data={"message": "log squats 3x5 60 each side"})
    assert r.status_code == 200
    assert "Log this workout?" in r.text
    assert 'value="Back Squat"' in r.text  # editable input pre-filled
    assert 'hx-post="/confirm"' in r.text
    assert 'name="s0-per_side"' in r.text  # per-field editable cells
    assert "✅" in r.text and "❌" in r.text


def _extract_token(frag: str) -> str:
    # Signed tokens are dot-separated itsdangerous payloads in a hidden field.
    return re.search(r'name="token" value="([^"]+)"', frag).group(1)


def _card_form(token: str, decision: str) -> dict:
    return {
        "token": token,
        "decision": decision,
        "n_sets": "1",
        "w-date": "2026-07-14",
        "w-type": "strength",
        "w-raw": "Squats 3x5 60 each side",
        "s0-exercise": "Back Squat",
        "s0-exercise_raw": "Squats",
        "s0-grp": "1",
        "s0-sets": "3",
        "s0-reps": "5",
        "s0-per_side": "60",
        "s0-total": "165",
    }


def test_confirm_commits_workout_to_store():
    store = _inject_store()
    client = _client(_pending_result())
    _login(client)
    token = _extract_token(client.post("/chat", data={"message": "log it"}).text)

    # nothing written before ✅
    assert store.read_workouts() == []

    r = client.post("/confirm", data=_card_form(token, "confirm"))
    assert r.status_code == 200
    assert "Logged W0001" in r.text
    workouts = store.read_workouts()
    assert len(workouts) == 1 and workouts[0].workout_id == "W0001"
    sets = store.read_sets()
    assert len(sets) == 1 and sets[0].exercise == "Back Squat" and sets[0].total_lbs == 165


def test_discard_writes_to_inbox_and_writes_nothing():
    store = _inject_store()
    client = _client(_pending_result())
    _login(client)
    token = _extract_token(client.post("/chat", data={"message": "log it"}).text)

    r = client.post("/confirm", data=_card_form(token, "discard"))
    assert "Discarded" in r.text
    assert store.read_workouts() == []  # nothing committed
    assert store._ws("inbox").get_all_records()  # raw text preserved in inbox


def test_replayed_or_tampered_card_cannot_double_log():
    store = _inject_store()
    client = _client(_pending_result())
    _login(client)
    token = _extract_token(client.post("/chat", data={"message": "log it"}).text)
    client.post("/confirm", data=_card_form(token, "confirm"))
    # Signed tokens aren't consumed server-side → a replayed ✅ must hit the
    # exact-duplicate guard instead of double-logging.
    r_again = client.post("/confirm", data=_card_form(token, "confirm"))
    assert "Already logged" in r_again.text
    assert len(store.read_workouts()) == 1
    # A tampered signature is rejected outright.
    r_bad = client.post("/confirm", data=_card_form(token[:-4] + "0000", "confirm"))
    assert "expired" in r_bad.text.lower()
    assert len(store.read_workouts()) == 1


# --- Strava mirror ---


def _strava_on(monkeypatch, push):
    """Configure Strava and stub only the network call, so the real gating runs."""
    monkeypatch.setattr(
        web.strava,
        "settings",
        replace(
            web.strava.settings,
            strava_client_id="1",
            strava_client_secret="s",
            strava_refresh_token="rt",
        ),
    )
    monkeypatch.setattr(web.strava, "push_workout", push)


def test_chat_confirm_mirrors_to_strava_but_a_backfill_card_does_not(monkeypatch):
    _inject_store()
    pushed: list = []
    _strava_on(monkeypatch, lambda w, description="", store=None: pushed.append(w.workout_id))
    client = _client(_pending_result())
    _login(client)

    token = _extract_token(client.post("/chat", data={"message": "log it"}).text)
    r = client.post("/confirm", data=_card_form(token, "confirm"))
    assert "Logged W0001" in r.text and "Strava ✓" in r.text
    assert pushed == ["W0001"]

    # Backfill cards post to this same route; replaying history must stay off the feed.
    web._BACKFILL_PARSER = lambda m: ParseResult(
        workouts=[ParsedWorkout(date="2025-01-02", type="basketball", raw_text=m)]
    )
    review = client.post("/backfill", data={"dump": "hoops on jan 2"})
    bf_form = {
        "token": _extract_token(review.text),
        "decision": "confirm",
        "n_sets": "0",
        "w-date": "2025-01-02",
        "w-type": "basketball",
        "w-raw": "hoops on jan 2",
    }
    r2 = client.post("/confirm", data=bf_form)
    assert "Logged W0002" in r2.text and "Strava" not in r2.text
    assert pushed == ["W0001"]


def test_confirm_still_commits_when_the_strava_push_blows_up(monkeypatch):
    store = _inject_store()

    def boom(*args, **kwargs):
        raise RuntimeError("strava (or the sheet's token read) failed")

    _strava_on(monkeypatch, boom)
    client = _client(_pending_result())
    _login(client)

    token = _extract_token(client.post("/chat", data={"message": "log it"}).text)
    r = client.post("/confirm", data=_card_form(token, "confirm"))
    assert r.status_code == 200 and "Strava ✗" in r.text
    assert len(store.read_workouts()) == 1  # the sheet write is what actually matters


# --- backfill review ---


def test_backfill_review_and_confirm_all():
    store = _inject_store()
    client = _client(AgentResult(text="x"))

    def fake_parser(message: str) -> ParseResult:
        pw = ParsedWorkout(
            date="2026-07-14",
            type="strength",
            raw_text=message,
            sets=[
                ParsedSetGroup(
                    exercise="Back Squat",
                    exercise_raw="squats",
                    grp=1,
                    sets=3,
                    reps=5,
                    per_side_lbs=60,
                    total_lbs=165,
                )
            ],
        )
        return ParseResult(workouts=[pw])

    web._BACKFILL_PARSER = fake_parser
    _login(client)
    dump = "Squats 3x5 60 each side\n\n\nDeadlift 3x10 hex bar 65 each side"
    review = client.post("/backfill", data={"dump": dump})
    assert review.status_code == 200
    assert "2 new" in review.text
    assert "Confirm all 2" in review.text

    tokens = re.findall(r'name="token" value="([^"]+)"', review.text)
    assert len(tokens) == 2

    r = client.post("/backfill/confirm-all", data={"tokens": ",".join(tokens)})
    assert "Logged 2 workouts" in r.text
    assert len(store.read_workouts()) == 2
    # Replayed confirm-all hits the exact-duplicate guard — nothing double-logs.
    r_again = client.post("/backfill/confirm-all", data={"tokens": ",".join(tokens)})
    assert "Logged 0 workouts" in r_again.text
    assert len(store.read_workouts()) == 2


def test_backfill_skips_already_logged():
    store = _inject_store()
    client = _client(AgentResult(text="x"))
    # pre-log a workout whose raw_text matches a dump message
    from app.logflow import commit_workout

    commit_workout(store, ParsedWorkout(date="2026-07-14", raw_text="Squats 3x5 60 each side"))

    web._BACKFILL_PARSER = lambda m: ParseResult(
        workouts=[ParsedWorkout(date="2026-07-14", raw_text=m)]
    )
    _login(client)
    review = client.post("/backfill", data={"dump": "Squats 3x5 60 each side"})
    assert "0 new" in review.text
    assert "1 already logged" in review.text
