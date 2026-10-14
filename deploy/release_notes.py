"""Record what just went live — run by `make deploy` after doctl succeeds.

Appends to `deploy/RELEASES.md`: when, the commit deployed, and everything that
landed on main since the previous entry — a pull request per line, plus anything
pushed straight to main. PRs, not commits: "address review comments" is noise
next to the PR it belongs to. Git data only — no URL, namespace, or API host,
since this file is tracked and the repo is public.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

RELEASES = Path(__file__).resolve().parent / "RELEASES.md"
# Entries are newest-first, so the first sha is the last thing deployed.
_SHA = re.compile(r"^## .*`([0-9a-f]{7,40})`", re.M)
# "Merge pull request #29 from SalimAlkharsa/branch" (title in the body) and
# "Add the /clear command (#29)" — GitHub's two merge styles.
_MERGE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
_SQUASH = re.compile(r"^(.*) \(#(\d+)\)$")
# Field/record separators git will never emit as commit text.
_FLD, _REC = "\x1f", "\x1e"


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


def release_body(span: str) -> str:
    """PRs merged in the range, then anything pushed straight to main.

    --first-parent walks main itself, so every entry is one thing that landed
    there — a PR merge or a direct commit — and a PR's own commits stay folded
    into its line, which is the whole point of listing PRs.
    """
    prs, direct = [], []
    log = _git("log", "--first-parent", f"--format=%s{_FLD}%b{_REC}", span)
    for record in log.split(_REC):
        subject, _, body = record.strip("\n").partition(_FLD)
        merge, squash = _MERGE.match(subject), _SQUASH.match(subject)
        if merge:
            number, branch = merge.groups()
            # The body carries the PR title — except after an API merge, which
            # leaves it empty and the branch name as the only thing left to say.
            title = next((ln.strip() for ln in body.splitlines() if ln.strip()), branch)
            prs.append(f"- #{number} {title}")
        elif squash:
            prs.append(f"- #{squash.group(2)} {squash.group(1)}")
        elif subject:
            direct.append(f"- {subject}")
    body = "\n".join(prs)
    if direct:
        body += ("\n\n" if body else "") + "Straight to main (no PR):\n\n" + "\n".join(direct)
    return body or "- redeploy — nothing new since the last entry"


def main() -> None:
    text = RELEASES.read_text()
    previous = previous_sha(text)
    sha = _git("rev-parse", "--short", "HEAD")
    body = release_body(f"{previous}..HEAD" if previous else "HEAD")
    if _git("status", "--porcelain"):
        # Blank line, not a newline: the body can open with the "Straight to main"
        # heading, which Markdown would otherwise fold into this bullet.
        body = "- ⚠️ dirty tree — uncommitted changes went out too\n\n" + body
    when = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")

    RELEASES.write_text(insert(text, f"## {when} — `{sha}`\n\n{body}\n\n"))
    print(f"\nRecorded {sha} in {RELEASES} — commit it so the log matches what's running.")


if __name__ == "__main__":
    main()
