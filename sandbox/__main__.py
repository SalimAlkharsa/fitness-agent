"""`python -m sandbox` — the skill tester.

  make sandbox                                 run every scenario in scenarios.py
  uv run python -m sandbox "squats 3x5 60lbs"  one ad-hoc message, verbose
  uv run python -m sandbox --reset             start from a fresh seeded sheet

Exits non-zero if any scenario fails.
"""

from __future__ import annotations

import sys

from sandbox.harness import HAIKU, LOGS, SHEET, Sandbox, run_scenario
from sandbox.scenarios import SCENARIOS


def main(argv: list[str]) -> int:
    words = [a for a in argv if not a.startswith("--")]
    sb = Sandbox(reset="--reset" in argv)
    print(f"model {HAIKU} | sheet {SHEET}\n")

    if words:
        result = sb.say(" ".join(words))
        print(f"tools called: {sb.tools_used() or '(none)'}")
        if result.pending:
            print(f"confirm card: {result.pending.summary}\ncommitted:    {sb.confirm(result)}")
        else:
            print(f"answer:       {result.text}")
        return 0

    failed = 0
    for scenario in SCENARIOS:
        ok, report = run_scenario(sb, scenario)
        failed += not ok
        print(report)
    print(f"\nsheet {SHEET}" + (f"\ntranscripts {LOGS}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
