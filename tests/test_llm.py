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


def test_http_200_with_choices_parses_normally(monkeypatch):
    ok_body = {"choices": [{"message": {"content": "pong", "tool_calls": []}}]}
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _Resp(200, ok_body))
    client = OpenRouterClient(api_key="k")
    assert client.chat([{"role": "user", "content": "ping"}]).content == "pong"
