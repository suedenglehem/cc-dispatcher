"""PRD §14 test 18 — a worker that writes outside its allowed_scope still
succeeds, but the job is flagged: failure_class=scope_violation,
scope_review_required, with the offending path recorded."""

from tests.helpers import wait_attempts, wait_job


def test_scope_violation_is_flagged(env):
    env.backend()
    h = env.daemon(worker_mode="scope_violation")
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1", objective="stay in src/", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    v = wait_job(c, jid, "succeeded")

    assert v["failure_class"] == "scope_violation"
    assert v["status_detail"] == "scope_review_required"
    assert "out/scope.py" in v["scope_violations"]

    att = wait_attempts(h, jid, 1)
    assert att[0]["status"] == "succeeded"
    assert att[0]["failure_class"] == "scope_violation"
