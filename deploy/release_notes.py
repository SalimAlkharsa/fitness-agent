"""Record what just went live — run by `make deploy` after doctl succeeds.

Appends to `deploy/RELEASES.md`: when, the commit deployed, and every pull
request merged since the previous entry. PRs, not commits: a release entry should
read as the list of changes that shipped, and "address review comments" reads as
noise next to the PR it belongs to. Both GitHub merge styles count — a merge
commit ("Merge pull request #29 from …", PR title in its body) and a squash
("Add the /clear command (#29)"). Anything pushed straight to main has no PR to
stand for it, so it's listed separately rather than dropped — the log's job is to
say what's running.

Git data only — no URL, namespace, or API host, since this file is tracked and
the repo is public.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

RELEASES = Path(__file__).resolve().parent / "RELEASES.md"
# Entries are newest-first, so the first sha is the last thing deployed.
_SHA = re.compile(r"^## .*`([0-9a-f]{7,40})`", re.M)
# "Merge pull request #29 from SalimAlkharsa/branch" — a GitHub merge commit.
_MERGE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
# "Add the /clear command (#29)" — a GitHub squash merge, one commit on main.
_SQUASH = re.compile(r"^(.*) \(#(\d+)\)$")

# A commit subject/body can contain anything printable, so records and fields are
# split on control characters git will never emit as content.
_REC, _FLD = "\x1e", "\x1f"
_FORMAT = _FLD.join(["%H", "%P", "%s", "%b"]) + _REC


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


def _log(span: str, fmt: str, *flags: str) -> list[tuple[str, ...]]:
    """Split `git log` into field tuples, newest commit first."""
    out = _git("log", *flags, f"--format={fmt}{_REC}", span)
    records = (r.strip("\n") for r in out.split(_REC))
    return [tuple(r.split(_FLD)) for r in records if r]


def release_body(span: str) -> str:
    """The bullet list for one release: merged PRs, then anything pushed direct.

    Walking newest-first means a PR's merge commit is seen before the commits it
    brought in, so those can be folded into the PR entry instead of listed twice.
    A merge that isn't a PR merge folds nothing away — its commits still get
    listed, since nothing else would speak for them.
    """
    prs: list[str] = []
    direct: list[str] = []
    merged: set[str] = set()
    for sha, parents, subject, body in _log(span, _FORMAT):
        if sha in merged:
            continue
        parent_shas = parents.split()
        if len(parent_shas) > 1:  # a merge commit
            match = _MERGE.match(subject)
            if not match:
                continue
            number, branch = match.groups()
            # --no-merges: a PR branch can carry another PR's merge commit (cut
            # from main while that one was in flight), and it deserves its entry.
            brought_in = _log(f"{parent_shas[0]}..{parent_shas[1]}", f"%H{_FLD}%s", "--no-merges")
            merged.update(c[0] for c in brought_in)
            # GitHub writes the PR title into the merge body, but an API merge can
            # leave it empty — then the branch's first commit says more than its name.
            title = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            prs.append(f"- #{number} {title or (brought_in[-1][1] if brought_in else branch)}")
            continue
        match = _SQUASH.match(subject)
        if match:
            prs.append(f"- #{match.group(2)} {match.group(1)}")
        else:
            direct.append(f"- {subject}")
    if not prs and not direct:
        return "- redeploy — nothing new since the last entry"
    parts = ["\n".join(prs)] if prs else []
    if direct:
        parts.append("Straight to main (no PR):\n\n" + "\n".join(direct))
    return "\n\n".join(parts)


def main() -> None:
    text = RELEASES.read_text()
    previous = previous_sha(text)
    sha = _git("rev-parse", "--short", "HEAD")
    body = release_body(f"{previous}..HEAD" if previous else "HEAD")
    if _git("status", "--porcelain"):
        body = "- ⚠️ dirty tree — uncommitted changes went out too\n" + body
    when = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")

    RELEASES.write_text(insert(text, f"## {when} — `{sha}`\n\n{body}\n\n"))
    print(f"\nRecorded {sha} in {RELEASES} — commit it so the log matches what's running.")


if __name__ == "__main__":
    main()
