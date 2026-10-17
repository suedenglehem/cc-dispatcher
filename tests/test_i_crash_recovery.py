"""P3 step 2 (non-acceptance) — crash / checkpoint-then-crash / tests-fail
recovery, driving the fake-worker modes that a09 (overflow) doesn't cover.

  crash_once           attempt 1 dies (exit 3, no checkpoint) → crash,
                       retried in the SAME worktree with an UNCERTAIN
                       recovery context → attempt 2 succeeds.
  checkpoint_then_crash attempt 1 saves a checkpoint, then dies → attempt 2
                       gets a CERTAIN recovery context (checkpoint present)
                       and succeeds.
  backend_down         attempt 1's API call fails (connection refused) →
                       backend_disconnect, retried like a crash → succeeds.
  tests_fail           attempt 1 reports outcome=failed with a failed test →
                       tests_failed, NON-retryable (allow_test_repair=0),
                       the job fails on attempt 1 with the result preserved.

For the two crash tests the profile sets FAKE_ECHO_PACKET, so the attempt-2
run dumps the packet it actually received: the recovery section must be in
the worker's stdin, not just in the database.
"""

import json
import os

from tests.helpers import wait_attempts, wait_job

ECHO = dict(id="w", env_overrides={"FAKE_ECHO_PACKET": "1"})


def _packet_text(client, job_id: str, attempt_no: int) -> str:
    v = client.job(job_id)
    # the view's `artifacts` is the LATEST attempt's dir; for these tests
    # the attempt we echo is the last one (attempt 2), so it matches
    assert attempt_no == v["attempt_count"], "echo dir = last attempt only"
    path = os.path.join(v["artifacts"], "echo_packet.txt")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_crash_retries_in_same_worktree_with_uncertain_context(env):
    env.backend()
    h = env.daemon(worker_mode="crash_once", profiles=[ECHO])
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="crash then recover",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]

    v = wait_job(c, jid, "succeeded")
    assert v["failure_class"] is None
    assert v["attempt_count"] == 2

    a1, a2 = wait_attempts(h, jid, 2)
    assert a1["status"] == "failed"
    assert a1["exit_code"] == 3
    assert a1["failure_class"] == "crash"
    assert a2["status"] == "succeeded"
    assert a2["failure_class"] is None

    # same worktree/branch, fresh session linked to the failed attempt
    assert a2["worktree_path"] == a1["worktree_path"]
    assert a2["branch"] == a1["branch"]
    assert a2["recovery_from"] == a1["id"]

    ctx = json.loads(a2["recovery_context"])
    assert ctx["uncertain"] is True  # no checkpoint was saved
    assert ctx["checkpoint"] is None
    assert ctx["log_tail"]  # attempt 1's stream is the only evidence
    assert ctx["diff"] == ""  # the crash left no edits

    # the UNCERTAIN recovery context reached attempt 2's packet
    pkt = _packet_text(c, jid, 2)
    assert "NO CHECKPOINT WAS SAVED" in pkt
    assert "uncertain" in pkt
    assert "PREVIOUS CHECKPOINT" not in pkt
    assert a1["id"] in pkt  # from_attempt attribution


def test_checkpoint_then_crash_gets_certain_recovery_context(env):
    env.backend()
    h = env.daemon(worker_mode="checkpoint_then_crash", profiles=[ECHO])
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="checkpoint then crash",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]

    v = wait_job(c, jid, "succeeded")
    assert v["failure_class"] is None
    assert v["attempt_count"] == 2

    a1, a2 = wait_attempts(h, jid, 2)
    assert a1["status"] == "failed"
    assert a1["exit_code"] == 3
    assert a1["failure_class"] == "crash"

    ctx = json.loads(a2["recovery_context"])
    assert ctx["uncertain"] is False  # the checkpoint WAS saved
    cp = ctx["checkpoint"]
    assert cp["status"] == "in_progress"
    assert cp["next_action"] == "continue"
    assert cp["remaining"] == ["finish the rest"]

    # the CERTAIN recovery context reached attempt 2's packet, attributed
    # to the attempt it came from
    pkt = _packet_text(c, jid, 2)
    assert f"PREVIOUS CHECKPOINT (from attempt {a1['id']})" in pkt
    assert "in_progress" in pkt
    assert "NO CHECKPOINT WAS SAVED" not in pkt


def test_backend_down_is_retried_like_crash(env):
    env.backend()
    h = env.daemon(worker_mode="backend_down")
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="backend blip then recover",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]

    v = wait_job(c, jid, "succeeded")
    assert v["failure_class"] is None
    assert v["attempt_count"] == 2

    a1, a2 = wait_attempts(h, jid, 2)
    assert a1["status"] == "failed"
    assert a1["failure_class"] == "backend_disconnect"
    assert a2["status"] == "succeeded"
    assert a2["recovery_from"] == a1["id"]

    # same uncertain-context recovery as a crash: no checkpoint was saved
    ctx = json.loads(a2["recovery_context"])
    assert ctx["uncertain"] is True
    assert ctx["checkpoint"] is None
    assert ctx["log_tail"]  # the API error is in the stream tail


def test_tests_fail_is_non_retryable(env):
    env.backend()
    h = env.daemon(worker_mode="tests_fail")
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="tests fail",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]

    v = wait_job(c, jid, "failed")
    assert v["failure_class"] == "tests_failed"
    assert v["status_detail"] == "worker_reported_failed"
    assert v["attempt_count"] == 1  # allow_test_repair=0 → no retry
    assert v["attempts"][0]["failure_class"] == "tests_failed"

    # the worker's report is preserved on the job for review
    row = h.query("SELECT result_json FROM jobs WHERE id=?", (jid,))[0]
    res = json.loads(row["result_json"])
    assert res["outcome"] == "failed"
    assert res["tests"][0]["status"] == "failed"
