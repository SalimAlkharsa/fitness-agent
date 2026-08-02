"""Tiny in-process test client for app.web.handle — keeps a cookie jar like a
browser would, no HTTP stack involved."""

from __future__ import annotations

from app import web


class MiniClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, data=None, headers=None) -> web.Response:
        req = web.Request(
            method=method,
            path=path,
            form=dict(data or {}),
            cookies=dict(self.cookies),
            headers={k.lower(): v for k, v in (headers or {}).items()},
        )
        resp = web.handle(req)
        set_cookie = resp.headers.get("Set-Cookie")
        if set_cookie:
            pair = set_cookie.split(";", 1)[0]
            name, _, value = pair.partition("=")
            if value and "Max-Age=0" not in set_cookie:
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)
        return resp

    def get(self, path: str, **kw) -> web.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, data=None, **kw) -> web.Response:
        return self.request("POST", path, data=data, **kw)
