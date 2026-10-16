"""PRD §14 test 8 — paused jobs never resume by themselves: switching back
to team mode alone does not launch them; an explicit resume (in team mode,
with a re-validated base) is the only path back. Resume in single mode is
rejected."""

import time

from local_dispatcher.models import ApiError, ErrorCodes
from tests.helpers import wait_job


def test_paused_jobs_need_explicit_resume(env):
    env.backend()
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "3"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    ja = c.submit("p1", objective="A (drains)", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]
    jb = c.submit("p1", objective="B (pauses)", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]

    wait_job(c, ja, "running")
    c.set_mode("single", project_id="p1")
    wait_job(c, jb, "paused")
    wait_job(c, ja, "succeeded")

    # back to team — B must stay paused (no silent resume)
    c.set_mode("team", project_id="p1")
    time.sleep(1.5)  # several ticks
    vb = c.job(jb)
    assert vb["status"] == "paused"
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jb,))[0]["n"] == 0

    # resume while in single mode is rejected
    c.set_mode("single", project_id="p1")
    try:
        c.resume(jb)
        raise AssertionError("resume in single mode must be rejected")
    except ApiError as e:
        assert e.code == ErrorCodes.RESUME_MODE_SINGLE

    # resume in team mode works and re-validates the base
    c.set_mode("team", project_id="p1")
    v = c.resume(jb)
    assert v["status"] == "queued"
    vb = wait_job(c, jb, "succeeded")
    assert vb["failure_class"] is None
