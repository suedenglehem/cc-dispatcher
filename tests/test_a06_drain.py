"""PRD §14 test 6 — switching to single mode with the drain policy lets the
running worker finish while queued jobs are paused (never launched);
switching back to team and resuming lets the paused job run."""

from tests.helpers import wait_attempts, wait_job


def test_single_mode_drains_running_pauses_queued(env):
    env.backend()
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "4"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    ja = c.submit("p1", objective="A (drains)", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]
    jb = c.submit("p1", objective="B (waits)", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]

    wait_job(c, ja, "running")
    # single mode, default policy = drain
    out = c.set_mode("single", project_id="p1")
    assert out["mode"] == "single"
    assert out["drain_policy"] == "drain"

    # B was queued -> paused, never launched
    vb = wait_job(c, jb, "paused")
    assert wait_attempts(h, ja, 1)  # A launched before the switch
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jb,))[0]["n"] == 0

    # A runs to completion under drain (not killed)
    va = wait_job(c, ja, "succeeded")
    assert va["failure_class"] is None

    # back to team: B is NOT auto-resumed; explicit resume runs it
    c.set_mode("team", project_id="p1")
    assert wait_job(c, jb, "paused")  # still paused immediately after
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jb,))[0]["n"] == 0
    c.resume(jb)
    vb = wait_job(c, jb, "succeeded")
    assert vb["failure_class"] is None
