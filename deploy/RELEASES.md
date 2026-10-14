# Release log

What's running in production, newest first — `make deploy` appends an entry and
tells you to commit it. Each entry lists the pull requests merged since the last
one (anything pushed straight to main is listed under its own heading). PR titles
and shas only: no URL or namespace, since this file is tracked.

## 2026-08-13 00:49 UTC — `92255b5`

- Stop the chat spinner from outliving the turn it waits on
- Record the 2026-08-08 deploy in RELEASES.md

## 2026-08-08 01:35 UTC — `1f7aa31`

- Delete tests/test_release_notes.py
- Record every deploy in deploy/RELEASES.md
- Simplify the sandbox: one backend, three files, no second runner
- Add sandbox/: offline skill tester for keyless agent runs
- Tell the agent to check for stale totals after a base change
- Tighten update_kb after review
- Add update_kb tool so the agent can fix its own exercise KB
- Test the stale-edit-card guard directly
- Address review findings on edit_workout
- Add edit_workout: confirm-gated in-place correction of a logged workout
- Revert "Merge pull request #23 from SalimAlkharsa/claude/workout-edit-skill-t09j4o"
- Add edit_workout: confirm-gated in-place correction of a logged workout

## 2026-07-24 — `d8c9e4e`

- Baseline: the tree verified live on 2026-07-24. Entries above are generated.
