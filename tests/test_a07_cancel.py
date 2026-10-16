"""PRD §14 test 7 — cancelling a running job kills the whole process group,
marks the job cancelled, and releases its reservations only after the group
is confirmed dead. The worker never logs its clean 'end' phase."""

from tests.helpers import (active_reservations, read_launch_log, wait_for,
                           wait_job)


def test_cancel_running_job(env):
    env.backend()
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "30"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1", objective="long job", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    wait_job(c, jid, "running")
    assert active_reservations(h), "running job should hold reservations"

    v = c.cancel(jid)
    assert v["status"] == "cancelled"

    # attempt is finalized as cancelled (kill confirmed, reservation released)
    def _done():
        a = h.query("SELECT * FROM attempts WHERE job_id=?", (jid,))
        return a if a and a[0]["status"] == "cancelled" else None
    att = wait_for(_done, timeout=15, message="attempt finalized cancelled")
    assert att[0]["failure_class"] == "cancelled"
    wait_for(lambda: not active_reservations(h), timeout=15,
             message="reservations released after kill")

    # the worker was signalled, not given a clean exit
    log = read_launch_log(env.root / "launch.log")
    starts = [e for e in log if e["phase"] == "start" and e["job"] == jid]
    ends = [e for e in log if e["phase"] == "end" and e["job"] == jid]
    assert len(starts) == 1
    assert ends == [], "killed worker must not log a clean end phase"

    # re-cancelling is a no-op, not an error
    v2 = c.cancel(jid)
    assert v2["status"] == "cancelled"
