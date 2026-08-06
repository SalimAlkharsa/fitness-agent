"""Hand-rolled agent loop — no framework.

A message runs through one linear dispatch loop (max MAX_STEPS). Each step the
model either answers in plain text (done), calls a *read* tool (executed
deterministically in-process, its result fed back as a proper `tool` message),
or calls a *write* tool (confirm-gated: the loop short-circuits and returns a
pending-confirmation payload the web layer renders as a card). The model picks
from a small, fixed menu of typed tools; it never authors or runs code.

Guards keep a weak free-tier model from burning the daily quota:
- validation errors on a *write* tool are fed back for up to MAX_VALIDATION_RETRIES,
  then the raw message goes to the `inbox` tab;
- validation errors on a *read* tool are fed back too, but never write to inbox;
- an identical tool call two steps running, or a second unknown-tool call, breaks
  the loop to a graceful answer instead of spinning.

Two tool-selection modes, switchable by config only (`TOOL_MODE`):
- native: OpenRouter function/tool-calling (`tools=`), reading `tool_calls`. The
  assistant tool-call turn and a `role:tool` result are recorded per the API spec.
- json:   the model emits `{"tool": ..., "args": ...}` or `{"answer": ...}` and
          the loop parses that from the message content; results feed back as
          marked text turns (there is no native tool channel in this mode).
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app import dates, skills
from app.config import settings
from app.kb_seed import STARTER_KB
from app.llm import LLMError, LLMResponse, OpenRouterClient, make_client
from app.models import InboxItem, KBEntry
from app.prompt import BASE_PARSE_RULES, render_kb
from app.tools import Tool, ToolContext, default_tools

# One LLM call per step. The longest legitimate flow is read_skill -> a read tool
# -> describe_data -> make_chart -> final answer, so 6 is ample, and it bounds one
# message to a small slice of the 50/day free-tier cap.
MAX_STEPS = 6
MAX_VALIDATION_RETRIES = 2
# Bounds context/cost for a long-lived session; each message is largely
# self-contained, so an old tail is little loss.
MAX_HISTORY_MESSAGES = 24
# Relative dates ("today", "did legs Monday") resolve against the user's home tz,
# not the server's, so a late-night log doesn't land on the wrong calendar day.
HOME_TZ = "America/Chicago"


def enriched_now() -> str:
    """Current time as 'Weekday YYYY-MM-DD HH:MM <tz>' for the prompt.

    The weekday and explicit timezone give the model what it needs for
    'yesterday' / 'Monday'. Falls back to a bare local ISO stamp if the tz
    database isn't available on the host.
    """
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.datetime.now(ZoneInfo(HOME_TZ))
        return dt.strftime("%A %Y-%m-%d %H:%M ") + HOME_TZ
    except Exception:  # noqa: BLE001 — missing tzdata etc.; a stamp is better than nothing
        return datetime.datetime.now().isoformat(timespec="minutes")


@dataclass
class PendingConfirmation:
    """A confirm-gated tool call awaiting the user's ✅/❌."""

    tool: str
    args: BaseModel
    summary: str


@dataclass
class AgentResult:
    text: str | None = None
    pending: PendingConfirmation | None = None
    inbox: bool = False
    charts: list[str] = field(default_factory=list)  # inline SVG strings to render


@dataclass
class ChatSession:
    """In-memory conversation state for one chat (single user → fine)."""

    session_id: str
    messages: list[dict] = field(default_factory=list)

    def add(self, role: str, content: str, **extra) -> None:
        msg = {"role": role, "content": content}
        msg.update(extra)
        self.messages.append(msg)


def _tools_catalog(tools: list[Tool]) -> str:
    """The tool menu with arg schemas, rendered into the system prompt in both
    modes so the model always knows exactly what exists."""
    blocks = []
    for t in tools:
        blocks.append(f"- {t.name}: {t.description}\n  args schema: {json.dumps(t.json_schema())}")
    return "\n".join(blocks)


def build_system_prompt(
    tools: list[Tool],
    kb: list[KBEntry],
    tool_mode: str,
    skill_index: str = "",
    now: str = "",
) -> str:
    parts = [
        "You are a personal workout-logging assistant for one user (Salim).",
        "You log workouts, answer questions about training history, and make charts.",
    ]
    if now:
        parts.append(
            f"The current date and time is {now}. Resolve 'today', 'yesterday', a "
            f"weekday like 'Monday', and any workout with no stated date against this."
        )
    parts += [
        "",
        BASE_PARSE_RULES,
        "",
        "Knowledge base (exercise_kb):",
        render_kb(kb),
        "",
        "Available tools (this is the complete menu — you cannot run arbitrary code):",
        _tools_catalog(tools),
    ]
    if skill_index:
        parts += [
            "",
            "Skills you can read on demand with the read_skill tool:",
            skill_index,
            "Before parsing a message that logs a workout, read the parsing skill. "
            "Before answering analysis questions or making charts, read the relevant "
            "skill.",
        ]
    parts += [
        "",
        "How to work: each turn, either call ONE tool or give the user your final "
        "answer in plain text. Call a tool when logging or querying; when you have "
        "what you need, answer directly. Don't repeat the same tool call.",
        "Correcting something already logged (wrong weight/reps/date on a past entry) "
        "→ edit_workout, never log_workout — log_workout would create a duplicate "
        "instead of fixing the existing row.",
        "",
        "Answering questions (pick the ONE tool that fits — you cannot run code):",
        "- Orient with describe_data when unsure what's logged (exercises, date range).",
        "- Progress/trend on an exercise → exercise_stats (volume/top_set/pr/frequency); "
        "for a visual, also call make_chart (it renders inline automatically) and add one "
        "sentence interpreting the trend.",
        "- 'What are my PRs' (all exercises) → personal_records; a rep-specific PR too.",
        "- 'How many times did I do/play X' → query_history's `session_count`/`sessions` "
        "(sport sessions like basketball have no set rows, so set counts miss them).",
        "- 'What did I do last time' → the most recent date in query_history; report "
        "per-side and total weight.",
    ]
    if tool_mode == "json":
        parts += [
            "",
            "Respond with a SINGLE JSON object and nothing else. To call a tool: "
            '{"tool": "<name>", "args": {<matching the tool arg schema>}}. '
            'To answer the user directly: {"answer": "<text>"}.',
        ]
    else:
        parts += [
            "",
            "Call a tool when logging or querying; otherwise answer directly.",
        ]
    return "\n".join(parts)


class Agent:
    def __init__(
        self,
        client: OpenRouterClient | None = None,
        tools: list[Tool] | None = None,
        store=None,
        tool_mode: str | None = None,
        skill_index: str = "",
        log_dir: Path | str = "logs",
    ) -> None:
        self.client = client or make_client()
        self.tools = {t.name: t for t in (tools if tools is not None else default_tools())}
        self.store = store
        self.tool_mode = tool_mode or settings.tool_mode
        # Default to the globbed skill index so dropping in a new skills/*/SKILL.md
        # needs no code change; callers can still pass an explicit index.
        self.skill_index = skill_index or skills.skill_index()
        self.log_dir = Path(log_dir)

    # --- transcript ---

    def _log(self, session_id: str, event: str, data: dict) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.date.today().isoformat()
            line = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "session": session_id,
                "event": event,
                **data,
            }
            with (self.log_dir / f"agent-{day}.jsonl").open("a") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError:
            pass  # transcript logging must never break a request

    # --- context ---

    def _kb(self) -> list[KBEntry]:
        if self.store is not None:
            try:
                return self.store.kb_entries()
            except Exception:  # noqa: BLE001 — fall back if the sheet is unreachable
                return list(STARTER_KB)
        return list(STARTER_KB)

    def _history(self, session: ChatSession) -> list[dict]:
        """Most recent slice of the conversation, safe to send to the model.

        A `tool` message must be preceded by the assistant turn that requested it;
        if the tail slice starts mid-pair (the assistant turn was trimmed off),
        drop the orphan tool messages so native mode doesn't reject the request.
        """
        hist = session.messages[-MAX_HISTORY_MESSAGES:]
        while hist and hist[0].get("role") == "tool":
            hist = hist[1:]
        return hist

    # --- tool-call extraction / recording (mode-aware, proper roles) ---

    def _extract_call(self, resp: LLMResponse) -> tuple[str, dict, str | None] | None:
        """Return (tool_name, raw_args, call_id) if the model wants a tool, else None.

        call_id is the native tool_call id (None in json mode, which has no channel).
        """
        if self.tool_mode == "native":
            if resp.tool_calls:
                tc = resp.tool_calls[0]
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                return name, args, tc.get("id")
            return None
        # json mode: parse the content
        obj = _safe_json(resp.content)
        if isinstance(obj, dict) and "tool" in obj:
            return str(obj["tool"]), obj.get("args") or {}, None
        return None

    def _feedback(
        self,
        session: ChatSession,
        resp: LLMResponse,
        name: str,
        call_id: str | None,
        payload,
        *,
        is_error: bool,
    ) -> None:
        """Record the model's tool-call turn and its result, with proper roles.

        Every path that keeps the loop going (tool result, validation error,
        unknown tool) goes through here, so the assistant tool-call turn and its
        `tool` reply are always written as a matched pair — native mode rejects a
        `tool` message not preceded by the call that produced it. Paths that return
        immediately (pending / inbox / graceful) record nothing, leaving no
        assistant turn dangling without a reply.

        Native: an assistant message carrying `tool_calls`, then a `tool` message.
        JSON: the assistant's JSON content, then a marked text turn (no tool channel).
        """
        body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        if self.tool_mode == "native":
            session.add("assistant", resp.content or "", tool_calls=resp.tool_calls)
            session.add("tool", body, tool_call_id=call_id or f"call_{name}", name=name)
        else:
            session.add("assistant", resp.content or "")
            marker = "[tool error]" if is_error else f"[tool result:{name}]"
            session.add("user", f"{marker} {body}")

    def _final_text(self, resp: LLMResponse) -> str:
        if self.tool_mode == "json":
            obj = _safe_json(resp.content)
            if isinstance(obj, dict) and "answer" in obj:
                return str(obj["answer"])
        return resp.content

    # --- the loop ---

    def run(self, session: ChatSession, user_text: str, now: str | None = None) -> AgentResult:
        kb = self._kb()
        now = now or enriched_now()
        system = build_system_prompt(
            list(self.tools.values()), kb, self.tool_mode, self.skill_index, now
        )
        session.add("user", user_text)
        self._log(session.session_id, "user_message", {"text": user_text})

        native_tools = self._native_tool_specs() if self.tool_mode == "native" else None
        ctx = ToolContext(store=self.store, kb=kb, user_text=user_text)
        write_failures = 0
        read_failures = 0
        unknown_calls = 0
        last_call_key: str | None = None
        chart_urls: list[str] = []

        for _step in range(MAX_STEPS):
            messages = [{"role": "system", "content": system}, *self._history(session)]
            try:
                resp = self.client.chat(
                    messages,
                    tools=native_tools,
                    json_mode=(self.tool_mode == "json"),
                )
            except LLMError as e:
                # Model unreachable / rate-limited / retries exhausted — say so
                # instead of returning a blank turn or a 500.
                self._log(session.session_id, "llm_error", {"error": str(e)})
                return self._model_unavailable(session)
            self._log(
                session.session_id,
                "model_response",
                {"content": resp.content, "tool_calls": resp.tool_calls},
            )

            call = self._extract_call(resp)
            if call is None:
                text = self._final_text(resp)
                # An empty completion is never a real answer: a rate-limited free
                # tier often returns HTTP 200 with no content. Blame the model
                # explicitly rather than rendering an empty bubble.
                if not (text or "").strip():
                    return self._model_unavailable(session)
                session.add("assistant", text)
                return AgentResult(text=text, charts=chart_urls)

            name, raw_args, call_id = call
            tool = self.tools.get(name)

            if tool is None:
                unknown_calls += 1
                if unknown_calls > 1:  # told the real menu once already; stop spinning
                    return self._graceful(session, chart_urls)
                self._feedback(
                    session,
                    resp,
                    name,
                    call_id,
                    f"unknown tool '{name}'. Available: {', '.join(self.tools)}.",
                    is_error=True,
                )
                continue

            try:
                args = tool.args_model.model_validate(raw_args)
            except ValidationError as e:
                self._log(
                    session.session_id,
                    "validation_error",
                    {"tool": name, "errors": e.errors(include_url=False)},
                )
                # Write tools fall back to the inbox so a workout is never lost;
                # read tools just apologize. Separate retry counters mean a model
                # repeating one bad call still terminates without the repeat-guard.
                if tool.confirm_required:
                    write_failures += 1
                    if write_failures > MAX_VALIDATION_RETRIES:
                        return self._to_inbox(session, user_text, str(e))
                else:
                    read_failures += 1
                    if read_failures > MAX_VALIDATION_RETRIES:
                        return self._graceful(session, chart_urls)
                self._feedback(
                    session,
                    resp,
                    name,
                    call_id,
                    f"your call to {name} failed validation: {_short_errors(e)}. "
                    "Fix the args and try again.",
                    is_error=True,
                )
                continue

            # A log_workout that parsed to zero workouts would render an empty
            # confirm card — treat it as a retryable write failure, not a no-op.
            if name == "log_workout" and not getattr(args, "workouts", None):
                write_failures += 1
                if write_failures > MAX_VALIDATION_RETRIES:
                    return self._to_inbox(session, user_text, "log_workout produced no workouts")
                self._feedback(
                    session,
                    resp,
                    name,
                    call_id,
                    "log_workout produced no workouts. Pass a non-empty "
                    '"workouts" array of workout objects (each with a "sets" list) '
                    "matching the schema, not a single stringified object.",
                    is_error=True,
                )
                continue

            # Every workout keeps the original message for audit, so backfill
            # raw_text rather than making the model echo it back.
            if name == "log_workout":
                for w in args.workouts:
                    if not w.raw_text:
                        w.raw_text = user_text
                # Dates are decided in code, not by the model: it ignores the
                # injected current date and answers from its training era. See
                # app/dates.py. The card stays editable if we get it wrong.
                dates.apply_to_workouts(args.workouts, user_text, self._today(now))

            if tool.confirm_required:
                summary = tool.summarize(args) if tool.summarize else name
                self._log(
                    session.session_id,
                    "pending_confirmation",
                    {"tool": name, "summary": summary},
                )
                return AgentResult(
                    pending=PendingConfirmation(tool=name, args=args, summary=summary)
                )

            # Repeat-call guard: the same read two steps running means the model is
            # ignoring the result and spinning — break rather than burn quota.
            call_key = f"{name}:{json.dumps(raw_args, sort_keys=True, default=str)}"
            if call_key == last_call_key:
                return self._graceful(session, chart_urls)
            last_call_key = call_key

            try:
                result = tool.handler(args, ctx)
            except Exception as e:  # noqa: BLE001 — surface to the model, keep looping
                self._feedback(session, resp, name, call_id, f"{name} raised: {e}", is_error=True)
                continue
            if name == "make_chart" and isinstance(result, dict) and result.get("svg"):
                # Keep the SVG out of the model's context — it renders inline in
                # the chat; the model only needs to know the chart happened.
                chart_urls.append(result.pop("svg"))
                result["chart"] = "rendered inline below your answer"
            self._log(session.session_id, "tool_result", {"tool": name, "result": result})
            self._feedback(session, resp, name, call_id, result, is_error=False)

        self._log(session.session_id, "max_steps", {})
        return self._graceful(session, chart_urls)

    @staticmethod
    def _today(now: str) -> datetime.date:
        """Today's date in the home tz, read off the same `now` string the prompt
        carries — so pinning `now` also pins the date logic."""
        match = re.search(r"\d{4}-\d{2}-\d{2}", now or "")
        if match:
            return datetime.date.fromisoformat(match.group(0))
        return datetime.datetime.now().date()

    def _graceful(self, session: ChatSession, chart_urls: list[str]) -> AgentResult:
        """Stop cleanly when the closed menu can't get there (stuck, exhausted,
        or a request nothing on the menu can express)."""
        self._log(session.session_id, "graceful_stop", {})
        text = "Sorry — I couldn't work that out precisely. Could you rephrase?"
        session.add("assistant", text)
        return AgentResult(text=text, charts=chart_urls)

    def _model_unavailable(self, session: ChatSession) -> AgentResult:
        """The model errored out (rate limit / upstream failure). Say so plainly so
        the user retries, rather than showing a blank turn."""
        text = (
            "⚠️ The model is busy right now (free-tier limit or a hiccup upstream). "
            "Your message wasn't logged — please try again in a minute."
        )
        session.add("assistant", text)
        return AgentResult(text=text)

    def _to_inbox(self, session: ChatSession, user_text: str, error: str) -> AgentResult:
        if self.store is not None:
            try:
                self.store.to_inbox(
                    InboxItem(
                        timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
                        raw_text=user_text,
                        error=error[:500],
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        self._log(session.session_id, "inbox", {"error": error[:500]})
        text = (
            "I couldn't parse that cleanly, so I saved the raw text to your inbox "
            "to sort out later."
        )
        session.add("assistant", text)
        return AgentResult(text=text, inbox=True)

    def _native_tool_specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.json_schema(),
                },
            }
            for t in self.tools.values()
        ]


def _safe_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _short_errors(e: ValidationError) -> str:
    errs = e.errors(include_url=False)
    return "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in errs[:5])
