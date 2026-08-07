# fitness-agent — agent operating guide

You are building this project autonomously for Salim. He wants to spend as little
time as possible on it: do the work, make reasonable decisions yourself, and ask him
only when genuinely blocked or when a decision is irreversible or product-shaping.

## What this is

A single-user workout logger. Salim texts unstructured workout notes into a
passphrase-gated chat page; an agent parses them into structured rows in Google
Sheets after a confirm card. He can ask for trends and charts. Full spec: `DESIGN.md`
(read it before writing any code). Real example inputs with parsing quirks:
`evals/golden/README.md`.

**The repo is public.** `README.md` is the front door (why it exists + the idea),
`docs/SETUP.md` is the run-it-yourself guide, `DESIGN.md` is the design record.
Keep them true when behaviour changes, and never let a live URL, namespace, or
secret land in a tracked file.

## How to work

1. **The twelve build tickets (#1–#12) are done and closed.** New work arrives as
   a request from Salim or a new issue; there's no backlog to grind. Check
   `gh issue list` before assuming something is untracked.
2. **Definition of done is verified, not "should work."** If an issue has
   acceptance criteria, check them off in the body and close it with a comment
   saying how each was verified — by running the thing.
3. **Commit directly to `main`**, small commits per logical step. Single-user
   project; no PRs unless Salim asks. Run `make test && make lint` before every push.
4. **Tests over demos, but bare-essential only** — see "Testing policy" below.
   Every parsing rule and total-weight computation is still covered against the real
   examples in `evals/golden/README.md`, but through the fewest tests that do it.
5. **Gave the agent a new tool or skill? Prove a model triggers it** — add a row
   to `sandbox/scenarios.py` and run `make sandbox`. It runs the real agent loop
   against Claude Haiku (via the `claude` CLI — works with no keys) and a local
   JSON sheet, so you can verify end to end without OpenRouter quota, Salim's
   spreadsheet, or a deploy. `sandbox/README.md` has the workflow. Unit tests with
   a scripted FakeLLM can't answer "does a model actually reach for this tool?".
6. If you discover the design is wrong somewhere, don't silently diverge:
   update `DESIGN.md` in the same commit and note it in the issue — or ask Salim if
   it's product-shaping (see below).

## Testing policy (Salim's — do not relitigate)

Salim hates test bloat. The goal is **maximal coverage of the full essential flows
with the fewest tests**, where one test deliberately catches several points of
failure. More tests is not more safety; it's more to maintain. Apply these rules
when writing or reviewing tests:

1. **Flow-first — one test per essential flow, asserting at every hop.** A good test
   walks the real path (e.g. parse → compute → commit → read-back) and checks each
   link, so a failure both trips the test and shows *where*. That is "several failure
   points in one test," done right. **On overlap, the flow test wins:** if a granular
   unit test only re-checks a sub-step some flow already exercises, delete the unit.
2. **Table-drive pure functions.** A function with N branches (e.g. `compute_total`
   across the equipment types) is **one parametrized test with N rows**, never N test
   functions. A new case is a new row, not a new `def`.
3. **A test earns its place only if it covers a failure the flow tests don't.** If
   deleting it wouldn't widen what a real bug can slip through, delete it. Distinct
   failure *modes* of an essential flow (e.g. the confirm card's discard vs. expiry,
   the agent's retry / inbox / max-iteration paths) each stay — that's coverage, not
   bloat. Sub-step re-checks and near-duplicates across files do not.
4. **The golden-fixture net is sacred.** The `evals/golden` corpus and the tests that
   commit every fixture end-to-end are the real parsing safety net — never thin them.

**Don't chase suite wall-clock by cutting tests.** Runtime lives in the few tests
that hit real machinery (SVG chart rendering, first web-app boot), not in test
count. Deleting essential tests to shave seconds is a bad trade; the suite runs in
seconds and that's fine. If speed ever genuinely matters, parallelize
(`pytest-xdist`), don't delete.

## Decisions: make vs. ask

**Decide yourself** (Salim explicitly delegates these): library choices within the
agreed stack, file layout, prompt wording, UI styling, naming, test structure,
error-message copy, anything reversible.

**Ask Salim** (he wants questions when needed — batch them, don't trickle):
- Anything requiring his accounts or money: GCP service-account creation, OpenRouter
  API key, paid model tiers, domain/HTTPS, repo visibility.
- Choice of passphrase (never invent and commit a secret).
- Product-shaping changes: schema changes beyond the spec, dropping/adding a feature,
  anything that changes what he'll see in the chat UI in a way DESIGN.md doesn't cover.
- Ambiguity in his own data that the KB can't resolve (e.g., whether two exercise
  names are the same movement) — though at runtime the confirm-card flow handles this.

**Never**: commit secrets (`.env`, service-account JSON are gitignored), write to a
sheet without the confirm flow, add Node/JS tooling (vendored htmx.min.js is the one
exception), or add an agent framework — the hand-rolled loop is a deliberate choice.

## Hard constraints (agreed with Salim — do not relitigate)

- Maximal Python: framework-free router (`app/web.py`) + Jinja2 + htmx, Pydantic,
  gspread, pysvgchart, uv/ruff/pytest. No Node, no build step. (Revised 2026-07-24
  with Salim's blessing: FastAPI/uvicorn/pandas/matplotlib dropped for the
  DO Functions migration — 48 MB bundle limit, no ASGI on OpenWhisk.)
- Google Sheets is the only datastore; `raw_text` stored on every workout.
- Confirm-first writes; read tools run freely.
- Model: `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter until the eval
  says otherwise. The eval report drives the model decision — data, not vibes.
- Schema changes go through the migration framework (`app/migrate.py`) —
  never hand-edit tab structure.

## Environment

- Dev happens on a Linux dev box; the repo is the working directory. **Deployment
  target is DigitalOcean Functions free tier** (decided 2026-07-24 to cut cost):
  one web function `fitness/app`, see `project.yml` + `deploy/RUNBOOK.md`. The old
  systemd deployment is retired.
- Secrets in `.env` (see `.env.example`). If a needed secret is missing, that's an
  "ask Salim" — don't stub around it silently.
- The app must stay fully stateless per-request (signed cookies, signed confirm
  cards, client-held chat history) — serverless instances share nothing.

## Current status

- Issues #1–#12 done and closed (full core loop, eval, migrations, agent, web
  chat + auth, log flow, query/charts, backfill, droplet deploy — since retired).
  Eval verdict: nemotron free tier passed (94% → 97.7% with the parsing skill);
  model choice stands.
- **2026-07-24: migrated to DigitalOcean Functions** (Salim-approved refactor to
  cut cost): pysvgchart inline-SVG charts (pandas/matplotlib gone), HMAC-signed
  stateless confirm cards with a replay guard, client-held chat history,
  framework-free router + OpenWhisk adapter + stdlib dev server, stateless 1s
  failed-login delay (rate limiter gone), `project.yml`/`make deploy` scaffolding.
- Provided/decided by Salim: OpenRouter API key, GCP service account + shared
  sheet (gitignored `.env`/`service-account.json`); **PASSPHRASE set** to his
  chosen value in `.env`; **staying on the OpenRouter free tier** (no $10 deposit
  — so pace live-LLM testing against the daily cap; model choice unchanged).
- **Dates are code-decided, not model-decided (2026-07-24)**: the free-tier model
  ignores the injected `now` and answers from its training era (undated log →
  `2025-08-20` while the prompt said `2026-07-24`); the transcript proved
  `read_skill` ran, so it's disobedience, not a missing skill. `app/dates.py`
  resolves after the parse (no cue → today; relative cue → computed; stated date →
  keep the parse). Don't "fix" this by only strengthening the prompt.
  Assistant replies render Markdown via `app/mdrender.py` (escape first, convert
  second — model output is untrusted).
- **Async /chat (2026-07-24)**: DO Functions caps synchronous HTTP at ~40s but the
  free-tier model parses in 30-50s, so a sync `/chat` returned a 202 the browser
  couldn't render (the "void" bug). Fixed: `/chat` dispatches the agent to a
  background self-invocation (`app/dofn.py`, `blocking=false`) and polls
  `/chat/poll`; async is on only when deployed (`__OW_*` present) so dev/tests stay
  synchronous. Also: OpenRouter returns rate-limit/upstream errors as HTTP 200 with
  an error body — the client now raises `LLMError` and the agent shows "model busy,
  try again" instead of a blank turn. Verified end-to-end in prod (5-exercise parse
  → confirm card at ~54s). Do NOT relitigate back to synchronous `/chat`.
- **Deployed live 2026-07-24**: verified end-to-end in production (login → LLM
  parse → confirm card → row in the sheet → cleanup). doctl is auth'd via
  `DIGITALOCEAN_ACCESS_TOKEN` in the gitignored `.env`; `BASE_PATH` is set there
  too. `make url` prints the URL — **never commit it, in any file**: the repo is
  going public and the passphrase page shouldn't be advertised.
- **Public-repo docs pass (2026-07-24)**: `README.md` rewritten as the front door
  (motivation + idea, no setup detail), new `docs/SETUP.md` for self-hosting,
  `deploy/RUNBOOK.md` narrowed to day-2 ops + platform gotchas, internal ticket
  refs dropped from `DESIGN.md`. Source comments simplified in the same pass; the
  *why* comments (platform caps, date resolution, escape-before-markdown, base64
  content-type rule) are load-bearing — don't strip them next time.
- **Pre-public exposure audit (2026-07-24)**: `.deployed/versions.json` is now
  untracked and gitignored, and `/healthz` no longer echoes raw exception text
  (gspread quotes the sheet ID and SA email; unauthenticated endpoint).
  **STILL OPEN — blocks going public:** two leaks survive in *git history* and
  need a rewrite + force-push, which is Salim's call: `.deployed/versions.json`
  (5 blobs: apihost + namespace = the live URL) and the dev droplet's public IP in
  the deleted `deploy/service-control.md` (1 blob). Untracking does not remove
  either from history.
