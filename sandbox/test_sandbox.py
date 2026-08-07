"""The scenarios as a pytest run: `uv run pytest sandbox`.

Deliberately outside `make test` (pyproject's testpaths is tests/): every
scenario calls a real model, so this is tens of seconds and belongs in the
tester, not the fast unit suite. Skips itself when the environment has no LLM.
"""

from __future__ import annotations

import pytest

from sandbox.harness import Sandbox, make_llm, run_scenario
from sandbox.scenarios import SCENARIOS

pytestmark = pytest.mark.skipif(
    make_llm()[0] is None, reason="no LLM backend (needs the `claude` CLI or OPENROUTER_API_KEY)"
)


@pytest.fixture(scope="module")
def sb() -> Sandbox:
    return Sandbox(reset=True)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["say"][:24])
def test_scenario(sb: Sandbox, scenario: dict) -> None:
    ok, report = run_scenario(sb, scenario)
    assert ok, report
