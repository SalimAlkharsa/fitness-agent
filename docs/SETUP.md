# Set it up for yourself

This walks through standing up your own copy end to end: a Google Sheet to write
into, an LLM key to parse with, running it locally, and deploying it.

It's built for a single user, so "setting it up" means *your* sheet, *your* keys,
*your* passphrase. There's no multi-user mode and no account system — see
[Making it yours](#making-it-yours) if your logs look different from mine.

**Roughly 20 minutes**, most of it clicking through Google Cloud once.

---

## 1. Prerequisites

- **Python 3.11+** (3.11 exactly is what deploys — see the runtime note in
  [`deploy/RUNBOOK.md`](../deploy/RUNBOOK.md))
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for dependency
  management
- A **Google account** (for the sheet) and an
  **[OpenRouter](https://openrouter.ai/)** account (for the model — the default
  is a free one)
- Only if you want to deploy: a **DigitalOcean account** and
  [`doctl`](https://docs.digitalocean.com/reference/doctl/how-to/install/)

```bash
git clone https://github.com/SalimAlkharsa/fitness-agent
cd fitness-agent
uv sync --extra dev     # venv + deps, including dev tooling
cp .env.example .env    # you'll fill this in as you go
```

`.env` is gitignored. Don't commit it, and don't commit the service-account key
you're about to download.

---

## 2. Google service account

The app talks to one spreadsheet through a **service account** — a robot Google
identity with its own email address. You'll create it, download its key, and
share your sheet with it.

1. **Create or pick a GCP project** — [console.cloud.google.com](https://console.cloud.google.com)
   → project dropdown → *New Project* (call it e.g. `fitness-agent`).
2. **Enable two APIs** in that project — APIs & Services → *Enable APIs* → enable
   both **Google Sheets API** and **Google Drive API**. Drive is required because
   migrations snapshot the whole spreadsheet before touching it.
3. **Create the service account** — IAM & Admin → Service Accounts → *Create*.
   Name it anything (`fitness-agent-sa`). **Skip the roles step** — it needs no
   project-level permissions; access is granted per-sheet in step 5.
4. **Download a JSON key** — open the service account → *Keys* → *Add Key* →
   *Create new key* → **JSON**. A file downloads. Put it somewhere outside the
   repo (or at the repo root — `service-account*.json` is gitignored) and set:
   ```ini
   GOOGLE_SA_JSON=/path/to/your-key.json
   ```
5. **Create the spreadsheet and share it** — make a blank Google Sheet. Copy its
   ID out of the URL:
   ```
   docs.google.com/spreadsheets/d/<THIS-PART-IS-THE-ID>/edit
   ```
   Set it as `SHEET_ID` in `.env`. Then hit **Share** and share the sheet with the
   service account's email (`...@<project>.iam.gserviceaccount.com`, shown on its
   page in the console) as an **Editor**.

> Step 5 is the one people miss. The service account can only see sheets that have
> been explicitly shared with it — a key alone gets you a `403`.

---

## 3. Fill in the rest of `.env`

```ini
OPENROUTER_API_KEY=sk-or-...   # https://openrouter.ai/keys
SHEET_ID=1a2b3c...             # from step 5 above
GOOGLE_SA_JSON=./service-account.json
PASSPHRASE=                    # what gates the chat page — make it long
SECRET_KEY=                    # signs cookies and confirm cards
```

Generate a real `SECRET_KEY` (and a decent passphrase) rather than inventing one
by hand:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The passphrase is the only thing standing between the internet and your
spreadsheet, so treat it like a password, not like a word. Everything else in
`.env.example` has a sensible default — including `MODEL`, which points at a free
OpenRouter model.

---

## 4. Create the tabs

```bash
make bootstrap
```

This creates the five tabs (`workouts`, `sets`, `exercise_kb`, `inbox`, `_meta`)
and seeds a starter exercise knowledge base. It's idempotent and never touches
existing data, so it's safe to re-run.

The **`exercise_kb`** tab is worth opening once. It maps how you write an exercise
to a canonical name, and records the equipment type and bar weight — which is how
`60 lbs on each side` becomes `165 lbs` for a barbell but stays `145 lbs` for a
stack machine. The agent adds to it as it learns new movements, but the seed rows
reflect my gym; adjust the `base_lbs` if your bars differ.

---

## 5. Run it

```bash
make test      # should pass before you trust anything
make dev       # http://localhost:8000
```

Open it, enter your passphrase, and paste a workout. The first real parse takes
30–65s on the free model — that's the model, not the app.

**Developing without burning LLM quota:**

```bash
make dev-dry   # canned agent responses, no OpenRouter calls
```

Dry-run mode serves placeholder confirm cards so you can work on the UI and flows
for free. Never set `DRY_RUN` in a deployed environment.

Other useful targets:

| command | what it does |
|---|---|
| `make test` | pytest suite |
| `make lint` | ruff check + format check |
| `make sandbox` | run the agent end to end against a mock model + mock sheet ([sandbox/](../sandbox/README.md)) |
| `make eval MODEL=<id>` | score a model against the golden parse fixtures |

---

## 6. Deploy (optional)

It runs fine locally. To put it on the internet — which is the point, since you
want it on your phone at the gym — it deploys as **one DigitalOcean Function** on
the free tier (75,000 GiB-seconds/month, which is enormous for single-user use).

```bash
doctl auth init                       # paste a read+write API token
doctl serverless install              # one-time plugin download
doctl serverless namespaces create --label fitness --region nyc1
doctl serverless connect
```

Two `.env` values change for deploys:

- **`GOOGLE_SA_JSON`** — the function has no filesystem to read your key from, so
  set it to the key's **full JSON content on one line**, single-quoted:
  ```ini
  GOOGLE_SA_JSON='{"type":"service_account","project_id":"...",...}'
  ```
- **`BASE_PATH`** — the URL prefix the function serves under. Leave it empty for
  the first deploy, then read it off `make url` (everything after the host, e.g.
  `/api/v1/web/fn-xxxx/fitness/app`), set it, and deploy again so links and
  redirects resolve.

Then:

```bash
make deploy    # stages packages/ and deploys via doctl
make url       # prints your function's URL
```

Verify: `<url>/healthz` returns `{"ok": true, ...}`, then open `<url>/` and log a
workout end to end.

One more `.env` value is needed for the deployed app to work properly —
**`DO_FN_AUTH`**, your namespace key, which lets the function invoke itself in the
background. Without it, parses that take longer than ~35s get cut off by the
platform. Grab it from `~/.config/doctl/sandbox/creds/*/credentials.json` after
connecting, and read the async-chat section of
[`deploy/RUNBOOK.md`](../deploy/RUNBOOK.md) for why this exists.

**Add it to your phone:** open the URL in mobile Safari/Chrome and *Add to Home
Screen*. It ships a PWA manifest, so it opens like an app.

---

## 7. Backfilling your history

If you already have a pile of old logs, put them in a text file separated by blank
lines and run them through the same parser:

```bash
uv run python -m app.backfill dump.txt             # preview, writes nothing
uv run python -m app.backfill dump.txt --commit    # parse and commit each
```

Re-running is safe — anything already logged is skipped via a hash of the
normalized original text. There's also a **Backfill** view in the web app that
gives you one editable card per workout plus a "confirm all" button, which is
nicer for a first pass.

Note that backfill will eat LLM quota fast: one request per message. Check your
OpenRouter rate limit before pointing it at two years of history.

---

## Operating it

| task | how |
|---|---|
| **Health check** | `GET <url>/healthz` (no auth) — returns `503` if the sheet is unreachable |
| **Logs** | `make fn-logs` tails activation logs |
| **Redeploy** | `make deploy` after any code change |
| **Rotate the passphrase** | edit `PASSPHRASE` in `.env`, then `make deploy`. Also regenerate `SECRET_KEY` to invalidate existing login cookies |
| **Schema changes** | never hand-edit tabs — see [`migrations/README.md`](../migrations/README.md), and run `make migrate` **before** `make deploy` |
| **Restore** | every migration snapshots the spreadsheet to the service account's Drive as `YYYY-MM-DD pre-migration-NNN`; copy it back over the live sheet |

Day-2 details and the platform constraints baked into the deploy config are in
[`deploy/RUNBOOK.md`](../deploy/RUNBOOK.md).

---

## Making it yours

The parsing is tuned to how *I* write workouts. If yours look different, these are
the places to change, in rough order of impact:

1. **`skills/parsing-salims-logs/SKILL.md`** — the parsing rules the agent reads
   before logging. This is the highest-leverage file; it's plain Markdown, and in
   my eval it took parse accuracy from 94% to 97.7%. Rewrite it for your own
   phrasing quirks.
2. **`exercise_kb`** (in your sheet) — your exercise names, equipment types, and
   bar weights.
3. **`evals/golden/`** — replace my fixtures with your own real messages and
   expected output, then use `make eval MODEL=<id>` to check whether a model or a
   prompt change actually helps. Guessing at prompt changes without this is how
   you go in circles.
4. **`src/app/models.py`** — the schema itself, if you want to track something I
   don't (RPE, distance, time). Schema changes go through
   [migrations](../migrations/README.md).

## Troubleshooting

| symptom | likely cause |
|---|---|
| `403` from Google | the sheet isn't shared with the service account's email (step 5) |
| `SpreadsheetNotFound` | wrong `SHEET_ID`, or the Sheets/Drive APIs aren't enabled |
| App refuses to start, complains about schema version | code `SCHEMA_VERSION` ≠ the sheet's `_meta` — run `make migrate` |
| Chat hangs, then "model busy — try again" | OpenRouter rate-limited you; free tiers are tight |
| Deployed page loads unstyled or buttons do nothing | stale edge-cached assets — bump `STATIC_VERSION` in `src/app/web.py` and redeploy |
| `No module named 'jinja2'` in the deployed function | `deploy/fn_build.sh` didn't ship or isn't executable |
