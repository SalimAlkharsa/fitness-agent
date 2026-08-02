"""DigitalOcean Functions (OpenWhisk raw web action) adapter for app.web.handle.

`main(event)` is the deployed entry point: it turns the OpenWhisk event
(__ow_method / __ow_path / __ow_headers / __ow_body) into an app.web.Request,
runs the router, and shapes the Response into the {statusCode, headers, body}
dict the platform expects.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs

from app.web import Request, handle


def parse_cookie_header(value: str | None) -> dict:
    cookies: dict[str, str] = {}
    for part in (value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _qs(raw: str) -> dict:
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


# A pure-base64 blob. An urlencoded form body always contains a mid-string "="
# (key=value), so it can never match this by accident.
_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def parse_form_body(raw: str) -> dict:
    """Parse an urlencoded body that OpenWhisk may or may not have base64-encoded."""
    if not raw:
        return {}
    if _B64_RE.fullmatch(raw):
        try:
            return _qs(base64.b64decode(raw, validate=True).decode())
        except (ValueError, UnicodeDecodeError):
            pass
    return _qs(raw)


def main(event: dict) -> dict:
    headers = {str(k).lower(): str(v) for k, v in (event.get("__ow_headers") or {}).items()}
    # `_job` is a top-level param set by dofn.async_invoke, never sent over HTTP,
    # so it only appears on a genuine background self-invocation. It is still
    # signature-verified in chat() — this just transports it.
    job = event.get("_job")
    request = Request(
        method=(event.get("__ow_method") or "get").upper(),
        path=event.get("__ow_path") or "/",
        form=parse_form_body(event.get("__ow_body") or ""),
        cookies=parse_cookie_header(headers.get("cookie")),
        headers=headers,
        worker_job=job if isinstance(job, str) else None,
    )
    resp = handle(request)
    out = {
        "statusCode": resp.status,
        "headers": {"Content-Type": resp.content_type, **resp.headers},
    }
    # DO web actions only base64-DECODE a body when the content-type is binary; for
    # text types (JS, SVG, JSON, HTML) a base64 body is served verbatim and reaches
    # the browser as gibberish. So base64 only genuinely-binary bytes (PNG icons)
    # and decode text bytes to a plain string.
    if isinstance(resp.body, bytes):
        if _is_text_type(resp.content_type):
            out["body"] = resp.body.decode("utf-8")
        else:
            out["body"] = base64.b64encode(resp.body).decode()
    else:
        out["body"] = resp.body
    return out


def _is_text_type(content_type: str) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    return (
        ct.startswith("text/")
        or ct.endswith(("+json", "+xml"))
        or ct in {"application/javascript", "application/json", "image/svg+xml"}
    )
