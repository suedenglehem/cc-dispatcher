"""PRD §14 test 3 — one backend with concurrency 1 never runs two workers
at once: the launch-log intervals of consecutive jobs are disjoint."""

from tests.helpers import (intervals_overlap, launch_intervals, wait_job)


def test_single_backend_serializes_jobs(env):
    env.backend()  # b1, default concurrency 1
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "2"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    ja = c.submit("p1", objective="job A", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]
    jb = c.submit("p1", objective="job B", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]

    wait_job(c, ja, "succeeded")
    wait_job(c, jb, "succeeded")

    iv = launch_intervals(env.root / "launch.log")
    ia, ib = iv[f"{ja}-a1"], iv[f"{jb}-a1"]
    assert ia[1] is not None and ib[1] is not None
    assert not intervals_overlap(ia, ib), \
        f"workers overlapped on one backend: {ia} vs {ib}"
    # the second job only started after the first worker had fully exited
    assert (ia[0] < ia[1] <= ib[0]) or (ib[0] < ib[1] <= ia[0])
