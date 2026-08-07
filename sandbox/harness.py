"""Wire the real agent to offline backends, picked from whatever the env has.

Only two edges are mocked — the agent loop, tools, prompts, KB, date logic and
commit path are the shipping ones:

  LLM    Claude Haiku through the already-authenticated `claude` CLI (works with
         no API key, which is the point), else OpenRouter when a key is set.
         `SANDBOX_LLM=claude|openrouter` forces one.
  store  the local JSON sheet (sandbox/data/sheet.json). `SANDBOX_SHEET=real`
         opts in to the live spreadsheet — off by default so a test run can never
         write to real data.

The CLI has no native tool-call channel, so that backend runs the agent in json
tool mode; OpenRouter keeps whatever TOOL_MODE says. Same scenarios either way.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import logflow
from app.agent import Agent, AgentResult, ChatSession
from app.config import settings
from app.kb_seed import STARTER_KB
from app.llm import LLMError, LLMResponse, make_client
from app.models import SetGroup, Workout
from app.store import SheetsStore
from sandbox.local_sheet import DEFAULT_PATH, LocalSheet

HAIKU = "claude-haiku-4-5-20251001"
LOG_DIR = DEFAULT_PATH.parent / "logs"  # agent transcripts — why a turn went wrong


class ClaudeCliClient:
    """LLM backend that runs Haiku through the `claude` CLI, no key needed.

    Mirrors `OpenRouterClient.chat`, so it drops into the agent loop untouched.
    """

    def __init__(self, model: str = HAIKU, timeout: float = 300.0) -> None:
        self.model = model
        self.timeout = timeout

    def chat(self, messages, tools=None, json_mode=False, temperature=0.0, model=None):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = "\n\n".join(
            f"<{m['role']}>\n{m.get('content') or ''}" for m in messages if m["role"] != "system"
        )
        # Restate the contract after the thread. Otherwise the CLI's own framing
        # wins intermittently and the reply is "I'm Claude Code, I don't have
        # access to those tools" instead of the call — worse once the thread
        # already holds a prose turn.
        convo += "\n\nAnswer the final <user> turn per the system prompt. Its tools are real"
        convo += " and callable — emitting the call IS using it; never say a tool is missing."
        if json_mode:
            convo += " Output one JSON object and nothing else."
        # --allowed-tools "": answer from the prompt, don't go read the repo.
        # --system-prompt replaces Claude Code's own; *appended* to it the model
        # stays "Claude Code, a CLI tool" and explains it can't reach these tools.
        flags = ["--model", model or self.model, "--allowed-tools", "", "--system-prompt", system]
        # cwd: a temp dir keeps the repo's own CLAUDE.md out of the model's context.
        opts = dict(capture_output=True, text=True, timeout=self.timeout, cwd=tempfile.gettempdir())
        try:
            proc = subprocess.run(["claude", "-p", *flags], input=convo, **opts)
        except (OSError, subprocess.SubprocessError) as e:
            raise LLMError(f"claude CLI failed: {e!r}") from e
        if proc.returncode != 0:
            raise LLMError(f"claude CLI exit {proc.returncode}: {proc.stderr[-300:]}")
        return LLMResponse(content=proc.stdout.strip())


def make_llm():
    """(client, tool_mode) for this environment; (None, "") if it has neither."""
    choice = (os.getenv("SANDBOX_LLM") or "auto").lower()
    if choice != "openrouter" and shutil.which("claude"):
        return ClaudeCliClient(), "json"
    if choice != "claude" and settings.openrouter_api_key:
        return make_client(), settings.tool_mode
    return None, ""


def _open_store(sheet_path: Path, reset: bool) -> tuple[LocalSheet | None, SheetsStore]:
    if os.getenv("SANDBOX_SHEET") == "real":
        return None, SheetsStore.open()
    if reset:
        sheet_path.unlink(missing_ok=True)
    fresh = not sheet_path.exists()
    sheet = LocalSheet(sheet_path)
    store = SheetsStore(sheet)
    store.bootstrap(kb_seed=STARTER_KB)  # idempotent: tabs, headers, starter KB
    if fresh:
        seed_history(store)
    sheet.save()
    return sheet, store


def seed_history(store: SheetsStore) -> None:
    """Two past sessions, so read/chart scenarios have something to find."""
    for date, lbs in (("2026-07-10", 60.0), ("2026-07-17", 65.0)):
        wid = store.next_workout_id()
        raw = f"Squats 3x5 {lbs:g} on each side"
        store.append_workout(
            Workout(workout_id=wid, date=date, type="strength", raw_text=raw, logged_at=date)
        )
        squat = dict(workout_id=wid, exercise="Back Squat", exercise_raw="Squats", grp=1, sets=3)
        store.append_sets([SetGroup(reps=5, per_side_lbs=lbs, total_lbs=45 + 2 * lbs, **squat)])


class Sandbox:
    """One chat session against the offline backends, plus sheet assertions."""

    def __init__(self, reset: bool = False, sheet_path: Path = DEFAULT_PATH) -> None:
        client, mode = make_llm()
        if client is None:
            raise RuntimeError("No LLM backend: need the `claude` CLI or OPENROUTER_API_KEY.")
        self.backend = f"{type(client).__name__}:{client.model}"
        self.sheet_path = sheet_path
        self.sheet, self.store = _open_store(sheet_path, reset)
        self.agent = Agent(client=client, store=self.store, tool_mode=mode, log_dir=LOG_DIR)
        self.session = ChatSession("sandbox")

    def say(self, text: str, now: str | None = None) -> AgentResult:
        result = self.agent.run(self.session, text, now=now)
        self.save()
        return result

    def confirm(self, result: AgentResult, **edits) -> str:
        """Approve a pending card the way the web layer's ✅ does.

        Keyword args edit the card's fields first, standing in for the user
        correcting the model — e.g. `confirm(r, date="2026-07-19")`.
        """
        pending = result.pending
        if pending is None:
            return ""
        if pending.tool == "undo":
            wid = pending.args.workout_id or self.latest_id()
            self.store.delete_workout(wid)
            summary = f"Deleted {wid}"
        elif pending.tool == "edit_workout":
            wid = pending.args.workout_id or self.latest_id()
            summary = self._commit(self._parsed(wid), edits, workout_id=wid)
        else:  # log_workout, and any future write tool taking a ParseResult
            summary = " | ".join(self._commit(w, edits) for w in pending.args.workouts)
        # The card's outcome is what the user sees next in the thread, so it
        # belongs in the conversation; without it the next turn still sees a
        # workout message with no reply and logs it a second time.
        self.session.add("assistant", summary)
        self.save()
        return summary

    def tools_used(self) -> list[str]:
        """Every tool this session actually invoked, in order — read off the
        transcript, so read tools (which never reach AgentResult) show up too."""
        used = []
        for msg in self.session.messages:
            if msg.get("role") == "tool" and msg.get("name"):
                used.append(msg["name"])  # native mode has a real tool channel
            else:
                used += re.findall(r"\[tool result:(\w+)\]", str(msg.get("content") or ""))
        return used

    def rows(self, tab: str) -> list[dict]:
        return self.store._ws(tab).get_all_records()

    def latest_id(self) -> str:
        return self.store.read_workouts()[-1].workout_id

    def save(self) -> None:
        if self.sheet is not None:
            self.sheet.save()

    def _commit(self, pw, edits: dict, workout_id: str | None = None) -> str:
        for field, value in edits.items():
            setattr(pw, field, value)
        return logflow.commit_workout(self.store, pw, workout_id=workout_id)[1]

    def _parsed(self, wid: str):
        workout = next(w for w in self.store.read_workouts() if w.workout_id == wid)
        sets = [s for s in self.store.read_sets() if s.workout_id == wid]
        return logflow.workout_to_parsed(workout, sets)


def run_scenario(sb: Sandbox, sc: dict) -> tuple[bool, str]:
    """Run one scenario dict (see scenarios.py) → (passed, report line).

    Each scenario starts a fresh conversation against the shared sheet. The
    question under test is "does this message, cold, reach the right tool?" —
    with the previous scenario still in context the model can just answer from
    the thread and legitimately call nothing.
    """
    sb.session = ChatSession(sc["say"][:24])
    before = {tab: len(sb.rows(tab)) for tab in sc.get("adds", {})}
    result = sb.say(sc["say"])
    called = sb.tools_used() + ([result.pending.tool] if result.pending else [])

    problems = []
    if (want := sc.get("tool")) and want not in called:
        problems.append(f"expected {want}, model called {called or 'nothing'}")
    if result.inbox:
        # Distinguishes "never triggered" from "triggered, args kept failing" —
        # the usual first result of a new write tool with a fiddly schema.
        problems.append("a write tool's args failed validation twice → sent to inbox")
    if sc.get("confirm"):
        sb.confirm(result, **sc.get("edits", {}))
    for tab, delta in sc.get("adds", {}).items():
        grew = len(sb.rows(tab)) - before[tab]
        if grew != delta:
            problems.append(f"{tab} rows +{grew}, expected +{delta}")

    head = f"{'ok  ' if not problems else 'FAIL'} {sc['say'][:52]!r} → {called or ['(answer)']}"
    return not problems, head + ("".join(f"\n       - {p}" for p in problems))
