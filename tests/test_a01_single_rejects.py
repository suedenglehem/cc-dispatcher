"""PRD §14 test 1 — single mode rejects submissions with an actionable
error; team mode accepts (and runs) them."""

from local_dispatcher.models import ApiError, ErrorCodes

from tests.helpers import wait_job


def test_single_rejects_submit(env):
    env.backend()
    h = env.daemon()
    c = h.client()

    try:
        c.submit("p1", objective="add a helper", base_commit=env.repo_head,
                 allowed_scope=["src"], profile_id="w")
        raise AssertionError("submit in single mode must be rejected")
    except ApiError as e:
        assert e.code == ErrorCodes.MODE_SINGLE

    # no job row was created
    assert h.query("SELECT count(*) AS n FROM jobs")[0]["n"] == 0


def test_team_accepts_and_runs(env):
    env.backend()
    h = env.daemon()
    c = h.client()

    c.set_mode("team", project_id="p1")
    out = c.submit("p1", objective="add a helper", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")
    job_id = out["job_id"]
    assert out["created"] is True

    v = wait_job(c, job_id, "succeeded")
    assert v["failure_class"] is None
    # exactly one attempt, succeeded
    att = h.query("SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_no",
                  (job_id,))
    assert len(att) == 1
    assert att[0]["status"] == "succeeded"
