"""PRD §14 test 16 — the worker and the primary share one Mycelium project
key: the worker's [worker-checkpoint] saves land in the same store the
primary reads (verified by the primary seeing the checkpoint via recall).

Phase 3: requires a real `mycelium serve` subprocess under
MYCELIUM_CONFIG→temp store."""

import pytest

pytestmark = pytest.mark.skip(
    reason="Phase 3: requires a real mycelium serve subprocess")


def test_worker_and_primary_share_memory_key(env):
    raise AssertionError("not implemented until Phase 3")
