# Deploy runbook — DigitalOcean Functions

Day-2 operations and the platform constraints baked into the deploy config. For
first-time setup (Google service account, `.env`, first deploy), start with
[`docs/SETUP.md`](../docs/SETUP.md).

The app deploys as ONE web function (`fitness/app`) that routes every path
internally (see `app/web.py`). The platform runs it on demand, and the free tier
(90,000 GiB-seconds/month) covers single-user use with huge headroom.

## Deploy

```sh
make deploy    # stages packages/ and runs doctl serverless deploy --remote-build
make url       # prints the function URL
make fn-logs   # tail activation logs
```

Redeploy after any code change. If the change bumps `SCHEMA_VERSION`, run
`make migrate` **first** — the web layer refuses requests when code and sheet
schema versions disagree.

A successful deploy appends an entry to [`RELEASES.md`](RELEASES.md) — when, the
commit, and every commit subject since the last deploy — so "is that fix live?"
is a question the repo answers. Commit it afterwards; nothing auto-commits.

Sessions, confirm cards, and chat history are all stateless (signed cookies +
signed card blobs + client-held history), so instances can come and go freely.
Cold starts just add a couple of seconds.

## Why `/chat` is asynchronous

DO Functions cuts any synchronous web response at ~40s (a fixed platform limit),
but the free-tier model takes 30–50s to parse a workout. A synchronous `/chat`
therefore returned an HTTP 202 the browser couldn't render — a message sent into
a void.

So `/chat` dispatches the agent to a background self-invocation and the browser
polls `/chat/poll` for the result (see the "Async chat" section of
[`DESIGN.md`](../DESIGN.md) and `app/dofn.py`). That self-invocation needs the
namespace key as `DO_FN_AUTH` in `.env`; the API host and namespace come from the
runtime automatically. **If you rotate the namespace key or recreate the
namespace, refresh `DO_FN_AUTH` from `~/.config/doctl/.../credentials.json` and
redeploy.**

Async is enabled only when deployed (it keys off runtime-injected `__OW_*` vars),
so local dev and the test suite stay synchronous.

## Edge caching

Cloudflare fronts the function and `_static` sets a long `Cache-Control`. Two
consequences are baked into the code — don't undo either:

1. **Text assets are served as plain strings, never base64.** DO only
   base64-*decodes* binary content-types, so a base64 body under
   `application/javascript` reaches the browser as gibberish and silently breaks
   htmx (the page loads, but nothing works).
2. **Static URLs carry a `?v=<STATIC_VERSION>` cache-buster.** Bump
   `STATIC_VERSION` in `app/web.py` whenever anything in `static/` changes, or
   browsers keep serving the cached old copy.

## Deploy-time gotchas already handled

Each of these cost a broken deploy to find. The scaffolding accounts for them:

- **`runtime: python:3.11`, never `python:default`** (= 3.9, which can't evaluate
  the `str | None` annotations pydantic resolves at runtime). Keep the code
  3.11-compatible: no PEP-695 generics — ruff's `target-version` enforces this.
- **`build.sh` is required.** Without it the remote build silently skips
  `requirements.txt` and the function dies with `No module named 'jinja2'`.
- **`skills/` must ship in the bundle** (`make stage` copies it). Without the
  parsing skill the free-tier model fails to produce confirm cards.
- **`project.yml` env values are single-quoted.** Substitution happens before YAML
  parsing and the inline `GOOGLE_SA_JSON` is full of double quotes. Every
  referenced variable must be non-empty — the Makefile supplies defaults.

## Limits that shaped the design

- **48 MB** max built function → no pandas/matplotlib (pysvgchart SVG instead).
- **1 MB** response cap → charts are inline SVG, never large images.
- **~40s** synchronous HTTP cap → the async chat path above.
- **No shared disk or memory** between invocations → everything is signed and
  round-tripped through the client.
