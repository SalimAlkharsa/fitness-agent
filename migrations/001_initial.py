"""Migration 001 — schema v1 marker.

Bootstrap (`python -m app.bootstrap`) creates the v1 tab structure directly, so
this migration is a no-op. It exists so the migration history starts at a known
point and `app.migrate --status` reports a sensible latest version.
"""

from __future__ import annotations


def apply(store) -> None:  # noqa: ANN001 — store is app.store.SheetsStore
    # Schema v1 is created by bootstrap; nothing to transform.
    return
