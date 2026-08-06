# fitness-agent

I text my workouts the way I'd text a friend, and an agent turns them into
structured rows in a spreadsheet I own.

## Why I built it

I already had a workout logging habit — it just lived in my notes app as messy
lines like `Squats 4x7 60lbs on each side` and `3.5 real pull ups`. That habit
survived because it costs nothing: I type the thing and move on to the next set.

Every fitness app I tried wanted the opposite. Tap the exercise, tap the set, tap
the weight, pick from a dropdown that doesn't have "Australian pull ups" in it.
That's fine at a desk and miserable between sets, so I'd use it for a couple of
weeks and quit. Meanwhile my notes kept accumulating real data I couldn't chart,
couldn't query, and couldn't answer a simple question with — *am I actually
progressing on squats, or does it just feel that way?*

So the tradeoff I wanted was: **keep the input as lazy as a text message, and move
all the structure to the other side.** An LLM is good at exactly that translation,
and it's the only part of this that needed one. Everything else — the math, the
dates, the storage — is ordinary code, because I'd rather a model never be the
thing standing between me and correct numbers.

Two more things I cared about:

- **I own the data.** It goes into a Google Sheet, not someone's product. Every
  row keeps the original message I typed, so I can always re-parse from scratch if
  I change how parsing works.
- **It should cost about nothing.** It runs on DigitalOcean Functions' free tier
  against a free LLM, so there's no server sitting idle and no subscription. If I
  stop using it for three months, it costs me the same as using it.

## The idea

A passphrase-gated chat page. I paste a workout in, an agent parses it, and it
shows me a **confirm card** — an editable table of exactly what's about to be
written. Nothing reaches the spreadsheet until I hit ✅. I can also just ask it
questions, and it answers with computed numbers and charts.

```
me ▸  Squats 4x7 60lbs on each side
      3.5 real pull ups
      Row Machine 15 reps of 35lbs then 15 of 65lbs

      ┌─ Log this workout? ──────────────────────────┐
      │ Back Squat     4 × 7   60/side    165 lbs    │
      │ Pull Up        1 × 3.5            bodyweight │
      │ Row Machine    1 × 15             35 lbs     │
      │ Row Machine    1 × 15             65 lbs     │
      │                                    ✅   ❌    │
      └──────────────────────────────────────────────┘

me ▸  am I progressing on squats?
      Top set is up 20 lbs over 8 weeks — 145 → 165. [chart]
```

A few decisions that turned out to matter:

- **Confirm-first writes.** Read tools run freely; anything that writes to the
  sheet renders a card and waits. The model gets to *propose*, never to *commit*.
- **The model can't run code.** It picks from a small menu of typed tools
  (`query_history`, `exercise_stats`, `make_chart`, …) that are ordinary Python
  functions with typed parameters. There's no code path that executes a
  model-supplied string, so prompt injection has nothing to reach for.
- **Dates are computed, not inferred.** The model kept confidently dating undated
  logs to its training era. A wrong year silently corrupts every trend, and dates
  are trivial to compute, so code decides them after the parse.
- **Weight math is code.** `60 lbs on each side` on a barbell is 165 lbs, and
  that's a lookup plus arithmetic, not something to hope a model gets right.

There's an optional **Strava mirror**: confirmed workouts get posted there as
manual activities, and my Strava runs get pulled back into the sheet. The sheet
stays the source of truth — if Strava is down the log still lands.

The parsing quirks it has to handle are real ones from my own logs — fractional
reps, per-side vs. stack weights, assisted dips, `then` meaning a second
set-group. They're collected in
[`evals/golden/README.md`](evals/golden/README.md) and pinned as fixtures, so a
prompt change that breaks them fails the test suite.

## Stack

Maximal Python, zero Node and no build step: a framework-free
`handle(Request) -> Response` router + Jinja2 + htmx, Pydantic, gspread,
pysvgchart for inline SVG charts, managed with [uv](https://docs.astral.sh/uv/).
No agent framework either — the tool-dispatch loop is one readable file, and I'd
rather maintain that than a framework's abstractions.

Deploys to **DigitalOcean Functions** (free tier) as a single web function.
Google Sheets is the only datastore; the app itself holds no state at all, which
is what makes serverless work — auth is a signed cookie, pending confirm cards
are signed blobs, and chat history rides along with each request.

## Docs

- **[Set it up for yourself](docs/SETUP.md)** — full walkthrough: Google service
  account, config, running locally, deploying.
- **[DESIGN.md](DESIGN.md)** — architecture and the reasoning behind each locked
  decision.
- **[deploy/RUNBOOK.md](deploy/RUNBOOK.md)** — day-2 operations and the
  platform gotchas worth knowing before you change deploy config.
- **[evals/golden/README.md](evals/golden/README.md)** — the real messages that
  define correct parsing.

## Security model

It's on the public internet, so: a single high-entropy passphrase (constant-time
compare, fixed 1s delay per failed attempt) gates the page and issues a signed
cookie. All writes are confirm-gated behind HMAC-signed card tokens, so a forged
request can't invent a write payload without the signing key.

This is deliberately *not* real auth — it's one user and one spreadsheet. Worst
case for a leaked passphrase is junk rows in a sheet (undoable, and the original
text is preserved) and a burned free-tier LLM quota. No other blast radius.

## Status

Deployed and working end to end. Built with [Claude Code](https://claude.com/claude-code)
— [`CLAUDE.md`](CLAUDE.md) is the operating guide it works from, kept in the repo
because it's a real part of how this was built.
