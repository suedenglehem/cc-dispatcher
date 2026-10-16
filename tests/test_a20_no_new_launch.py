"""PRD §14 test 20 — the no-new-launch guarantee: the authoritative mode
read happens INSIDE the launch commit transaction, so

  * M→L (mode change wins): a single-mode commit that lands before the
    launch transaction means the launch never commits — zero attempts;
  * L→M (launch wins): a launch committed before the single-mode commit
    runs to completion (drain), while later launches are blocked.

The hook runs on the daemon's event loop between prefilter and gate,
exactly where a mode commit could interleave in production.
"""

import asyncio
import threading

from tests.helpers import (read_launch_log, wait_for, wait_job)


def _install_mode_switch(handle, for_job=None, project_id="p1", when=None):
    """Install a one-shot pre_gate_hook that switches to single mode.

    `for_job`: only this job's launch triggers the switch (else the first
    eligible job would). `when` (threading.Event or None): if given, the
    hook waits for it (on an executor thread, so the loop keeps running)
    before switching.
    """
    state = {"fired": False}

    async def hook(job):
        if state["fired"] or (for_job is not None and job["id"] != for_job):
            return
        state["fired"] = True
        if when is not None:
            await asyncio.to_thread(when.wait, 15)
        await handle.scheduler.set_mode(project_id, "single", "drain")

    handle.scheduler.pre_gate_hook = hook
    return state


def test_mode_single_committed_first_blocks_launch(env):
    """M→L: the mode commit lands before the launch tx → no attempt."""
    env.backend()
    h = env.daemon()
    c = h.client()
    c.set_mode("team", project_id="p1")

    # installed before the submit: no other jobs exist, so the first
    # eligible job is ours and the switch is race-free
    _install_mode_switch(h)
    jid = c.submit("p1", objective="raced", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    v = wait_job(c, jid, "paused", timeout=15)
    assert v["status"] == "paused"

    # nothing launched: no attempt rows, no worker process ever started
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jid,))[0]["n"] == 0
    assert read_launch_log(env.root / "launch.log") == []
    row = h.query("SELECT mode FROM projects WHERE id='p1'")[0]
    assert row["mode"] == "single"


def test_launch_committed_first_drains_after_mode_change(env):
    """L→M: job A's launch commits, then single mode lands — A drains to
    success, B (still queued) is blocked by the in-tx re-read."""
    env.backend()
    go = threading.Event()
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "2"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    ja = c.submit("p1", objective="A (launches first)",
                  base_commit=env.repo_head, allowed_scope=["src"],
                  profile_id="w")["job_id"]
    jb = c.submit("p1", objective="B (races the mode change)",
                  base_commit=env.repo_head, allowed_scope=["src"],
                  profile_id="w")["job_id"]

    # switch to single only once A is observably running; B's launch then
    # hits the hook → mode commit → in-tx re-read → not_team. Safe to
    # install late: B cannot commit while A holds the only backend.
    _install_mode_switch(h, for_job=jb, when=go)
    wait_for(lambda: any(e["job"] == ja for e in
                         read_launch_log(env.root / "launch.log")
                         if e["phase"] == "start"),
             timeout=15, message="A launched")
    go.set()

    va = wait_job(c, ja, "succeeded")
    assert va["failure_class"] is None
    vb = wait_job(c, jb, "paused")
    assert vb["status"] == "paused"
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jb,))[0]["n"] == 0

    starts = [e for e in read_launch_log(env.root / "launch.log")
              if e["phase"] == "start"]
    assert [e["job"] for e in starts] == [ja], \
        "exactly A launched; B must not have started"
