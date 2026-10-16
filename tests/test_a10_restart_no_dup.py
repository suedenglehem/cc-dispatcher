"""PRD §14 test 10 — kill -9 dispatcherd mid-job, restart, the running
attempt is re-adopted by process identity (pid, pgid, starttime), never
re-spawned: exactly one worker process per attempt, capacity reconciled.

Phase 3: needs a REAL dispatcherd subprocess (SIGKILL between spawn and
identity UPDATE, then a fresh process running startup_reconcile), which the
in-process harness cannot provide."""

import pytest

pytestmark = pytest.mark.skip(
    reason="Phase 3: requires a real dispatcherd subprocess + SIGKILL")


def test_restart_adopts_without_duplicate(env):
    raise AssertionError("not implemented until Phase 3")
