"""Stateless confirm cards.

Serverless instances share nothing, so a pending card is never held in process
memory: the whole card is serialized, HMAC-signed (itsdangerous), and
round-tripped through a hidden form field. Any instance can then verify and
commit it. Tokens expire after CARD_MAX_AGE; tampering fails the signature.

Signed tokens stay replayable until they expire (there is no server-side
"consumed" bit), so the commit path pairs this with an exact-duplicate guard —
see the confirm handlers in app.web.
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from app import config
from app.dedup import DuplicateMatch
from app.logflow import UnknownExercise
from app.models import ParsedWorkout

CARD_MAX_AGE = 60 * 60  # seconds — a card left on screen an hour is stale
_SALT = "fitness-agent-card"

# Async /chat plumbing (see app/dofn.py). A job blob carries the message +
# client-held history into the background worker; an activation token carries the
# id the browser polls. Both are signed so a browser can neither forge a worker
# call nor poll an arbitrary activation.
_JOB_SALT = "fitness-agent-job"
_ACT_SALT = "fitness-agent-activation"
JOB_MAX_AGE = 120  # a dispatched job is consumed within seconds
ACT_MAX_AGE = 600  # poll window: comfortably longer than the worst parse


class PendingCard(BaseModel):
    """A confirm card awaiting ✅/❌.

    kind 'log' → a single parsed workout (+ any unknown-exercise questions).
    kind 'edit' → the same editable form, prefilled from an already-logged
        workout (workout_id set); confirming replaces that workout in place.
    kind 'undo' → a workout deletion.
    """

    kind: str
    workout: ParsedWorkout | None = None
    unknowns: list[UnknownExercise] = Field(default_factory=list)
    duplicate: DuplicateMatch | None = None  # set when this workout looks already-logged
    workout_id: str | None = None
    label: str = ""
    # Backfill cards post to the same /confirm route as chat ones, but replaying
    # history must not publish years of workouts to Strava. Signed, so unforgeable;
    # defaults to not-chat so a token signed before this existed can't mirror.
    source: str = "backfill"


def _serializer(salt: str = _SALT) -> URLSafeTimedSerializer:
    if not config.settings.secret_key:
        raise RuntimeError("SECRET_KEY is not set — cannot sign confirm cards.")
    return URLSafeTimedSerializer(config.settings.secret_key, salt=salt)


def sign_card(card: PendingCard) -> str:
    return _serializer().dumps(card.model_dump(mode="json"))


def load_card(token: str) -> PendingCard | None:
    """Verify + deserialize a card token; None on tamper/expiry/garbage."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=CARD_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return PendingCard.model_validate(data)


def sign_job(message: str, history: str) -> str:
    return _serializer(_JOB_SALT).dumps({"message": message, "history": history})


def load_job(token: str) -> dict | None:
    """Verify a background-worker job blob; None on tamper/expiry. A valid blob is
    what authorizes the synchronous agent run, so this must never accept a forgery."""
    if not token:
        return None
    try:
        return _serializer(_JOB_SALT).loads(token, max_age=JOB_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def sign_activation(activation_id: str) -> str:
    return _serializer(_ACT_SALT).dumps(activation_id)


def load_activation(token: str) -> str | None:
    if not token:
        return None
    try:
        return _serializer(_ACT_SALT).loads(token, max_age=ACT_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
