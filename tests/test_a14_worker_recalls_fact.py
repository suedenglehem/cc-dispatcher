"""PRD §14 test 14 — a fact the primary saves to Mycelium before submission
appears in the worker's task packet (bounded recall at packet render time).

Phase 3: needs a real `mycelium serve` subprocess under
MYCELIUM_CONFIG→temp store (the user's ~/.mycelium is never touched)."""

import pytest

pytestmark = pytest.mark.skip(
    reason="Phase 3: requires a real mycelium serve subprocess")


def test_worker_packet_contains_recalled_fact(env):
    raise AssertionError("not implemented until Phase 3")
