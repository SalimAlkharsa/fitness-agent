"""The one silent failure in deploy/release_notes.py: if a new entry doesn't land
on top, every later release diffs from the wrong commit and lists wrong notes."""

from __future__ import annotations

from deploy.release_notes import insert, previous_sha

LOG = "# Release log\n\nprose\n\n## 2026-07-24 — `d8c9e4e`\n\n- baseline\n"


def test_the_newest_entry_wins_the_next_range():
    assert previous_sha(LOG) == "d8c9e4e"
    assert previous_sha("# Release log\n\nno entries yet\n") is None

    grown = insert(LOG, "## 2026-08-08 — `bedf435`\n\n- add sandbox\n\n")
    assert previous_sha(grown) == "bedf435"
    assert grown.startswith("# Release log\n\nprose\n\n##")  # header prose survives
    assert grown.index("bedf435") < grown.index("d8c9e4e")  # newest first
