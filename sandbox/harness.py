"""Run the real agent against a fake model and a fake sheet.

Mocked: the LLM is Claude Haiku driven through the `claude` CLI — already
authenticated, so this needs no API key, which is the point — and the
spreadsheet is a JSON file under sandbox/data/. Everything between (agent loop,
tool menu, prompts, KB, skill loading, date resolution, retry/inbox guards,
commit path) is the shipping code.

Offline always: no OpenRouter quota to spend, no live spreadsheet to corrupt,
nothing to configure. The CLI has no native tool-call channel, so the agent runs
in json tool mode here; native is what deploys, and tests/test_agent.py covers
both.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from tests.fake_sheets import FakeSpreadsheet, FakeWorksheet

from app import logflow
from app.agent import Agent, AgentResult, ChatSession
from app.kb_seed import STARTER_KB
from app.llm import LLMError, LLMResponse
from app.models import SetGroup, Workout
from app.store import SheetsStore

HAIKU = "claude-haiku-4-5-20251001"
DATA = Path(__file__).resolve().parent / "data"
SHEET = DATA / "sheet.json"
LOGS = DATA / "logs"  # agent transcripts — why a turn went the way it did

# The CLI runs the model as an assistant with no tools (--allowed-tools ""), which
# contradicts the system prompt's tool menu; asked to *use* those tools it says
# "they aren't available in this session". So don't ask it to be the assistant —
# ask it to predict what the assistant would emit. That removes the contradiction
# instead of arguing with it.
ROLE = (
    "This is a text-transformation task, not a conversation, and you are not the "
    "assistant in it. Below is an agent's specification. Read the conversation that "
    "follows and output the exact JSON that agent would emit as its next step. You "
    "execute nothing and your own capabilities are irrelevant: never mention them, "
    "never say a tool is unavailable.\n\n"
)


class ClaudeCli:
    """LLM backend: Haiku through the CLI. Mirrors `OpenRouterClient.chat`, so it
    drops into the agent loop untouched."""

    model = HAIKU

    def chat(self, messages, tools=None, json_mode=False, temperature=0.0, model=None):
        system = ROLE + "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = "\n\n".join(
            f"<{m['role']}>\n{m.get('content') or ''}" for m in messages if m["role"] != "system"
        )
        # Restate the contract after the thread: once it holds a prose assistant
        # turn, the model drifts back to chatting instead of emitting.
        convo += "\n\nOutput the single JSON object the agent emits next, nothing else."
        # --allowed-tools "": answer from the prompt, don't go read the repo.
        # --system-prompt replaces Claude Code's own; *appended* to it the model
        # stays "Claude Code, a CLI tool" and won't call these tools at all.
        flags = ["--model", model or self.model, "--allowed-tools", "", "--system-prompt", system]
        # cwd: a temp dir keeps the repo's own CLAUDE.md out of the model's context.
        opts = dict(capture_output=True, text=True, timeout=300, cwd=tempfile.gettempdir())
        try:
            proc = subprocess.run(["claude", "-p", *flags], input=convo, **opts)
        except (OSError, subprocess.SubprocessError) as e:
            raise LLMError(f"claude CLI failed: {e!r}") from e
        if proc.returncode != 0:
            raise LLMError(f"claude CLI exit {proc.returncode}: {proc.stderr[-300:]}")
        return LLMResponse(content=_first_json(proc.stdout))


def _first_json(text: str) -> str:
    """The first JSON object in the reply, or the reply unchanged.

    `claude -p` narrates, sometimes emitting a second candidate block after
    second-guessing itself. OpenRouter's json mode returns one bare object, so
    the mock normalises to that here — the shipping parser spans first `{` to
    last `}` and would choke on two blocks.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                return json.dumps(decoder.raw_decode(text, i)[0])
            except ValueError:
                continue
    return text.strip()


class LocalSheet(FakeSpreadsheet):
    """The mock spreadsheet, persisted to `path` as {tab: rows}.

    The unit suite's in-memory gspread fake plus load/save, so a run leaves an
    artifact you can open. Reused rather than reimplemented: the fiddly part (A1
    updates, append-after-last-non-empty-row, headers) is exactly what it encodes.
    """

    def __init__(self, path: Path = SHEET) -> None:
        super().__init__(title="local-sandbox", id="local-sheet")
        self.path = path
        if path.is_file():
            self._sheets = []
            for title, grid in json.loads(path.read_text()).items():
                self._sheets.append(FakeWorksheet(title))
                self._sheets[-1]._grid = [list(row) for row in grid]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({w.title: w._grid for w in self._sheets}, indent=1) + "\n")


class Sandbox:
    """One chat session against the mock backends, plus sheet assertions."""

    def __init__(self, reset: bool = False) -> None:
        if reset:
            SHEET.unlink(missing_ok=True)
        fresh = not SHEET.exists()
        self.sheet = LocalSheet()
        self.store = SheetsStore(self.sheet)
        self.store.bootstrap(kb_seed=STARTER_KB)  # idempotent: tabs, headers, KB
        if fresh:
            _seed(self.store)
        self.sheet.save()
        self.agent = Agent(client=ClaudeCli(), store=self.store, tool_mode="json", log_dir=LOGS)
        self.session = ChatSession("sandbox")

    def say(self, text: str, now: str | None = None) -> AgentResult:
        result = self.agent.run(self.session, text, now=now)
        self.sheet.save()
        return result

    def confirm(self, result: AgentResult, **edits) -> str:
        """Approve a pending card the way the web layer's ✅ does. Keyword args
        edit card fields first, as the user would: `confirm(r, date="2026-07-19")`.
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
        # belongs in the conversation; without it the next turn sees a workout
        # message with no reply and logs it a second time.
        self.session.add("assistant", summary)
        self.sheet.save()
        return summary

    def tools_used(self) -> list[str]:
        """Every tool the session actually invoked — read off the transcript, so
        read tools (which never reach AgentResult) show up too."""
        blob = "\n".join(str(m.get("content") or "") for m in self.session.messages)
        return re.findall(r"\[tool result:(\w+)\]", blob)

    def rows(self, tab: str) -> list[dict]:
        return self.store._ws(tab).get_all_records()

    def latest_id(self) -> str:
        return self.store.read_workouts()[-1].workout_id

    def _commit(self, pw, edits: dict, workout_id: str | None = None) -> str:
        for field, value in edits.items():
            setattr(pw, field, value)
        return logflow.commit_workout(self.store, pw, workout_id=workout_id)[1]

    def _parsed(self, wid: str):
        workout = next(w for w in self.store.read_workouts() if w.workout_id == wid)
        sets = [s for s in self.store.read_sets() if s.workout_id == wid]
        return logflow.workout_to_parsed(workout, sets)


def _seed(store: SheetsStore) -> None:
    """One past session, so a read or edit scenario still has something to find
    when the scenario before it failed."""
    wid, day = store.next_workout_id(), "2026-07-17"
    raw = "Squats 3x5 65 on each side"
    store.append_workout(
        Workout(workout_id=wid, date=day, type="strength", raw_text=raw, logged_at=day)
    )
    squat = dict(workout_id=wid, exercise="Back Squat", exercise_raw="Squats", grp=1, sets=3)
    store.append_sets([SetGroup(reps=5, per_side_lbs=65, total_lbs=175, **squat)])


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
    if result.inbox:  # tells "never triggered" apart from "triggered, args kept failing"
        problems.append("a write tool's args failed validation twice → sent to inbox")
    if sc.get("confirm"):
        sb.confirm(result, **sc.get("edits", {}))
    for tab, delta in sc.get("adds", {}).items():
        grew = len(sb.rows(tab)) - before[tab]
        if grew != delta:
            problems.append(f"{tab} rows +{grew}, expected +{delta}")

    head = f"{'ok  ' if not problems else 'FAIL'} {sc['say'][:52]!r} → {called or ['(answer)']}"
    return not problems, head + ("".join(f"\n       - {p}" for p in problems))
