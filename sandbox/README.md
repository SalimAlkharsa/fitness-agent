# sandbox — offline skill tester

Runs the real agent against a fake model and a fake sheet, so you can check a new
ability before it goes near Salim's data, his OpenRouter quota, or a deploy. No
keys, no network, nothing to configure.

It answers the one question unit tests can't:

> I gave the agent a new tool/skill. Does a model actually *reach for it* when a
> human phrases the request naturally — and does the write land?

```bash
make sandbox                                    # run every scenario
uv run python -m sandbox "squats 3x5 60 each"   # one ad-hoc message, verbose
uv run python -m sandbox --reset                # start from a fresh seeded sheet
```

## Adding a check for your new ability

Add a row to `scenarios.py`, run `make sandbox`. That's the workflow.

```python
{
    "say": "how much did I bench in July?",   # what a human would type
    "tool": "exercise_stats",                 # the tool it should reach for
    "confirm": True,                          # approve the card, so the write lands
    "edits": {"date": "2026-07-19"},          # card fields the user fixes first
    "adds": {"workouts": 1, "sets": 2},       # rows the turn should add
},
```

Only `say` is required. Scenarios share one seeded sheet (a later one can act on
what an earlier one logged) but each gets a fresh conversation, so what's tested
is the message alone reaching the tool, not the model coasting on chat context.

For anything the table can't express, drive it directly:

```python
sb = Sandbox(reset=True)
result = sb.say("squats 3x5 at 60 each side")
assert result.pending.tool == "log_workout"   # confirm-gated, nothing written yet
sb.confirm(result, date="2026-07-19")         # ✅, with the user fixing a field
assert sb.rows("workouts")[-1]["date"] == "2026-07-19"
assert "query_history" in sb.tools_used()     # read tools show up here too
```

## What's real

Everything except the two mocked edges: the agent loop, tool menu, system prompt,
KB, skill loading, date resolution, validation/retry/inbox guards, and the commit
path. Mocked are the model (Claude Haiku via the `claude` CLI, already
authenticated) and the spreadsheet (`sandbox/data/sheet.json`).

Not covered: the web layer — `tests/test_web.py` owns login, card rendering,
signed tokens, replay — and the async `/chat` dispatch, which is deploy-only.

## Artifacts (gitignored, rebuilt on `--reset`)

- `sandbox/data/sheet.json` — the mock spreadsheet, one entry per tab. Answers
  "did the row land, with the right columns?"
- `sandbox/data/logs/agent-*.jsonl` — the transcript: what the model was sent,
  what it answered, which validation error it got fed back.

## Gotchas

- **Haiku is a stand-in, not a verdict.** It's stronger than the free-tier
  nemotron the app runs on, so green here means "the tool is reachable and the
  flow works," not "nemotron will trigger it." `make eval` is the model judge.
- **A failure is evidence, not a verdict either.** The model is
  non-deterministic: read the transcript before concluding your code is broken.
  "expected edit_workout, model called nothing" often means the tool
  *description* doesn't say when to reach for it — a real finding. Re-run once to
  tell a wording problem from a flake.
- **It's slow** (~2 min: every agent step is a real model call), which is why
  it's not part of `make test`.
