"""Demo migration (tests only) — adds an `rpe` column to the sets tab.

Proves the standard read → transform → clear → write-back pattern end-to-end
without shipping a real schema change. Idempotent.
"""

from __future__ import annotations


def apply(store) -> None:  # noqa: ANN001
    ws = store._ws("sets")
    values = ws.get_all_values()
    if not values:
        return
    header = values[0]
    if "rpe" in header:
        return
    header = header + ["rpe"]
    rows = [header] + [row + [""] for row in values[1:]]
    ws.clear()
    ws.update(rows, "A1")
