"""Direct parse helper shared by the eval and backfill.

Builds a parse request through the same prompt/schema as live logging
(`app.prompt`) and returns a validated ParseResult. Live chat logging goes through
the agent's log_workout tool; backfill and the eval call this directly so they
don't need the full tool loop.
"""

from __future__ import annotations

from app.evals import extract_json
from app.llm import OpenRouterClient
from app.models import KBEntry, ParseResult
from app.prompt import build_parse_messages

_BACKFILL_GUIDANCE = (
    "This is a historical backfill entry. If the message does not clearly state a "
    'date, set the workout date to "unknown" and do not guess a date.'
)


def parse_workouts(
    message: str,
    kb: list[KBEntry],
    client: OpenRouterClient,
    now: str = "",
    backfill: bool = False,
) -> ParseResult:
    guidance = _BACKFILL_GUIDANCE if backfill else None
    messages = build_parse_messages(message, kb, now, extra_guidance=guidance)
    reply = client.chat(messages, json_mode=True).content
    return ParseResult.model_validate(extract_json(reply))
