"""Integration (P3 step 5): the startup orphan-kill is scoped to the
instance's OWN state dir — two instances must not kill each other's
workers.

Two REAL `dispatcherd` processes on DIFFERENT state dirs (separate DB,
flock, socket, worktree root), sharing one repo and one fake backend:

  1. instance A launches a `hold`-mode worker (attempt running),
  2. instance B starts: its startup_reconcile sees A's marker process in
     the GLOBAL /proc scan, but the marker's DISPATCHER_STATE_DIR tag
     names A's state dir — B must record `foreign_marker_ignored` and
     never kill it (pre-fix behaviour killed any unmatched marker),
  3. A's worker survives past B's kill grace, and A's job still
     succeeds — B never touched A's state.

(Real subprocesses because the decision happens in startup_reconcile,
which only runs in daemon startup; same pattern as test 10.)
"""

from __future__ import annotations

import json
import time

from local_dispatcher.procfs import pid_alive
from tests.helpers import wait_for, wait_job
from tests.test_a10_restart_no_dup import RealDaemon, db_query

# the worker must outlive B's startup AND B's kill_grace_s (1.0s in the
# test TOML): hold well past both
HOLD_S = "12"


def test_foreign_instance_worker_survives(env):
    env.backend()  # b1: fake backend, concurrency 1
    root_b = env.tmp_path / "state-b"
    profiles_a = [dict(id="w", adapter="claude", backend_ids=["b1"],
                       deadline_s=300, max_attempts=1,
                       env_overrides={
                           "FAKE_WORKER_MODE": "hold",
                           "FAKE_HOLD_S": HOLD_S,
                           "FAKE_LAUNCH_LOG": str(env.root / "launch-a.log")})]
    profiles_b = [dict(id="w", adapter="claude", backend_ids=["b1"],
                       deadline_s=300, max_attempts=1,
                       env_overrides={
                           "FAKE_WORKER_MODE": "normal",
                           "FAKE_LAUNCH_LOG": str(root_b / "launch-b.log")})]
    cfg_a = env._write_toml(env.backends, [], profiles_a, [env.project()])
    cfg_b = env._write_toml(env.backends, [], profiles_b, [env.project()],
                            root=root_b)

    daemons: list[RealDaemon] = []
    try:
        # -- instance A: launch the job -----------------------------------------
        a = RealDaemon(cfg_a, env.root, "daemon-a.log").start()
        daemons.append(a)
        ca = a.client()
        a.wait_ready()
        ca.set_mode("team", project_id="p1")
        jid = ca.submit("p1",
                        objective="a foreign instance must not kill my worker",
                        base_commit=env.repo_head, allowed_scope=["src"],
                        profile_id="w")["job_id"]
        rows = wait_for(lambda: db_query(env.root,
                      "SELECT * FROM attempts WHERE job_id=? "
                      "AND status='running' AND pid IS NOT NULL", (jid,)),
                        timeout=20, message="attempt running under A")
        att = rows[0]
        wpid = att["pid"]

        # -- instance B starts on a DIFFERENT state dir -------------------------
        # serve() runs startup_reconcile BEFORE the socket comes up, so a
        # ready B has already seen (and, pre-fix, killed) A's worker.
        b = RealDaemon(cfg_b, root_b, "daemon-b.log").start()
        daemons.append(b)
        b.wait_ready()

        assert pid_alive(wpid), "instance B must not kill A's worker"
        # and not kill it late either — well past B's kill grace window
        time.sleep(1.5)
        assert pid_alive(wpid), "no delayed kill after B's grace window"

        # B saw the foreign marker and left it: observability, not silence
        ev = db_query(root_b, "SELECT * FROM events WHERE subject_id=?",
                      (att["id"],))
        foreign = [json.loads(e["detail_json"])
                   for e in ev if e["event"] == "foreign_marker_ignored"]
        assert foreign, "foreign_marker_ignored event missing"
        assert all(d["state_dir"] == str(env.root) for d in foreign), foreign
        assert not any(e["event"] == "orphan_killed"
                       for e in db_query(root_b, "SELECT event FROM events"))
        # B launched nothing of its own (it has no jobs)
        assert not (root_b / "launch-b.log").exists()

        # A is undisturbed: its worker finishes and the job succeeds
        v = wait_job(ca, jid, "succeeded", timeout=30)
        assert v["attempt_count"] == 1
        assert v["failure_class"] is None
    finally:
        for d in daemons:
            d.stop()
