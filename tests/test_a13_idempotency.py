"""PRD §14 test 13 — resubmitting with the same idempotency key is a no-op:
same job id, created=False, and exactly one job/attempt ever runs."""

from tests.helpers import wait_attempts, wait_job


def test_idempotency_key_dedupes_submit(env):
    env.backend()
    h = env.daemon()
    c = h.client()
    c.set_mode("team", project_id="p1")

    common = dict(objective="dedupe me", base_commit=env.repo_head,
                  allowed_scope=["src"], profile_id="w",
                  idempotency_key="k-1")
    out1 = c.submit("p1", **common)
    out2 = c.submit("p1", **common)

    assert out1["created"] is True
    assert out2["created"] is False
    assert out1["job_id"] == out2["job_id"]
    assert h.query("SELECT count(*) AS n FROM jobs")[0]["n"] == 1

    v = wait_job(c, out1["job_id"], "succeeded")
    assert v["failure_class"] is None
    assert len(wait_attempts(h, out1["job_id"], 1)) == 1
