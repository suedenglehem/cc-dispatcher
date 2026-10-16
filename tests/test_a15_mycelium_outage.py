"""PRD §14 test 15 — when Mycelium is unavailable, launches proceed
without the memory section (degrade) unless memory_required=1, in which
case the job is blocked (needs a decision).

Phase 3: requires the mycelium client (respawn-once, single-flight) plus a
real `mycelium serve` to kill mid-run."""

import pytest

pytestmark = pytest.mark.skip(
    reason="Phase 3: requires a real mycelium serve subprocess")


def test_mycelium_outage_degrades_or_blocks(env):
    raise AssertionError("not implemented until Phase 3")
