"""PRD §14 test 19 — the primary backend is reserved for the coordinating
session: an explicit submit to it is rejected, and a profile whose only
candidate is the primary can never launch."""

import time

from local_dispatcher.models import ApiError, ErrorCodes
from tests.helpers import wait_for


def test_primary_backend_never_worker_scheduled(env):
    env.backend(id="prim", primary=True)
    env.backend(id="w1")
    h = env.daemon(profiles=[dict(id="w"),
                             dict(id="w-primary-only", backend_ids=["prim"])])
    c = h.client()
    c.set_mode("team", project_id="p1")

    # explicit backend_id = primary -> actionable error
    try:
        c.submit("p1", objective="x", base_commit=env.repo_head,
                 allowed_scope=["src"], profile_id="w", backend_id="prim")
        raise AssertionError("submit to the primary backend must be rejected")
    except ApiError as e:
        assert e.code == ErrorCodes.PRIMARY_BACKEND_RESERVED

    # profile whose only candidate is the primary -> never launches
    jid = c.submit("p1", objective="y", base_commit=env.repo_head,
                   allowed_scope=["src"],
                   profile_id="w-primary-only")["job_id"]
    def waiting_view():
        vv = c.job(jid)
        return vv if vv and vv["status_detail"] == "no_capacity" else None
    v = wait_for(waiting_view, timeout=10, message="job waiting on no_capacity")
    assert v["status"] == "queued"
    time.sleep(1.0)  # several more ticks
    assert h.query("SELECT count(*) AS n FROM attempts WHERE job_id=?",
                   (jid,))[0]["n"] == 0

    # the workers view marks it
    states = {w["id"]: w["state"] for w in c.workers()["workers"]}
    assert states["prim"] == "primary-reserved"
