"""`python -m sandbox` — the skill tester.

  uv run python -m sandbox                     run every scenario in scenarios.py
  uv run python -m sandbox "squats 3x5 60lbs"  one ad-hoc message, verbose
  uv run python -m sandbox --reset             start from a fresh seeded sheet

Exits non-zero if any scenario fails, so it works as a check in a loop.
"""

from __future__ import annotations

import sys

from sandbox.harness import LOG_DIR, Sandbox, run_scenario
from sandbox.scenarios import SCENARIOS


def main(argv: list[str]) -> int:
    words = [a for a in argv if not a.startswith("--")]
    sb = Sandbox(reset="--reset" in argv)
    print(f"backend {sb.backend} | tool mode {sb.agent.tool_mode} | sheet {sb.sheet_path}\n")

    failed = 0
    if words:
        result = sb.say(" ".join(words))
        print(f"tools called: {sb.tools_used() or '(none)'}")
        if result.pending:
            card = f"{result.pending.tool} — {result.pending.summary}"
            print(f"confirm card: {card}\ncommitted:    {sb.confirm(result)}")
        else:
            print(f"answer:       {result.text}")
    else:
        for scenario in SCENARIOS:
            ok, report = run_scenario(sb, scenario)
            failed += not ok
            print(report)

    print(f"\nsheet artifact: {sb.sheet_path}")
    if failed:
        print(f"{failed}/{len(SCENARIOS)} scenarios failed — transcripts in {LOG_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
