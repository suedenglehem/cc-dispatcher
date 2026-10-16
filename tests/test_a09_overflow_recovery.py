"""PRD §14 test 9 — a context-overflow failure is retried in the SAME
worktree with a fresh session and a recovery context (no checkpoint was
written, so uncertain=true); the job succeeds on attempt 2."""

import json

from tests.helpers import wait_attempts, wait_job


def test_overflow_retries_with_recovery_context(env):
    env.backend()
    h = env.daemon(worker_mode="overflow")
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1", objective="overflow then recover",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]
    v = wait_job(c, jid, "succeeded")
    assert v["failure_class"] is None
    assert v["attempt_count"] == 2

    att = wait_attempts(h, jid, 2)
    a1, a2 = att
    assert a1["failure_class"] == "context_overflow"
    assert a1["status"] == "failed"
    assert a2["status"] == "succeeded"
    assert a2["failure_class"] is None

    # same worktree/branch, fresh attempt, linked to the failed one
    assert a2["worktree_path"] == a1["worktree_path"]
    assert a2["branch"] == a1["branch"]
    assert a2["recovery_from"] == a1["id"]

    ctx = json.loads(a2["recovery_context"])
    assert ctx["uncertain"] is True  # attempt 1 died before a checkpoint
    assert "log_tail" in ctx
