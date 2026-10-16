"""PRD §14 test 2 — with two free backends, two independent jobs run in
parallel (the launch-log oracle shows overlapping worker intervals)."""

from tests.helpers import launch_intervals, wait_job


def test_two_backends_run_in_parallel(env):
    env.backend()  # b1
    env.backend()  # b2
    h = env.daemon(worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "3"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    ja = c.submit("p1", objective="job A", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]
    jb = c.submit("p1", objective="job B", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w")["job_id"]

    va = wait_job(c, ja, "succeeded")
    vb = wait_job(c, jb, "succeeded")
    assert va["failure_class"] is None and vb["failure_class"] is None

    iv = launch_intervals(env.root / "launch.log")
    ia, ib = iv[f"{ja}-a1"], iv[f"{jb}-a1"]
    # both workers overlapped in time ⇒ true parallelism, not serialization
    assert ia[1] is not None and ib[1] is not None
    assert ia[0] < ib[1] and ib[0] < ia[1], \
        f"intervals did not overlap: {ia} vs {ib}"
