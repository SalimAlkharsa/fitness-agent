"""DigitalOcean Functions self-invocation + activation polling.

The free-tier model takes 30-50s per parse call, but DO Functions caps a
synchronous web response at ~40s (a fixed platform limit — see
deploy/RUNBOOK.md). So `/chat` dispatches the slow agent work to a *background*
activation via the namespace API (`blocking=false`, which is not bound by that
wall) and polls the activation record for its result. Both calls authenticate
with the namespace access key (`DO_FN_AUTH`, an `id:secret` pair) over Basic auth.

All network failures degrade to None so the web layer can fall back to running
synchronously rather than 500 — the app stays usable even if self-invoke breaks.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from app.config import settings

log = logging.getLogger("fitness-agent")

_TIMEOUT = 15.0


def _auth() -> dict[str, str]:
    token = base64.b64encode((settings.do_fn_auth or "").encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _actions_url() -> str:
    return (
        f"{settings.do_fn_host}/api/v1/namespaces/{settings.do_fn_namespace}"
        f"/actions/fitness/app?blocking=false&result=false"
    )


def _activation_url(activation_id: str) -> str:
    return (
        f"{settings.do_fn_host}/api/v1/namespaces/{settings.do_fn_namespace}"
        f"/activations/{activation_id}"
    )


def async_invoke(params: dict) -> str | None:
    """Fire fitness/app as a non-blocking background activation. Returns the
    activation id to poll later, or None if self-invoke isn't configured/failed."""
    if not settings.async_dispatch:
        return None
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        _actions_url(),
        data=data,
        headers={**_auth(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        body = urllib.request.urlopen(req, timeout=_TIMEOUT).read().decode()
        return json.loads(body).get("activationId")
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        log.warning("async_invoke failed: %s", e)
        return None


def get_activation_result(activation_id: str) -> dict | None:
    """The finished activation's result dict ({statusCode, headers, body}), or
    None if it isn't ready yet (the record 404s until the invocation completes)."""
    req = urllib.request.Request(_activation_url(activation_id), headers=_auth())
    try:
        body = urllib.request.urlopen(req, timeout=_TIMEOUT).read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.warning("get_activation_result HTTP %s", e.code)
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("get_activation_result failed: %s", e)
        return None
    resp = json.loads(body).get("response") or {}
    return resp.get("result") or {}
