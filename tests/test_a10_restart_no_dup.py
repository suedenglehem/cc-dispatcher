"""Acceptance test 10 — restart does not duplicate execution.

A REAL `dispatcherd` subprocess (the in-process test daemon cannot be
SIGKILLed):

  1. daemon #1 starts, the job launches attempt 1 (a long `hold`-mode
     fake worker),
  2. daemon #1 is SIGKILLed mid-job — the worker survives (own session,
     PRD §13),
  3. daemon #2 starts on the SAME state dir: startup_reconcile must
     adopt (attach) the running attempt and never re-spawn it,
  4. the adopted worker finishes after daemon #1 is gone — its stdout
     pipe is severed, so the on-disk result file is the authority
     (exit_code=None path in _finalize) → the job succeeds on the SAME
     attempt,
  5. the attempt's reservations are released (capacity reconciled) and
     the freed slot actually accepts the next job.

Flock negative control: a second daemon on the same state dir while
#1 is alive must exit 1 (PRD §5: exactly one daemon).

The launch log (FAKE_LAUNCH_LOG) is the no-duplication oracle: exactly
one start/end pair for the attempt across both daemon lifetimes, and
the attempt row's pid is stable across the restart (adopted, not
re-spawned).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from local_dispatcher.api import ApiClient
from local_dispatcher.models import ApiError
from local_dispatcher.procfs import pid_alive, scan_marker_processes
from tests.helpers import (intervals_overlap, launch_intervals,
                           read_launch_log, wait_for, wait_job)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# worker holds 8s: alive at the kill, still alive when daemon #2 starts
# (so adoption takes the live-attach path), done well within the timeouts
HOLD_S = "8"


class RealDaemon:
    """A real `dispatcherd` subprocess on the temp state dir."""

    def __init__(self, cfg_path: Path, root: Path, log_name: str):
        self.cfg_path = cfg_path
        self.root = root
        self.log_path = root / log_name
        self.proc: subprocess.Popen | None = None

    def start(self) -> "RealDaemon":
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "local_dispatcher",
             "--config", str(self.cfg_path)],
            cwd=str(REPO), stdout=logf, stderr=subprocess.STDOUT)
        return self

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 15.0) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def client(self) -> ApiClient:
        return ApiClient(str(self.root / "run" / "dispatcher.sock"))

    def wait_ready(self, timeout: float = 30.0) -> dict:
        # serve() runs startup_reconcile BEFORE api.start(), so a working
        # socket implies reconciliation (adoption) has already happened.
        def _up():
            try:
                return self.client().status()
            except ApiError:
                return None
        return wait_for(_up, timeout, 0.1, "real daemon ready")


def db_query(root: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Fresh WAL reader on the daemon's DB (same pattern as the
    in-process handle's query())."""
    conn = sqlite3.connect(str(root / "dispatch.db"), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_restart_adopts_without_duplicate(env):
    env.backend()  # b1: fake backend, concurrency 1
    root = env.root
    launch_log = root / "launch.log"
    ov = {"FAKE_LAUNCH_LOG": str(launch_log)}
    profiles = [
        dict(id="w", adapter="claude", backend_ids=["b1"], deadline_s=300,
             max_attempts=2,
             env_overrides={**ov, "FAKE_WORKER_MODE": "hold",
                            "FAKE_HOLD_S": HOLD_S}),
        dict(id="w2", adapter="claude", backend_ids=["b1"], deadline_s=300,
             max_attempts=2,
             env_overrides={**ov, "FAKE_WORKER_MODE": "normal"}),
    ]
    cfg_path = env._write_toml(env.backends, [], profiles, [env.project()])

    daemons: list[RealDaemon] = []
    try:
        # -- daemon #1: launch the job --------------------------------------------
        d1 = RealDaemon(cfg_path, root, "daemon1.log").start()
        daemons.append(d1)
        c1 = d1.client()
        d1.wait_ready()
        c1.set_mode("team", project_id="p1")
        jid = c1.submit("p1", objective="restart must not duplicate",
                        base_commit=env.repo_head, allowed_scope=["src"],
                        profile_id="w")["job_id"]

        rows = wait_for(lambda: db_query(root,
                      "SELECT * FROM attempts WHERE job_id=? "
                      "AND status='running' AND pid IS NOT NULL", (jid,)),
                        timeout=20, message="attempt 1 running under daemon #1")
        att = rows[0]
        wpid = att["pid"]

        # flock negative control: a second daemon on the same state dir
        # must fail fast (PRD §5: exactly one daemon)
        dup = RealDaemon(cfg_path, root, "daemon_dup.log").start()
        daemons.append(dup)
        rc = dup.proc.wait(timeout=15)
        assert rc == 1, "second daemon must exit 1 while #1 holds the flock"

        # -- SIGKILL daemon #1 mid-job; the worker survives (own session) ----------
        d1.stop(signal.SIGKILL)
        time.sleep(0.5)
        assert pid_alive(wpid), \
            "worker must outlive its daemon (start_new_session)"

        # -- daemon #2 on the same state dir: startup_reconcile adopts -------------
        d2 = RealDaemon(cfg_path, root, "daemon2.log").start()
        daemons.append(d2)
        d2.wait_ready()

        # adopted, not re-spawned: the SAME pid, still running, and the
        # re_adopted event recorded
        rows = db_query(root, "SELECT * FROM events WHERE subject_id=? "
                        "AND event='re_adopted'", (att["id"],))
        assert rows, "re_adopted event missing — the attempt was not adopted"
        att2 = db_query(root, "SELECT * FROM attempts WHERE id=?",
                        (att["id"],))[0]
        assert att2["status"] == "running"
        assert att2["pid"] == wpid, "adoption must track the SAME process"
        # (asserted per-attempt, not as the global marker set: other
        # dispatcher instances on this box are out of scope here)
        markers = scan_marker_processes()
        assert markers.get(att["id"], {}).get("pid") == wpid

        # -- the adopted worker finishes after daemon #1 is gone -------------------
        c2 = d2.client()
        v = wait_job(c2, jid, "succeeded", timeout=30)
        assert v["attempt_count"] == 1, \
            "no second attempt: the adopted one finished"
        assert v["failure_class"] is None

        # no duplicate execution: exactly one start/end pair for the
        # attempt across BOTH daemon lifetimes, on the original pid
        log = read_launch_log(launch_log)
        starts = [e for e in log if e["phase"] == "start"]
        ends = [e for e in log if e["phase"] == "end"]
        assert len(starts) == 1 and starts[0]["attempt"] == att["id"]
        assert len(ends) == 1 and ends[0]["attempt"] == att["id"]
        assert starts[0]["pid"] == wpid

        # capacity reconciled: the attempt's reservations are released
        res = db_query(root, "SELECT * FROM reservations WHERE attempt_id=?",
                       (att["id"],))
        assert res, "reservation rows missing"
        assert all(r["status"] == "released" and r["released_at"]
                   for r in res), res

        # and the freed slot is actually usable: a second job launches and
        # succeeds without overlapping the first worker's interval
        j2 = c2.submit("p1", objective="capacity reconciled",
                       base_commit=env.repo_head, allowed_scope=["src"],
                       profile_id="w2")["job_id"]
        v2 = wait_job(c2, j2, "succeeded", timeout=30)
        assert v2["attempt_count"] == 1
        iv = launch_intervals(launch_log)
        assert not intervals_overlap(iv[f"{jid}-a1"], iv[f"{j2}-a1"])
    finally:
        for d in daemons:
            d.stop()
