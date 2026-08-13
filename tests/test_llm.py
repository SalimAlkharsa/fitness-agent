"""OpenRouterClient error handling — the paths that turn a bad upstream reply into
a clean failure instead of a silent blank turn."""

from __future__ import annotations

import pytest

import app.llm as llm
from app.llm import LLMError, OpenRouterClient


class _Resp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def test_http_200_with_error_body_raises_not_empty(monkeypatch):
    # OpenRouter returns provider failures (e.g. a rate-limited free tier) as
    # HTTP 200 with an `error` object and no `choices`. That must raise LLMError
    # after retries — never be parsed into empty content (which becomes a void reply).
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    err_body = {"error": {"message": "ResourceExhausted: limit reached", "code": 502}}
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _Resp(200, err_body))
    client = OpenRouterClient(api_key="k", retries=3)
    with pytest.raises(LLMError) as e:
        client.chat([{"role": "user", "content": "hi"}])
    assert "ResourceExhausted" in str(e.value)


def test_deadline_clamps_request_timeout_and_stops_retrying(monkeypatch):
    # A congested free tier used to burn retries × timeout (~254s) on one step —
    # past the platform's 300s function cap, so the activation was killed and the
    # browser polled into nothing. Walk that path: every request is clamped to the
    # remaining budget, backoff never overruns it, and it gives up while there's
    # still time for the caller to render an answer.
    # Fake clock: a hung request burns its full timeout, a sleep burns its delay.
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(llm.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    timeouts: list[float] = []

    def fake_post(*_a, **kw):
        timeouts.append(kw["timeout"])
        now[0] += kw["timeout"]  # upstream hangs until the timeout expires
        return _Resp(503, {})

    monkeypatch.setattr(llm.httpx, "post", fake_post)

    client = OpenRouterClient(api_key="k", timeout=60.0, retries=4)
    start, budget = now[0], 120.0
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}], deadline=start + budget)

    assert timeouts, "should have attempted at least one request"
    # The whole call lands inside the budget — this is the property that keeps a
    # turn under the platform cap. Unclamped it would run 4×60s + 14s backoff.
    assert now[0] - start <= budget


def test_http_200_with_choices_parses_normally(monkeypatch):
    ok_body = {"choices": [{"message": {"content": "pong", "tool_calls": []}}]}
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _Resp(200, ok_body))
    client = OpenRouterClient(api_key="k")
    assert client.chat([{"role": "user", "content": "ping"}]).content == "pong"
