"""PRD §14 test 5 — every configured backend (including the i7-style
extra one) appears in the workers view with an accurate state:
available / busy / reserved / disabled / primary-reserved / offline."""

from tests.helpers import wait_for, wait_job


def test_workers_view_states(env):
    env.backend(id="b1")
    env.backend(id="b2", manually_reserved=True, reserved_note="interactive")
    env.backend(id="b3", enabled=False)               # the shipped i7 shape
    env.backend(id="prim", primary=True)
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", backend_ids=["b1"],
                                   env_overrides={"FAKE_HOLD_S": "4"})])
    c = h.client()

    def states(**kw):
        return {w["id"]: w["state"] for w in c.workers(**kw)["workers"]}

    # the first probe runs right after ready; wait for it to land
    wait_for(lambda: states()["b1"] == "available",
             timeout=10, message="b1 available after first probe")
    s = states()
    assert s["prim"] == "primary-reserved"
    assert s["b2"].startswith("reserved")
    assert s["b3"] == "disabled"

    # a running worker flips its backend to busy
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="hold", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    wait_job(c, jid, "running")
    assert states()["b1"] == "busy"

    # backend goes away => offline (force re-probe); _fakes[0] is b1's fake
    env._fakes[0].stop()
    wait_for(lambda: states(refresh=True)["b1"] == "offline",
             timeout=10, message="b1 offline after fake stop")

    # the worker itself does not talk to the backend, so it still finishes
    v = wait_job(c, jid, "succeeded")
    assert v["failure_class"] is None
