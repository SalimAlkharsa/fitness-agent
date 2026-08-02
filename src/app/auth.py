"""Passphrase gate + signed-cookie auth (#8).

Not real authentication — a shared passphrase discourages casual snoops on a
single-user app. A correct passphrase mints a signed, timed cookie (itsdangerous)
good for 90 days. Brute-force throttling is a stateless fixed delay on failure
(see app.web.LOGIN_FAIL_DELAY) — serverless instances share no attempt counter.
"""

from __future__ import annotations

import hmac

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

COOKIE_NAME = "fa_session"
COOKIE_MAX_AGE = 90 * 24 * 3600  # 90 days
_SALT = "fitness-agent-auth"


def _serializer() -> URLSafeTimedSerializer:
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY is not set — cannot sign auth cookies.")
    return URLSafeTimedSerializer(settings.secret_key, salt=_SALT)


def make_cookie() -> str:
    return _serializer().dumps({"ok": True})


def cookie_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def passphrase_ok(candidate: str) -> bool:
    """Constant-time compare against the configured passphrase."""
    expected = settings.passphrase or ""
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())
