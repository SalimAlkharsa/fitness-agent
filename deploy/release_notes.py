"""Record what just went live — run by `make deploy` after doctl succeeds.

Appends to `deploy/RELEASES.md`: when, the commit deployed, and every commit
subject since the previous entry. Git data only — no URL, namespace, or API
host, since this file is tracked and the repo is public.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

RELEASES = Path(__file__).resolve().parent / "RELEASES.md"
# Entries are newest-first, so the first sha is the last thing deployed.
_SHA = re.compile(r"^## .*`([0-9a-f]{7,40})`", re.M)


def previous_sha(text: str) -> str | None:
    """The commit the newest entry recorded — where this release's range starts."""
    match = _SHA.search(text)
    return match.group(1) if match else None


def insert(text: str, entry: str) -> str:
    """Newest entry on top, under the header prose."""
    head, marker, rest = text.partition("## ")
    return head + entry + marker + rest


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def main() -> None:
    text = RELEASES.read_text()
    previous = previous_sha(text)
    sha = _git("rev-parse", "--short", "HEAD")
    # --no-merges: "Merge pull request #25 from ..." says nothing about what shipped.
    span = f"{previous}..HEAD" if previous else "HEAD"
    body = _git("log", "--no-merges", "--format=- %s", span) or "- redeploy — no new commits"
    if _git("status", "--porcelain"):
        body = "- ⚠️ dirty tree — uncommitted changes went out too\n" + body
    when = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")

    RELEASES.write_text(insert(text, f"## {when} — `{sha}`\n\n{body}\n\n"))
    print(f"\nRecorded {sha} in {RELEASES} — commit it so the log matches what's running.")


if __name__ == "__main__":
    main()
