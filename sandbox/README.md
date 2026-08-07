# sandbox — offline skill tester

Run the real agent against a fake model and a fake sheet, so you can check that a
new ability actually works before it goes anywhere near Salim's data, his
OpenRouter quota, or a deploy. No API keys, no network calls to Google or
OpenRouter.

It exists for one question, the one unit tests can't answer:

> I gave the agent a new tool/skill. Does a model actually *reach for it* when a
> human phrases the request naturally — and does the write land?

```bash
make sandbox                                    # run every scenario
uv run python -m sandbox "squats 3x5 60 each"   # one ad-hoc message, verbose
uv run python -m sandbox --reset                # start from a fresh seeded sheet
uv run pytest sandbox                           # the same scenarios, as tests
```

## Adding a check for your new ability

Add one row to `scenarios.py` and run `make sandbox`. That's the whole workflow.

```python
{
    "say": "how much did I bench in July?",   # what a human would type
    "tool": "exercise_stats",                 # the tool it should reach for
    "confirm": True,                          # approve the card, so the write lands
    "edits": {"date": "2026-07-19"},          # card fields the user fixes first
    "adds": {"workouts": 1, "sets": 2},       # rows the turn should add
},
```

Only `say` is required. Scenarios run in order against one seeded sheet, so a
later one can act on what an earlier one logged — but each gets a fresh
conversation, so what's tested is the message alone reaching the tool, not the
model coasting on chat context. Keep the table to essential flows — a write, a
read, a correction, plus yours — not variants of what's there.

For anything the table can't express, drive it directly:

```python
sb = Sandbox(reset=True)
result = sb.say("squats 3x5 at 60 each side")
assert result.pending.tool == "log_workout"       # confirm-gated, nothing written yet
sb.confirm(result, date="2026-07-19")             # ✅, with the user fixing a field
assert sb.rows("workouts")[-1]["date"] == "2026-07-19"
assert "query_history" in sb.tools_used()         # read tools show up here too
```

## What's real and what isn't

Real: the agent loop, the tool menu, the system prompt, the KB, skill loading,
date resolution, validation/retry/inbox guards, and the commit path
(`logflow.commit_workout`). If your change touches any of those, this exercises it.

Mocked, and picked from the environment so the same scenarios run anywhere:

| | in a keyless agent sandbox | on a box with keys |
|---|---|---|
| **model** | Claude Haiku via the `claude` CLI (already authenticated, no key) | same, unless `SANDBOX_LLM=openrouter` |
| **sheet** | `sandbox/data/sheet.json` | same, unless `SANDBOX_SHEET=real` |

Both defaults are the offline ones on purpose: a test run must never spend the
free-tier quota or write to the live spreadsheet by accident. Set the env vars
explicitly when you want a real smoke test.

Not covered: the web layer (`tests/test_web.py` owns login, card rendering,
signed tokens, replay) and the async `/chat` dispatch, which is deploy-only.

## Artifacts

- `sandbox/data/sheet.json` — the mock spreadsheet, one JSON object per tab. Open
  it; it's the answer to "did the row actually land, with the right columns?"
- `sandbox/data/logs/agent-*.jsonl` — the agent transcript, one line per event.
  This is where you find out *why* a scenario failed: what the model was sent,
  what it answered, which validation error it got fed back.

Both are gitignored and rebuilt on `--reset`.

## Gotchas

- **Haiku is a stand-in, not the shipping model.** It's smarter than the free-tier
  nemotron the app runs on, so a scenario passing here means "the tool is
  reachable and the flow works," not "nemotron will trigger it." The real
  model-quality verdict is `make eval` against `evals/golden`.
- **The CLI backend has no native tool channel**, so it runs the agent in json
  tool mode. Native mode is what deploys; if your change is mode-sensitive, say
  so and cover it in `tests/test_agent.py`, which exercises both.
- **It's slow** — every step is a real model call, so a full run is a couple of
  minutes. That's why it's not in `make test`.
- **A failure is evidence, not a verdict.** The model is non-deterministic, so
  read `sandbox/data/logs/` before concluding your code is broken: "expected
  edit_workout, model called nothing" often means the tool *description* doesn't
  say when to reach for it, which is a real finding. Re-run once to tell a wording
  problem from a flake.
- Each scenario turn burns model calls (one per agent step). Don't loop it.
