# Fitness Agent — Design

Personal workout logger for exactly one user. He texts unstructured workout notes
into a passphrase-gated chat page; an agent parses them, shows a confirm card, and
appends structured rows to Google Sheets. He can also ask questions ("am I
progressing on squats?") and get computed answers and charts back.

This is the design record — what was decided and why, including the decisions that
were later revised. For what the project *is*, start with the
[README](README.md); to run your own copy, see [docs/SETUP.md](docs/SETUP.md).

## Locked decisions

| Decision | Choice |
|---|---|
| Interface | Passphrase-gated chat web page. PWA manifest so it installs to the phone home screen. Not real auth: shared passphrase → signed cookie, server-side check; failed logins pay a fixed 1s delay (stateless brute-force throttle). |
| Hosting | DigitalOcean Functions free tier (revised 2026-07-24, was: systemd on the droplet). One web function routing all paths (`app/web.py` + the `app/serverless.py` OpenWhisk adapter). Platform limits shaped the design: 48 MB bundle → no pandas/matplotlib; 1 MB responses → inline SVG charts; no shared disk/memory → every pending thing is signed and round-tripped through the client; **~40s synchronous-HTTP cap → `/chat` runs the slow agent in a background self-invocation and polls (see Async chat below).** |
| State | Google Sheets is the only datastore. `raw_text` stored on every workout = full re-parse is always possible. Server holds zero request state: auth is a signed cookie, pending confirm cards are HMAC-signed blobs in the form (1h TTL, replay stopped by the exact-dup guard), chat history is client-held and rides along with each message. |
| Stack | Maximal Python, zero Node: framework-free `handle(Request)->Response` router + Jinja2 + htmx (server-rendered UI, no build step), Pydantic models, gspread, pysvgchart (inline SVG charts), httpx, uv/ruff/pytest. |
| Model | `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter. The choice was gated on data, not vibes: an eval harness scored field accuracy against the golden set, with a ≥90% bar and a cheap paid fallback (Haiku-class) ready if it missed. It scored 94%, and 97.7% once the parsing skill was added — see [`evals/reports/DECISION.md`](evals/reports/DECISION.md). |
| Agent | Hand-rolled linear tool-dispatch loop (max 6 steps, `MAX_STEPS`), no framework. The model picks from a fixed menu of typed tools and never runs code. Native tool-calling first (proper `tool` roles); JSON-mode fallback by config. Pydantic validates every call; write-tool validation errors are fed back for up to 2 retries then the raw message goes to `inbox`, read-tool errors just feed back. Repeat-call + unknown-tool guards stop a stuck model from burning the free-tier quota. |
| Skills | Markdown files in `skills/`, loaded on demand via a `read_skill` tool. System prompt carries only a one-line index of each. |
| Write flow | Confirm-first. Write tools (`log_workout`, `edit_workout`, `undo`, `update_kb`) render an editable confirm card; nothing hits the sheet until ✅. `edit_workout` reuses the log card, prefilled from the target workout, and replaces it in place on confirm. Read tools run freely. |
| Exercise KB | `exercise_kb` sheet tab. Unknown exercise name → confirm card asks "same as X, or new?" and the answer is saved. Learning requires no redeploy. |

## Sheet schema (SCHEMA_VERSION = 1)

Five tabs, all continuous (never a new table per day). Sessions are grouped by
`workout_id`; each `sets` row belongs to a workout.

### Tab `workouts` — one row per session
Basketball etc. is a session with `type=basketball` and no sets rows.

| column | example |
|---|---|
| workout_id | W0031 |
| date | 2026-07-10 (workout date, not message date) |
| type | strength / basketball / … |
| start | 17:00 (optional) |
| end | 19:00 (optional) |
| notes | lower body |
| raw_text | full original message (audit trail) |
| logged_at | 2026-07-10T19:42 |

### Tab `sets` — one row per set-group
"3x12 then 3x15" of the same exercise = two rows, `grp` 1 and 2.

| column | example | notes |
|---|---|---|
| workout_id | W0031 | FK to workouts |
| exercise | Back Squat | canonical name from KB |
| exercise_raw | Squats | as typed |
| grp | 1 | set-group ordinal within workout+exercise |
| sets | 5 | |
| reps | 6 | float — "3.5 real pull ups" is reps=3.5 |
| per_side_lbs | 60 | blank for stack/bodyweight |
| total_lbs | 165 | per_side×2 + KB base_lbs; stack value verbatim; "bodyweight" |
| assist_lbs | 95 | assisted dips/pull-ups |
| notes | negatives | |

Equipment and base bar weight live in the KB (they are properties of the exercise,
not the set). `total_lbs` is computed at log time; per_side is kept so totals can be
recomputed if a KB base weight is later corrected.

### Tab `exercise_kb`
| canonical | aliases (`;`-separated) | equipment | base_lbs | notes |
|---|---|---|---|---|
| Back Squat | squats; squats both sides | barbell | 45 | |
| Smith Calf Raise | calf raises on smith machine; calf raises smith elevated | smith | 25 | elevated variant = same exercise |
| Hip Abduction (Out) | hip abductor outer | stack | 0 | total = stated number |

### Tab `inbox`
timestamp, raw_text, error. Failed parses land here; nothing is ever lost.

### Tab `_meta`
schema_version, workout_id counter.

## Migrations

Sheets has no ALTER TABLE, so:
- `migrations/NNN_name.py` scripts each expose `apply(store)`; runner CLI
  `python -m app.migrate` applies pending ones in order and bumps `_meta`.
- Runner snapshots the whole spreadsheet via the Drive API (copy named
  `YYYY-MM-DD pre-migration-NNN`) before touching anything.
- App startup asserts code `SCHEMA_VERSION` == sheet `_meta` version and refuses to
  run on mismatch — a deploy can never write new-shape rows into old-shape tabs.
- Data is tiny: full-tab rewrite migrations (read → transform in Python → clear →
  write) are the standard pattern.
- Nuclear option: replay every `raw_text` through the parser into fresh tabs.
  Caveat: `edit_workout` (below) keeps the workout's original `raw_text` and
  overwrites `logged_at`, so a replay would silently revert any hand-edit back
  to the original parse — acceptable since edits are rare, manual corrections.

## Async chat (serverless, revised 2026-07-24)

DO Functions returns HTTP 202 "Response not yet ready" for any synchronous web
call that runs longer than ~40s (a fixed platform limit). The free-tier model
takes 30-50s for a single parse call, so a synchronous `/chat` returned a 202 the
browser couldn't render — the reported "void". Fix: decouple the slow work from
the HTTP response.

- `POST /chat` (browser) signs the message + client-held history into a job blob
  and fires a **background self-invocation** of the same function via the
  namespace API (`blocking=false`, which is not bound by the 40s wall — see
  `app/dofn.py`). It returns immediately with a thinking placeholder (`_thinking.html`)
  that htmx-polls `POST /chat/poll`.
- The background activation carries the signed job (`_job`), so `handle()` skips
  the cookie gate — the signature *is* the authorization — and `chat()` runs the
  agent synchronously, rendering the turn HTML as its activation result.
- `POST /chat/poll` fetches the activation record (404 until it completes) and
  returns the finished turn, or the placeholder again to keep polling.
- Credentials: host + namespace come from the runtime-injected `__OW_API_HOST`/
  `__OW_NAMESPACE` (present only when deployed → async stays off locally, so dev
  and tests run synchronously); the namespace key rides in via `project.yml`
  (`DO_FN_AUTH`), because `__OW_API_KEY` is withheld from the action.
- The tokens (job, activation-id) are signed like confirm cards, so a browser can
  neither forge a "run synchronously" worker call nor poll an arbitrary activation.

Model-failure handling (same change): OpenRouter returns upstream failures — a
rate-limited free tier especially — as HTTP 200 with an `error` body and no
`choices`. The client (`app/llm.py`) now treats that as a retryable error and
raises `LLMError` rather than yielding empty content, and the agent surfaces a
clear "model busy — try again" message. An empty completion is likewise never
rendered as a blank turn.

## Agent architecture

One linear dispatch loop: the model picks from a small, fixed
menu of typed tools — it never authors or runs code. Each step it either answers in
plain text (done), calls a *read* tool (executed in-process, result fed back), or
calls a *write* tool (confirm-gated → the loop short-circuits to a pending card).

```
message → loop (max MAX_STEPS = 6 steps) {
  model sees: system prompt (identity + enriched `now` + KB + fixed tool menu +
              skill index) + conversation with proper roles + tool results
  each step → ONE of:
    read_skill(name)        → full text of a skills/<name>/SKILL.md file        [read]
    query_history(filters)  → set-group rows + session list as JSON (row-capped) [read]
    describe_data(since?)   → overview: counts, date range, top exercises, types [read]
    exercise_stats(ex,…)    → one exercise: series + PR + trend (volume/top_set/  [read]
                              pr/frequency)
    personal_records(ex?)   → PRs across all tracked exercises (or one)          [read]
    make_chart(spec)        → pysvgchart SVG rendered inline in chat            [read]
    log_workout(json)       → ParseResult → Pydantic validate → confirm card    [confirm]
                              per workout → on ✅ append workouts+sets (KB learns)
    undo(workout_id?)       → delete a workout + its sets                       [confirm]
    edit_workout(workout_id?) → same editable card as log_workout, prefilled    [confirm]
                              from the workout's current values; on ✅ replaces
                              it in place (same id, old rows deleted first)
    <plain text>            → final answer
} until a final answer / a confirm short-circuit / a guard stop
```

Roles are honest: a tool call is recorded as an assistant turn carrying `tool_calls`
(native mode) and its result as a `role:tool` message — no fake assistant/user
injection. In json mode (config fallback) results feed back as marked text turns.

Limits & guards (a weak free-tier model must not burn the daily request quota):
- **MAX_STEPS = 6** LLM calls per message; then a graceful "couldn't work that out".
- **MAX_VALIDATION_RETRIES = 2** on a *write* tool → then the raw message goes to the
  `inbox` tab. Read-tool validation errors feed back but never write to inbox.
- **Repeat-call guard**: the same read tool + args twice running → break gracefully.
- **Unknown-tool guard**: the real menu is fed back once, a second unknown call breaks.
- **MAX_HISTORY_MESSAGES = 24** caps context; orphan `tool` messages are trimmed so a
  history slice never sends a `tool` reply without its originating call.

Dates: the injected `now` is enriched to `Weekday YYYY-MM-DD HH:MM America/Chicago`
so "today"/"yesterday"/"Monday"/undated logs resolve in the user's home timezone,
not the server's. No date tool — it's a per-request constant, injected once.

**But the model does not get the final say on the date (revised 2026-07-24).** In
production an undated message parsed to `2025-08-20` while the prompt said
`2026-07-24`; the transcript confirmed `read_skill` ran and `now` was correct, so
the model was simply answering from its training era and inventing a rationale
("last Monday"). A wrong year silently corrupts every trend, and dates are trivial
to compute, so `app/dates.py` decides after the parse (live agent path only):

- no date cue anywhere in the message → **today**
- a relative cue we understand ("yesterday", "monday", "3 days ago") → **computed
  in code** (weekday = most recent past occurrence)
- an explicitly stated date ("aug 4", "2026-06-01") → the model's parse is kept,
  since that's the one case where it actually read the date off the text

The confirm card is still editable, so a wrong guess is one keystroke to fix.
Backfill keeps its own handling (an unrecoverable date stays flagged rather than
silently becoming today). Prompt/skill wording stays as the first line of defence
— this is the deterministic backstop behind it.

Assistant replies render as Markdown (`app/mdrender.py`): the model answers with
tables and bold, which previously showed as raw `**`/pipes. Model output is
untrusted, so the text is HTML-escaped *first* and converted *second* — injected
markup becomes visible text, never live HTML.

Pydantic models are the single source of truth: `.model_json_schema()` is embedded in
the prompt AND validates the output — one definition, both jobs.

### No arbitrary code — a closed read menu (prompt-injection containment)

The app is on the public internet, so the LLM (reachable by anyone with the
passphrase, and steerable by prompt injection in a logged message) is treated as
hostile. The containment is structural, not defensive: **it cannot author or run
code at all.** An earlier design gave it a `run_python` tool behind a sandbox
(`unshare --net` namespace, CPU/address-space rlimits, a 60 s kill); that whole
stack was deleted in favour of the fixed tool menu below. There is nothing left to
contain because there is no code path that executes a model-supplied string.

Analysis instead goes through a small, fixed menu of typed, deterministic read
tools (`query_history`, `describe_data`, `exercise_stats`, `personal_records`,
`make_chart`). The injection-safe boundary lives in `app/analysis.py`:
- **Typed params only**: every filter is a plain typed comparison (equality /
  `<=` on values) or a KB dict-lookup via `resolve_exercise`. No caller-supplied
  string is ever eval'd, formatted into a query, or used as a path. This is a
  **hard rule** for any future read handler — it is the entire reason removing
  `run_python` is a security win.
- **No store/creds exposure**: handlers read through `SheetsStore` in-process and
  return fixed-shape, row-capped JSON; the model sees data, never objects or keys.
- **Charts touch no filesystem**: `make_chart` returns an SVG string rendered
  inline; the model never influences a path.

Writes still only ever happen through the confirm-gated `log_workout` / `undo`
tools, on the user's ✅ — never from a read tool. Confirm-card tokens are signed
(`app/cards.py`), so a forged POST can't invent a write payload without
`SECRET_KEY`. OS-level isolation now comes free from the platform: each
invocation runs in DigitalOcean's sandbox with no persistent host to harden
(the old systemd unit is gone with the droplet deployment).

### Deduplication

Live logging must not write the same session twice by accident. `app/dedup.py` is
the single source of truth (backfill imports its hashers, so the two paths can't
drift) with two read-time signals:
- **exact** — sha1 of normalized `raw_text`: a literal re-send / double-tap.
- **semantic** — sha1 of `date|type|sorted(exercise:setsxreps)`; weight is excluded
  so a re-log that only corrected a weight still matches.

`find_duplicate(pw, store, kb)` runs in `_build_cards` as the confirm card is built;
a hit stashes on the `PendingCard` and renders an **overridable warning banner**
above ✅/❌ (exact wins over semantic). Nothing is ever silently blocked or dropped —
confirm-first means ✅ always commits. No schema change: both signals derive from
columns that already exist (`raw_text`; `exercise`/`sets`/`reps` joined to
`date`/`type`), the same technique backfill already uses.

## Strava mirror (added 2026-08-06)

Optional, off unless `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` /
`STRAVA_REFRESH_TOKEN` are all set. Strava's MCP connector is read-only (and
subscriber-gated), so `app/strava.py` uses the v3 REST API directly.

- **Out**: a confirmed workout is posted as a manual activity (`POST /activities`).
  `workout.type` maps to a Strava `sport_type` (`strength → WeightTraining`,
  `basketball → Basketball`, …, default `Workout`); the commit summary becomes the
  description. Duration comes from `start`/`end` when the card has them, else a
  flat hour.
- **Only chat logs push.** Backfill cards post to the same `/confirm` route, so
  the card carries a signed `source` and only `chat` mirrors — otherwise pasting a
  two-year dump would publish all of it. The CLI paths don't push at all.
- **In**: `python -m app.strava backfill [--commit]` pulls `GET /athlete/activities`
  and maps each run (`Run`/`TrailRun`/`VirtualRun`) to a `type=run` workout with
  distance/time/pace in `notes` —
  no LLM, no schema change (a run is a session with no sets, like basketball).
  `raw_text` is `strava:<activity_id> …`, so the existing raw-text hash dedup makes
  re-runs idempotent. Manual activities are skipped: the runs *we* push are manual,
  and re-importing one would duplicate the session it came from.
- **The sheet is the source of truth**: the push is best-effort and never raises —
  a Strava outage appends `Strava ✗` to the confirm message and nothing else
  changes.
- **Auth** is a refresh-token swap per call, since serverless instances cache
  nothing. Strava may rotate the refresh token and invalidates the old one
  immediately, so the live token is kept in `_meta` (a key/value tab — no schema
  change) with `.env` as the seed. Without that, one rotation would silently kill
  the mirror until `auth` was re-run.
- **Strava has no delete endpoint** and does not dedup manual activities, so a
  double post would be unremovable via API. Nothing extra guards this: the confirm
  path's existing replay guard returns *before* commit, so one commit = one push.
  `undo` removes the sheet rows only — the Strava activity must be deleted in the
  app.

## Backfill

- `python -m app.backfill dump.txt [--commit]` splits a historical dump on
  2+-blank-line gaps, runs each message through the same parse pipeline as live
  logging, and dedups on a hash of normalized `raw_text` (skips anything already
  committed or repeated within the run).
- The web "Backfill" view renders one editable confirm card per parsed workout
  with per-card ✅/❌ plus a "confirm all" button — same `_log_card` UI as chat.
- Dates: stated dates are used; a message with no recoverable date gets
  `date=unknown` and is flagged in the card for manual entry.
- KB growth: the interactive card asks "same as X, or new?" per unknown exercise.
  The non-interactive paths (CLI `--commit`, web "confirm all") auto-register each
  unknown by name with a conservative equipment guess (`cable` if weighted so the
  stated total passes through verbatim, else `bodyweight`) — refine later by
  editing the KB. The CLI parses+commits one message at a time, re-reading the KB
  between each, so aliases learned earlier resolve later messages at parse time.

## How this was built

The work was cut into twelve dependency-ordered tickets (filed as GitHub issues
#1–#12) and built by Claude Code against [`CLAUDE.md`](CLAUDE.md), which carries
the operating rules — testing policy, what to decide alone vs. ask about, and the
constraints above restated as things not to relitigate. Both are kept in the repo
because they're a real part of how it got made.

Two decisions here were revised *after* production contact rather than at design
time — synchronous chat (the ~40s platform cap) and model-chosen dates (a
confidently wrong year). Both are documented above as revisions instead of being
quietly rewritten, since the failure is the interesting part.
