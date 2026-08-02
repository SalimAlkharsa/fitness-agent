"""Agent-core tests with a mocked LLM — no network.

Covers the essential harness paths (happy, validation-retry, retry-exhausted→inbox,
max-steps cutoff, repeat-call guard), native/json mode switching by config only,
unknown-tool feedback, and history capping. Transcript logging is asserted on the
happy path.
"""

from __future__ import annotations

import json

from app.agent import MAX_STEPS, Agent, ChatSession
from app.kb_seed import STARTER_KB
from app.llm import LLMResponse
from app.models import SetGroup, Workout
from app.store import SheetsStore
from app.tools import default_tools
from tests.fake_sheets import FakeSpreadsheet


class FakeLLM:
    """Returns scripted LLMResponses; records the requests it saw."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, json_mode=False, **kw) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "json_mode": json_mode})
        return self._responses.pop(0)


def _store() -> SheetsStore:
    s = SheetsStore(FakeSpreadsheet())
    s.bootstrap(kb_seed=STARTER_KB)
    return s


def _seed_squat(store: SheetsStore) -> None:
    store.append_workout(
        Workout(workout_id="W0001", date="2026-07-10", type="strength", raw_text="x", logged_at="t")
    )
    store.append_sets(
        [
            SetGroup(
                workout_id="W0001",
                exercise="Back Squat",
                exercise_raw="squats",
                grp=1,
                sets=3,
                reps=5,
                per_side_lbs=60,
                total_lbs=165,
            )
        ]
    )


def _json(obj) -> LLMResponse:
    return LLMResponse(content=json.dumps(obj))


def _agent(llm, store, mode="json", **kw) -> Agent:
    return Agent(client=llm, tools=default_tools(), store=store, tool_mode=mode, **kw)


def test_happy_path_query_then_answer_and_writes_transcript(tmp_path):
    store = _store()
    _seed_squat(store)
    llm = FakeLLM(
        [
            _json({"tool": "query_history", "args": {"exercise": "Back Squat"}}),
            _json({"answer": "Your last squat was 3x5 @ 165."}),
        ]
    )
    agent = _agent(llm, store, log_dir=tmp_path)
    session = ChatSession("s1")
    result = agent.run(session, "what did I squat last?")

    assert result.pending is None
    assert result.text == "Your last squat was 3x5 @ 165."
    # the read tool actually ran (its result landed in the transcript)
    assert any("[tool result:query_history]" in m["content"] for m in session.messages)
    assert len(llm.calls) == 2
    # a jsonl transcript was written with the key events
    logs = list(tmp_path.glob("agent-*.jsonl"))
    assert len(logs) == 1
    kinds = {json.loads(line)["event"] for line in logs[0].read_text().splitlines()}
    assert {"user_message", "model_response"} <= kinds


def test_log_workout_returns_pending_with_parsed_sets(tmp_path):
    store = _store()
    parsed = {
        "tool": "log_workout",
        "args": {
            "workouts": [
                {
                    "date": "2026-07-14",
                    "type": "strength",
                    "raw_text": "Squats 3x5 60lbs each side",
                    "sets": [
                        {
                            "exercise": "Back Squat",
                            "exercise_raw": "Squats",
                            "grp": 1,
                            "sets": 3,
                            "reps": 5,
                            "per_side_lbs": 60,
                            "total_lbs": 165,
                        }
                    ],
                }
            ]
        },
    }
    llm = FakeLLM([_json(parsed)])
    agent = _agent(llm, store, log_dir=tmp_path)
    result = agent.run(ChatSession("s2"), "log this workout: Squats 3x5 60lbs each side")

    assert result.text is None and result.pending is not None
    assert result.pending.tool == "log_workout"
    pr = result.pending.args  # ParseResult
    assert len(pr.workouts) == 1 and len(pr.workouts[0].sets) == 1
    assert pr.workouts[0].sets[0].exercise == "Back Squat"
    assert pr.workouts[0].sets[0].total_lbs == 165
    assert "Back Squat 3x5" in result.pending.summary
    assert store.read_workouts() == []  # nothing written — confirm gate holds


def test_validation_error_retries_then_succeeds(tmp_path):
    store = _store()
    bad = {"tool": "log_workout", "args": {"workouts": [{"type": "strength"}]}}  # no date/raw_text
    good = {
        "tool": "log_workout",
        "args": {
            "workouts": [
                {
                    "date": "2026-07-14",
                    "raw_text": "Squats 3x5 60 each side",
                    "sets": [
                        {
                            "exercise": "Back Squat",
                            "exercise_raw": "Squats",
                            "grp": 1,
                            "sets": 3,
                            "reps": 5,
                            "per_side_lbs": 60,
                            "total_lbs": 165,
                        }
                    ],
                }
            ]
        },
    }
    llm = FakeLLM([_json(bad), _json(good)])
    agent = _agent(llm, store, log_dir=tmp_path)
    session = ChatSession("s3")
    result = agent.run(session, "log squats")

    assert result.pending is not None  # succeeded after the retry
    assert any("[tool error]" in m["content"] for m in session.messages)  # error fed back
    assert len(llm.calls) == 2


def test_validation_exhausted_goes_to_inbox(tmp_path):
    store = _store()
    bad = _json({"tool": "log_workout", "args": {"workouts": [{"type": "strength"}]}})  # invalid
    llm = FakeLLM([bad, bad, bad])  # 3 failures: 1,2 fed back, 3rd → inbox
    agent = _agent(llm, store, log_dir=tmp_path)
    result = agent.run(ChatSession("s4"), "log garbled thing")

    assert result.inbox is True and "inbox" in (result.text or "").lower()
    inbox_rows = store._ws("inbox").get_all_records()
    assert inbox_rows and inbox_rows[0]["raw_text"] == "log garbled thing"
    assert len(llm.calls) == 3


def test_max_steps_cutoff(tmp_path):
    store = _store()
    # distinct read-tool calls each step (so the repeat guard doesn't fire first) →
    # the loop never gets a final answer and stops cleanly at MAX_STEPS.
    calls = [
        _json({"tool": "query_history", "args": {"exercise": f"ex{i}"}}) for i in range(MAX_STEPS)
    ]
    agent = _agent(FakeLLM(calls), store, log_dir=tmp_path)
    result = agent.run(ChatSession("s5"), "loop forever")

    assert result.pending is None and "couldn't" in (result.text or "").lower()
    assert len(store.read_workouts()) == 0  # a run-away read loop never writes


def test_repeat_call_guard_breaks_the_loop(tmp_path):
    store = _store()
    _seed_squat(store)
    # the same read call twice running = the model is spinning; the guard breaks it
    same = {"tool": "query_history", "args": {"exercise": "Back Squat"}}
    agent = _agent(FakeLLM([_json(same), _json(same)]), store, log_dir=tmp_path)
    session = ChatSession("s5b")
    result = agent.run(session, "squat squat squat")

    assert result.pending is None and "couldn't" in (result.text or "").lower()
    assert len(session.messages)  # the first call ran; the second tripped the guard


def test_native_mode_extracts_tool_call_and_mode_defaults_from_config(tmp_path):
    store = _store()
    _seed_squat(store)
    native_call = LLMResponse(
        content="",
        tool_calls=[
            {
                "id": "c1",
                "function": {
                    "name": "query_history",
                    "arguments": json.dumps({"exercise": "Back Squat"}),
                },
            }
        ],
    )
    llm = FakeLLM([native_call, LLMResponse(content="Here you go.")])
    agent = _agent(llm, store, mode="native", log_dir=tmp_path)
    result = agent.run(ChatSession("s6"), "squat history")

    assert result.text == "Here you go."
    assert llm.calls[0]["tools"] is not None  # native tool specs were sent
    assert llm.calls[0]["json_mode"] is False

    # mode is config-driven when unspecified → switching is config-only, no code change
    from app.agent import settings

    assert Agent(client=FakeLLM([]), store=None).tool_mode == settings.tool_mode
    assert Agent(client=FakeLLM([]), store=None, tool_mode="json").tool_mode == "json"


def test_unknown_tool_is_fed_back(tmp_path):
    store = _store()
    llm = FakeLLM([_json({"tool": "nonexistent", "args": {}}), _json({"answer": "ok"})])
    agent = _agent(llm, store, log_dir=tmp_path)
    session = ChatSession("s7")
    result = agent.run(session, "hi")
    assert result.text == "ok"
    assert any("unknown tool" in m["content"] for m in session.messages)


def test_history_is_capped_when_sent_to_model(tmp_path):
    from app.agent import MAX_HISTORY_MESSAGES

    store = _store()
    session = ChatSession("cap")
    for i in range(100):  # a long history the model shouldn't be flooded with
        session.add("user", f"msg {i}")
    llm = FakeLLM([_json({"answer": "ok"})])
    agent = _agent(llm, store, log_dir=tmp_path)
    agent.run(session, "latest")

    sent = llm.calls[0]["messages"]
    # system prompt + at most MAX_HISTORY_MESSAGES history entries
    assert len(sent) <= MAX_HISTORY_MESSAGES + 1
    assert sent[0]["role"] == "system"


class RaisingLLM:
    """Raises the given exception on chat() — models an upstream failure."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def chat(self, *a, **k):
        raise self.exc


def test_llm_error_returns_model_unavailable_not_blank(tmp_path):
    from app.llm import LLMError

    agent = _agent(RaisingLLM(LLMError("rate limited")), _store(), log_dir=tmp_path)
    result = agent.run(ChatSession("err"), "log squats 3x5")
    # A model failure must surface a clear message, never a blank/void turn or a raise.
    assert result.pending is None
    assert result.text and "try again" in result.text.lower()


def test_empty_model_answer_is_graceful_not_blank(tmp_path):
    # A 200-with-no-content response (e.g. a rate-limited free tier) must not
    # render as an empty bubble — surface it as a model-unavailable message.
    agent = _agent(FakeLLM([LLMResponse(content="")]), _store(), log_dir=tmp_path)
    result = agent.run(ChatSession("empty"), "hello?")
    assert result.text and "try again" in result.text.lower()


def test_model_date_is_overridden_for_an_undated_message(tmp_path):
    # The free-tier model answers undated logs with a training-era date even
    # though the prompt carries the right one — the code must win (app/dates.py).
    parsed = {
        "tool": "log_workout",
        "args": {
            "workouts": [
                {
                    "date": "2025-08-20",  # what the model actually returned
                    "type": "strength",
                    "raw_text": "",
                    "sets": [
                        {
                            "exercise": "Back Squat",
                            "exercise_raw": "Squats",
                            "grp": 1,
                            "sets": 4,
                            "reps": 7,
                            "per_side_lbs": 60,
                            "total_lbs": 165,
                        }
                    ],
                }
            ]
        },
    }
    agent = _agent(FakeLLM([_json(parsed)]), _store(), log_dir=tmp_path)
    result = agent.run(
        ChatSession("dates"),
        "Squats 4x7 60lbs on each side",  # no date stated anywhere
        now="Friday 2026-07-24 13:04 America/Chicago",
    )
    assert result.pending.args.workouts[0].date == "2026-07-24"
