"""Local dev server (stdlib http.server) wrapping app.web.handle.

Mirrors the serverless adapter so `make dev` exercises the same request path the
deployed function runs. Not for production — deployment is DigitalOcean
Functions (see app.serverless).
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app.serverless import parse_cookie_header
from app.web import Request, handle


class _Handler(BaseHTTPRequestHandler):
    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        request = Request(
            method=self.command,
            path=urlsplit(self.path).path,
            form=form,
            cookies=parse_cookie_header(self.headers.get("Cookie")),
            headers={k.lower(): v for k, v in self.headers.items()},
        )
        resp = handle(request)
        payload = resp.body if isinstance(resp.body, bytes) else resp.body.encode()
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


def main() -> None:
    port = int(os.getenv("PORT") or 8000)
    print(f"fitness-agent dev server on http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
