# Model decision

**Date:** 2026-07-15
**Decision: stay on `nvidia/nemotron-3-ultra-550b-a55b:free`.**

## Evidence

Ran the golden parse eval (`make eval`) through the production parse path
(`app.prompt` → OpenRouter → per-field scoring) over the 13 hand-verified
fixtures in `evals/golden/`.

| model | guidance | aggregate field accuracy | verdict |
|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | base rules only | **94.0%** (250/266) | **PASS** (≥90% threshold) |

The per-run report is written to `evals/reports/<model>.md`, which is gitignored
(it embeds the full parse output); re-generate it with `make eval MODEL=<id>`.

### Per-field breakdown

| field | accuracy |
|---|---|
| date | 100% |
| type | 100% |
| sets | 100% |
| per_side_lbs | 100% |
| total_lbs | 100% |
| assist_lbs | 100% |
| exercise (via KB) | 94% |
| reps | 94% |
| **grp** | **65%** |

## Interpretation

Nemotron clears the 90% gate comfortably. Weight math (per-side doubling + KB
base), stack passthrough, assist weights, dates, and session typing are all
essentially perfect — the hardest domain-specific rules are handled.

The one weak field is **`grp`** (set-group ordinal), missed mostly on the
multi-group "then"/prose cases (fixtures 02 negatives and 06 soleus). That is
exactly what the parsing skill targets, so `grp` is the field expected to move.

## Parsing skill impact

Re-ran the same eval with `skills/parsing-salims-logs/SKILL.md` injected as guidance
(the production path — the agent reads this skill before logging):

| run | aggregate | grp | exercise | reps |
|---|---|---|---|---|
| base rules only | 94.0% (250/266) | 65% | 94% | 94% |
| **+ parsing skill** | **97.7% (260/266)** | **94%** | 94% | 94% |

The skill closes almost the entire gap, and all of it is the `grp` (set-group
ordinal) field the base run struggled with — the "then"/multi-group cases. With ≥
without, so the skill earns its place.

## Model config

No `FALLBACK_MODEL` is set — the free tier passed. If free-tier rate limits or
availability become a problem in practice, the documented fallback is a
Haiku-class paid model set via `FALLBACK_MODEL` in `.env`. Model choice is read
from config, so switching needs no code change.

## Outcome

Staying on the free tier, no deposit. Day-to-day single-user logging fits inside
the free daily request cap comfortably; the thing that strains it is backfilling
a large history in one go (one request per message), so pace that.
