"""PRD §14 test 4 — a resource group with max_concurrency 1 caps aggregate
worker count across all its backends: two backends in one group still
serialize their jobs."""

from tests.helpers import (intervals_overlap, launch_intervals, wait_job)


def test_group_max_concurrency_serializes_backends(env):
    env.backend(id="b1", resource_group="g1")
    env.backend(id="b2", resource_group="g1")
    h = env.daemon(worker_mode="hold",
                   groups=[dict(id="g1", max_concurrency=1)],
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "2"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    ja = c.submit("p1", objective="job A", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]
    jb = c.submit("p1", objective="job B", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]

    wait_job(c, ja, "succeeded")
    wait_job(c, jb, "succeeded")

    # the group reservation is visible in the DB
    rows = h.query("SELECT * FROM reservations WHERE resource_id='g1'")
    assert rows, "no group reservation was recorded"

    iv = launch_intervals(env.root / "launch.log")
    ia, ib = iv[f"{ja}-a1"], iv[f"{jb}-a1"]
    assert ia[1] is not None and ib[1] is not None
    assert not intervals_overlap(ia, ib), \
        f"group cap violated, workers overlapped: {ia} vs {ib}"
