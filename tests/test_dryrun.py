"""Dry-run mode (local UI testing without spending OpenRouter quota).

Covers the DryRunClient contracts: a workout-looking message yields a confirm
card through the agent loop, a question yields a canned answer (no card / no
network), and the direct parse path (backfill) returns a bare ParseResult.
"""

from __future__ import annotations

from app.agent import Agent, ChatSession
from app.llm import DryRunClient, make_client
from app.parse import parse_workouts


def test_make_client_returns_dryrun_when_flag_set(monkeypatch):
    import dataclasses

    import app.llm as llm

    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, dry_run=True))
    assert isinstance(make_client(), DryRunClient)


def test_workout_message_yields_confirm_card():
    agent = Agent(client=DryRunClient(), store=None)
    result = agent.run(ChatSession("t"), "bench 3x10 at 135", now="2026-07-15T10:00")
    assert result.pending is not None
    assert result.pending.tool == "log_workout"
    assert result.pending.args.workouts  # a card will render


def test_question_yields_canned_answer_without_card():
    agent = Agent(client=DryRunClient(), store=None)
    result = agent.run(ChatSession("t"), "how am I progressing on squats?", now="2026-07-15T10:00")
    assert result.pending is None
    assert result.text and "Dry-run" in result.text


def test_parse_path_returns_bare_parse_result():
    pr = parse_workouts(
        "did squats 5x5 200 on each side", [], DryRunClient(), now="2026-07-15T10:00", backfill=True
    )
    assert len(pr.workouts) == 1
    # Non-workout lines parse to nothing rather than a bogus stub.
    empty = parse_workouts("rest day", [], DryRunClient(), now="2026-07-15T10:00")
    assert empty.workouts == []
