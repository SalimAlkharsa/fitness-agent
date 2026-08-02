"""Backfill CLI: replay a historical dump through the parse pipeline.

`python -m app.backfill dump.txt [--commit]` splits the dump into messages, skips
any already committed (normalized-raw_text hash dedup), parses each through the
same schema as live logging, and either previews or commits them. Unknown
exercises grow the KB as they're confirmed, so later messages ask fewer questions.

The web "Backfill" review view (app/web.py) reuses this to render one confirm card
per parsed workout with per-card ✅/❌ and a "confirm all" button.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# raw_text hashing lives in app.dedup (single source of truth); re-exported here
# so backfill's importers keep working and the two paths can't drift.
from app.dedup import committed_hashes, normalize_raw, raw_hash
from app.models import ParsedWorkout

__all__ = [
    "BackfillItem",
    "committed_hashes",
    "main",
    "normalize_raw",
    "parse_dump",
    "raw_hash",
    "split_dump",
]


def split_dump(text: str) -> list[str]:
    """Split a dump into candidate messages.

    Messages are separated by a blank-line *gap* of two or more empty lines, so a
    single blank line inside one message (prose-style logs) keeps it together.
    """
    blocks = re.split(r"\n[ \t]*\n[ \t]*\n+", text.strip())
    return [b.strip() for b in blocks if b.strip()]


@dataclass
class BackfillItem:
    message: str
    workouts: list[ParsedWorkout] = field(default_factory=list)
    duplicate: bool = False
    error: str = ""

    @property
    def needs_date(self) -> bool:
        return any(w.date in ("", "unknown") for w in self.workouts)


def parse_dump(dump_text: str, parse_fn, store) -> list[BackfillItem]:
    """Parse every message in a dump up front, skipping duplicates.

    Used by the web review view (which shows all cards at once) and tests;
    `parse_fn(message) -> ParseResult` is injected so tests need no network.
    Dedup is against both already-committed workouts and earlier messages in this
    run. The CLI `--commit` path instead parses and commits one message at a time
    (see `main`) so auto-learned KB aliases resolve later messages at parse time.
    """
    seen = committed_hashes(store)
    items: list[BackfillItem] = []
    for message in split_dump(dump_text):
        h = raw_hash(message)
        if h in seen:
            items.append(BackfillItem(message=message, duplicate=True))
            continue
        seen.add(h)
        try:
            result = parse_fn(message)
            workouts = list(result.workouts)
            # Pin raw_text to the exact message so re-running backfill dedups
            # reliably (the model may not echo the message verbatim).
            for pw in workouts:
                pw.raw_text = message
            items.append(BackfillItem(message=message, workouts=workouts))
        except Exception as e:  # noqa: BLE001 — record and keep going
            items.append(BackfillItem(message=message, error=str(e)[:200]))
    return items


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m app.backfill dump.txt [--commit]")
        return
    path = argv[0]
    do_commit = "--commit" in argv

    from app.config import settings
    from app.llm import OpenRouterClient
    from app.logflow import autolearn_kb_updates, commit_workout
    from app.parse import parse_workouts
    from app.store import SheetsStore

    dump_text = open(path).read()  # noqa: SIM115
    store = SheetsStore.open()
    client = OpenRouterClient(model=settings.model)

    # Parse (and, with --commit, write) message-by-message, re-reading the KB
    # between each so aliases auto-learned from earlier messages resolve later ones
    # at parse time too.
    seen = committed_hashes(store)
    kb = store.kb_entries()
    messages = split_dump(dump_text)
    n_new = n_dup = n_err = 0

    for message in messages:
        h = raw_hash(message)
        if h in seen:
            n_dup += 1
            print(f"  [DUP] {message[:50]!r}")
            continue
        seen.add(h)
        try:
            result = parse_workouts(message, kb, client, backfill=True)
            workouts = list(result.workouts)
            for pw in workouts:
                pw.raw_text = message  # pin so re-runs dedup reliably
        except Exception as e:  # noqa: BLE001 — record and keep going
            n_err += 1
            print(f"  [ERR {str(e)[:120]}] {message[:50]!r}")
            continue
        n_new += 1
        for pw in workouts:
            flag = " (date unknown — needs manual entry)" if pw.date in ("", "unknown") else ""
            print(f"  {pw.type} {pw.date} — {len(pw.sets)} set-groups{flag}")
            if do_commit:
                updates = autolearn_kb_updates(pw, kb)
                _w, summary = commit_workout(store, pw, updates)
                kb = store.kb_entries()  # refreshed so later messages benefit
                learned = f" (+{len(updates)} new KB)" if updates else ""
                print(f"    ✓ {summary}{learned}")

    print(f"\n{len(messages)} messages: {n_new} new, {n_dup} duplicates, {n_err} errors")
    if not do_commit:
        print("Dry run. Re-run with --commit to write, or use the web Backfill view.")


if __name__ == "__main__":
    main()
