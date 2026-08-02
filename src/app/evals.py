"""Golden parse eval + per-field scorer — gates the model decision.

`make eval MODEL=<openrouter-id>` runs every fixture in evals/golden/ through the
*production* parse path (app.prompt.build_parse_messages + PARSE_SCHEMA — no
test-only prompt), scores per-field accuracy against the hand-verified expected
JSON, and writes a report to evals/reports/<model>.md.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

from app.config import settings
from app.kb_seed import STARTER_KB
from app.llm import OpenRouterClient
from app.models import ParseResult
from app.prompt import build_parse_messages, resolve_exercise

GOLDEN_DIR = pathlib.Path("evals/golden")
REPORTS_DIR = pathlib.Path("evals/reports")

PASS_THRESHOLD = 90.0  # aggregate field accuracy % — the model-decision gate

# Field groups scored per set-group.
SET_FIELDS = ["exercise", "grp", "sets", "reps", "per_side_lbs", "total_lbs", "assist_lbs"]
WORKOUT_FIELDS = ["date", "type"]


# --- transport ---


def call_openrouter(messages: list[dict], model: str, json_mode: bool = False) -> str:
    """Thin wrapper over the shared OpenRouter client (production transport)."""
    client = OpenRouterClient(model=model, timeout=90)
    return client.chat(messages, json_mode=json_mode).content


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Pull the JSON object out of a model reply that may wrap it in prose/fences.

    Tries the cleanest interpretations first so a well-behaved reply is never
    mangled by prose braces: whole text, then a fenced block, then a
    brace-to-brace slice as a last resort.
    """
    text = text.strip()
    candidates = [text]
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in reply")


# --- scoring ---


def _num_eq(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 1e-6


def _score_setgroup(exp: dict, pred: dict | None, kb) -> dict[str, bool]:
    if pred is None:
        return {f: False for f in SET_FIELDS}
    res = {}
    # resolve the prediction through the KB before comparing to the expected canonical
    pred_canon = resolve_exercise(str(pred.get("exercise", "")), kb) or str(
        pred.get("exercise", "")
    )
    res["exercise"] = pred_canon.lower() == str(exp["exercise"]).lower()
    res["grp"] = int(pred.get("grp", 0)) == int(exp["grp"])
    res["sets"] = int(pred.get("sets", 0)) == int(exp["sets"])
    res["reps"] = _num_eq(pred.get("reps"), exp["reps"])
    res["per_side_lbs"] = _num_eq(pred.get("per_side_lbs"), exp["per_side_lbs"])
    res["total_lbs"] = _num_eq(pred.get("total_lbs"), exp["total_lbs"])
    res["assist_lbs"] = _num_eq(pred.get("assist_lbs"), exp["assist_lbs"])
    return res


def _score_workout(exp: dict, pred: dict | None, kb) -> tuple[dict[str, bool], list[dict]]:
    wfields = {}
    if pred is None:
        wfields = {f: False for f in WORKOUT_FIELDS}
        pred_sets: list = []
    else:
        wfields["date"] = str(pred.get("date", "")) == str(exp["date"])
        wfields["type"] = str(pred.get("type", "")).lower() == str(exp["type"]).lower()
        pred_sets = pred.get("sets", []) or []
    exp_sets = exp.get("sets", []) or []
    set_scores = []
    for j in range(max(len(exp_sets), len(pred_sets))):
        e = exp_sets[j] if j < len(exp_sets) else None
        p = pred_sets[j] if j < len(pred_sets) else None
        if e is None:
            # extra predicted set-group → all fields wrong (penalize hallucination)
            set_scores.append({f: False for f in SET_FIELDS})
        else:
            set_scores.append(_score_setgroup(e, p, kb))
    return wfields, set_scores


@dataclass
class FixtureResult:
    name: str
    ok: bool  # response parsed into a valid ParseResult
    error: str = ""
    matched: int = 0
    total: int = 0
    field_hits: dict[str, list[bool]] = field(default_factory=dict)

    @property
    def pct(self) -> float:
        return 100.0 * self.matched / self.total if self.total else 0.0


def score_fixture(expected: dict, predicted: dict | None, kb) -> FixtureResult:
    exp_ws = expected.get("workouts", [])
    pred_ws = (predicted or {}).get("workouts", []) if predicted else []
    matched = total = 0
    field_hits: dict[str, list[bool]] = {}

    def record(fname: str, hit: bool):
        nonlocal matched, total
        total += 1
        matched += int(hit)
        field_hits.setdefault(fname, []).append(hit)

    for i in range(max(len(exp_ws), len(pred_ws))):
        e = exp_ws[i] if i < len(exp_ws) else None
        p = pred_ws[i] if i < len(pred_ws) else None
        if e is None:
            # extra predicted workout → penalize its workout fields
            for f in WORKOUT_FIELDS:
                record(f, False)
            continue
        wfields, set_scores = _score_workout(e, p, kb)
        for f, hit in wfields.items():
            record(f, hit)
        for sset in set_scores:
            for f, hit in sset.items():
                record(f, hit)
    return FixtureResult(name="", ok=True, matched=matched, total=total, field_hits=field_hits)


# --- runner ---


def load_fixtures() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        out.append((path.name, json.loads(path.read_text())))
    return out


def run_eval(
    model: str,
    guidance: str | None = None,
    json_mode: bool = False,
    limit: int | None = None,
) -> list[FixtureResult]:
    kb = STARTER_KB
    fixtures = load_fixtures()
    if limit:
        fixtures = fixtures[:limit]
    results: list[FixtureResult] = []
    for name, fx in fixtures:
        messages = build_parse_messages(fx["message"], kb, fx.get("now", ""), guidance)
        try:
            reply = call_openrouter(messages, model, json_mode=json_mode)
            predicted = extract_json(reply)
            ParseResult.model_validate(predicted)  # must be schema-valid
        except Exception as e:  # noqa: BLE001 — record and score as total miss
            fr = FixtureResult(name=name, ok=False, error=str(e)[:200])
            # still count expected field slots as missed so aggregate is honest
            scored = score_fixture(fx["expected"], None, kb)
            fr.matched, fr.total, fr.field_hits = (
                0,
                scored.total,
                {k: [False] * len(v) for k, v in scored.field_hits.items()},
            )
            results.append(fr)
            print(f"  {name}: PARSE ERROR — {fr.error}")
            continue
        scored = score_fixture(fx["expected"], predicted, kb)
        scored.name = name
        results.append(scored)
        print(f"  {name}: {scored.matched}/{scored.total} ({scored.pct:.0f}%)")
    return results


def _aggregate_fields(results: list[FixtureResult]) -> dict[str, tuple[int, int]]:
    agg: dict[str, list[bool]] = {}
    for r in results:
        for f, hits in r.field_hits.items():
            agg.setdefault(f, []).extend(hits)
    return {f: (sum(h), len(h)) for f, h in agg.items()}


def build_report(model: str, results: list[FixtureResult], guidance_label: str) -> str:
    total_matched = sum(r.matched for r in results)
    total_fields = sum(r.total for r in results)
    agg = total_matched / total_fields * 100 if total_fields else 0.0
    lines = [
        f"# Eval report — `{model}`",
        "",
        f"- Guidance: {guidance_label}",
        f"- Fixtures: {len(results)}",
        f"- **Aggregate field accuracy: {agg:.1f}%** "
        f"({total_matched}/{total_fields}) — threshold {PASS_THRESHOLD:.0f}%",
        f"- Verdict: {'PASS ✅' if agg >= PASS_THRESHOLD else 'BELOW THRESHOLD ❌'}",
        "",
        "## Per fixture",
        "",
        "| fixture | score | notes |",
        "|---|---|---|",
    ]
    for r in results:
        note = "" if r.ok else f"parse error: {r.error}"
        lines.append(f"| {r.name} | {r.matched}/{r.total} ({r.pct:.0f}%) | {note} |")
    lines += ["", "## Per field category", "", "| field | accuracy |", "|---|---|"]
    for f, (hit, tot) in sorted(_aggregate_fields(results).items()):
        pct = 100 * hit / tot if tot else 0
        lines.append(f"| {f} | {hit}/{tot} ({pct:.0f}%) |")
    lines.append("")
    return "\n".join(lines)


def _parse_argv(argv: list[str]) -> dict:
    opts = {"model": settings.model, "guidance": None, "json_mode": False, "limit": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("MODEL="):
            opts["model"] = a.split("=", 1)[1]
        elif a in ("--model", "-m"):
            i += 1
            opts["model"] = argv[i]
        elif a in ("--guidance", "-g"):
            i += 1
            opts["guidance"] = pathlib.Path(argv[i]).read_text()
            opts["guidance_label"] = argv[i]
        elif a == "--json-mode":
            opts["json_mode"] = True
        elif a == "--limit":
            i += 1
            opts["limit"] = int(argv[i])
        i += 1
    return opts


def main(argv: list[str] | None = None) -> None:
    opts = _parse_argv(argv if argv is not None else sys.argv[1:])
    model = opts["model"]
    if not model or model.startswith("MODEL="):
        print("Usage: make eval MODEL=<openrouter-model-id>", file=sys.stderr)
        sys.exit(2)
    guidance_label = opts.get("guidance_label", "base rules only")
    print(f"Running eval: model={model}, guidance={guidance_label}")
    results = run_eval(model, opts["guidance"], opts["json_mode"], opts["limit"])
    report = build_report(model, results, guidance_label)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    # Guided runs get their own file so with/without-skill reports coexist.
    if opts["guidance"]:
        tag = pathlib.Path(opts.get("guidance_label", "guided")).stem
        safe = f"{safe}__{re.sub(r'[^A-Za-z0-9._-]', '_', tag)}"
    out = REPORTS_DIR / f"{safe}.md"
    out.write_text(report + "\n")
    print("\n" + report)
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
