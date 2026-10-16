"""PRD §14 test 17 — exit 0 without a valid result report is NOT success:
the attempt fails with invalid_result and the job is left for a decision
(no auto-retry of invalid_result)."""

import time

from tests.helpers import wait_attempts, wait_job


def test_zero_exit_without_report_is_failure(env):
    env.backend()
    h = env.daemon(worker_mode="invalid_report")
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1", objective="silent exit", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    v = wait_job(c, jid, "failed")
    assert v["failure_class"] == "invalid_result"

    att = wait_attempts(h, jid, 1)
    assert att[0]["status"] == "failed"
    assert att[0]["failure_class"] == "invalid_result"
    assert att[0]["exit_code"] == 0  # the process WAS clean

    # invalid_result is not auto-retried: stays failed, one attempt
    time.sleep(1.0)  # several ticks
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jid,))[0]["n"] == 1
    assert c.job(jid)["status"] == "failed"
