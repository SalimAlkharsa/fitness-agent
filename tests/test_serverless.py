"""DO Functions adapter flow (app.serverless): OpenWhisk event → Request →
Response dict, covering form decode (plain + base64), cookies both ways, and
binary bodies — the hops the web tests can't see."""

from __future__ import annotations

import base64
import os

os.environ["PASSPHRASE"] = "test-pass"
os.environ["SECRET_KEY"] = "test-secret-key-for-cookies-000000"
os.environ["BASE_PATH"] = ""  # a deploy-configured .env must not skew URL asserts

import app.config as config  # noqa: E402

config.settings = config.Settings.from_env()
import app.auth as auth  # noqa: E402
import app.web as web  # noqa: E402

auth.settings = config.settings
web.settings = config.settings

from app.serverless import main, parse_form_body  # noqa: E402


def _event(method="GET", path="/", body="", headers=None):
    return {
        "__ow_method": method,
        "__ow_path": path,
        "__ow_body": body,
        "__ow_headers": headers or {},
    }


def test_event_to_response_flow():
    # Login with a base64-encoded form body (as OpenWhisk delivers raw bodies)
    # → 303 + signed Set-Cookie.
    body = base64.b64encode(b"passphrase=test-pass").decode()
    out = main(_event("post", "/login", body))
    assert out["statusCode"] == 303
    cookie = out["headers"]["Set-Cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    token = cookie.split(";")[0].split("=", 1)[1]

    # The cookie header comes back in and authenticates the chat page.
    page = main(_event("get", "/", headers={"cookie": f"{auth.COOKIE_NAME}={token}"}))
    assert page["statusCode"] == 200 and "Fitness Agent" in page["body"]

    # Without it: redirect to login.
    assert main(_event("get", "/"))["statusCode"] == 303

    # Binary static asset → base64 body (DO decodes it because image/png is binary).
    png = main(_event("get", "/static/icons/icon-192.png"))
    assert png["headers"]["Content-Type"] == "image/png"
    assert base64.b64decode(png["body"])[:8] == b"\x89PNG\r\n\x1a\n"

    # Text static asset (JS) → plain string, NOT base64 — DO serves a base64 body
    # verbatim for text content-types, which silently breaks the served script.
    js = main(_event("get", "/static/vendor/htmx.min.js"))
    assert js["headers"]["Content-Type"].startswith("application/javascript")
    assert js["body"].startswith("var htmx")  # real JS, not base64 gibberish

    # Plain (non-base64) form bodies parse too, and padded base64 still decodes
    # (a padded blob once parsed as plain with a bogus "=" value — regression).
    assert parse_form_body("message=hi&history=") == {"message": "hi", "history": ""}
    padded = base64.b64encode(b"passphrase=smoke").decode()
    assert padded.endswith("==") and parse_form_body(padded) == {"passphrase": "smoke"}
