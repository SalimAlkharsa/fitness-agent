"""OpenRouter chat client.

One small httpx wrapper used by both the agent loop and the eval harness.
Supports native tool-calling (`tools=`) and JSON mode (`json_mode=True`), with
backoff on 429/5xx.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """Non-retryable failure talking to OpenRouter (or retries exhausted)."""


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        retries: int = 4,
    ) -> None:
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.model
        self.timeout = timeout
        self.retries = retries

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set.")
        payload: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        delay, last_err = 2.0, ""
        for _attempt in range(self.retries):
            try:
                r = httpx.post(OPENROUTER_URL, headers=headers, json=payload, timeout=self.timeout)
            except httpx.HTTPError as e:
                last_err = repr(e)
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                body = r.json()
                # OpenRouter returns upstream provider failures as HTTP 200 with an
                # `error` object and no `choices` (e.g. "ResourceExhausted" on a
                # rate-limited free tier). Treat that as retryable, never as empty
                # content — otherwise the agent silently replies with a blank turn.
                err = body.get("error")
                if err or not body.get("choices"):
                    msg = err.get("message") if isinstance(err, dict) else err
                    last_err = f"upstream error: {msg}"
                    time.sleep(delay)
                    delay *= 2
                    continue
                return self._parse(body)
            if r.status_code in _RETRYABLE:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(delay)
                delay *= 2
                continue
            raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:300]}")
        raise LLMError(f"OpenRouter failed after {self.retries} attempts: {last_err}")

    @staticmethod
    def _parse(body: dict) -> LLMResponse:
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            raw=body,
        )


# Set/rep shorthand (3x10, 4 x 8) — the cheap "this looks like a workout" signal.
_SETS_RE = re.compile(r"\b\d+\s*[x×]\s*\d+\b", re.IGNORECASE)


class DryRunClient:
    """Offline stand-in for OpenRouterClient (DRY_RUN=1).

    Serves canned responses so the chat UI, confirm cards, and flows can be
    exercised locally without spending the OpenRouter free-tier quota. It does no
    real parsing: a workout-looking message yields a placeholder log_workout
    confirm card, anything else a dry-run notice. The signature mirrors
    OpenRouterClient.chat so it drops into the agent loop.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.model = "dry-run"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> LLMResponse:
        # The direct parse path (backfill) expects a bare ParseResult, not a tool
        # envelope. Detect it and answer in that shape.
        parse_msg = _parse_request_message(messages)
        if parse_msg is not None:
            result = _stub_parse_result(parse_msg) if _looks_like_workout(parse_msg) else {}
            return LLMResponse(content=json.dumps(result))

        user_text = _last_user_text(messages)
        # Once a tool has run, answer rather than call another one, so the agent
        # loop terminates instead of spinning.
        acted = any(
            m.get("role") == "tool" or str(m.get("content", "")).startswith("[tool result")
            for m in messages
        )

        if not acted and user_text and _looks_like_workout(user_text):
            args = _stub_parse_result(user_text)
            if json_mode:
                return LLMResponse(content=json.dumps({"tool": "log_workout", "args": args}))
            call = {
                "id": "dryrun-log_workout",
                "type": "function",
                "function": {"name": "log_workout", "arguments": json.dumps(args)},
            }
            return LLMResponse(content="", tool_calls=[call])

        answer = (
            "🧪 Dry-run mode — no LLM was called, so this is a canned reply "
            "(free-tier quota untouched). Unset DRY_RUN to use the real model."
        )
        if json_mode:
            return LLMResponse(content=json.dumps({"answer": answer}))
        return LLMResponse(content=answer)


def _parse_request_message(messages: list[dict]) -> str | None:
    """The raw workout message if these are a direct parse request
    (app.prompt.build_parse_messages), else None — recognised by that prompt's
    distinctive 'Message timestamp: ...\\n\\nMessage:\\n<msg>' user turn."""
    for m in messages:
        if m.get("role") != "user":
            continue
        content = str(m.get("content", ""))
        marker = "\n\nMessage:\n"
        if content.startswith("Message timestamp:") and marker in content:
            return content.split(marker, 1)[1]
    return None


def _last_user_text(messages: list[dict]) -> str:
    """The most recent genuine user turn, skipping the tool-error feedback the
    loop injects under the same 'user' role."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = str(m.get("content", ""))
        if content.startswith("[tool "):
            continue
        return content
    return ""


def _looks_like_workout(text: str) -> bool:
    if "?" in text:
        return False
    if _SETS_RE.search(text):
        return True
    # Digits and no leading question word → probably a log, not a query.
    lowered = text.strip().lower()
    question_words = ("how", "what", "when", "why", "show", "chart", "list", "which", "did i")
    if lowered.startswith(question_words):
        return False
    return any(ch.isdigit() for ch in text)


def _stub_parse_result(user_text: str) -> dict:
    """A minimal, schema-valid ParseResult so a confirm card renders. The exercise
    is deliberately KB-unknown so the 'known / new?' resolution UI shows too."""
    return {
        "workouts": [
            {
                "date": datetime.date.today().isoformat(),
                "type": "strength",
                "raw_text": user_text,
                "notes": "dry-run placeholder — not parsed by an LLM",
                "sets": [
                    {
                        "exercise": "dry run exercise",
                        "exercise_raw": user_text[:40],
                        "grp": 1,
                        "sets": 3,
                        "reps": 10,
                        "per_side_lbs": 45.0,
                        "total_lbs": 135.0,
                    }
                ],
            }
        ]
    }


def make_client(**kwargs) -> OpenRouterClient | DryRunClient:
    """The offline DryRunClient when DRY_RUN is set, otherwise a real
    OpenRouterClient. Keyword args pass through (ignored in dry-run)."""
    if settings.dry_run:
        return DryRunClient()
    return OpenRouterClient(**kwargs)
