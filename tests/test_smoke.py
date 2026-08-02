"""Scaffold smoke tests: the web layer imports, serves pages, and reports health.
These run with no secrets configured."""

import app.web as web
from app import SCHEMA_VERSION
from tests.webclient import MiniClient


def test_healthz(monkeypatch):
    # Hermetic: force "no store" so the probe never touches a real sheet.
    monkeypatch.setattr(web, "get_store", lambda: None)
    web._health_cache["at"] = None
    resp = MiniClient().get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["sheet"]["reachable"] is None  # no creds configured


def test_index_requires_auth():
    resp = MiniClient().get("/")
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/login"


def test_login_page_open():
    resp = MiniClient().get("/login")
    assert resp.status_code == 200
    assert "Fitness Agent" in resp.text


def test_static_served_with_traversal_guard():
    client = MiniClient()
    js = client.get("/static/vendor/htmx.min.js")
    assert js.status_code == 200 and js.content_type == "application/javascript"
    png = client.get("/static/icons/icon-192.png")
    assert png.status_code == 200 and isinstance(png.body, bytes)
    assert png.body[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/static/../config.py").status_code == 404
