"""P3 step 1 (non-acceptance) — the live 65% context warning.

An in-flight attempt whose streamed usage crosses daemon.context_warn_pct
(0.65 of the derived working limit) must:
  * surface context_pct/context_warning in the job view while running,
  * emit exactly ONE context_warning event (deduped per attempt),
  * end up in the durable usage_json with the same evidence,
  * not fail the attempt — the warning is advisory.
Below the threshold: no warning at all."""

import json

import pytest

from tests.helpers import wait_attempts, wait_for, wait_job

# test backend ctx_max=32000 -> derived working limit int(32000*0.90)=28800
CTX_MAX = 28800
TOKENS = 20000  # 20000/28800 ~= 0.694 >= 0.65


def test_context_warning_live_view_and_one_shot_event(env):
    env.backend()
    h = env.daemon(worker_mode="high_context", profiles=[
        dict(id="w", env_overrides={
            "FAKE_USAGE_TOKENS": str(TOKENS),
            "FAKE_CTX_HOLD_S": "2"})])  # hold AFTER the usage line
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="context warning",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]

    a1 = wait_attempts(h, jid, 1)[0]

    # while the attempt is alive holding the high usage, the job view
    # decorates the running attempt with the live measurement
    def _live():
        att = c.job(jid)["attempts"]
        return att[0] if att and att[0].get("context_warning") is True else None
    live = wait_for(_live, timeout=10,
                    message="live context_warning in job view")
    assert live["context_pct"] == pytest.approx(TOKENS / CTX_MAX, abs=0.001)
    assert live["last_context_tokens"] == TOKENS
    assert live["ctx_max"] == CTX_MAX

    # the tick emits a context_warning event for the attempt
    def _warn():
        return h.query(
            "SELECT detail_json FROM events "
            "WHERE subject_id=? AND event='context_warning'", (a1["id"],))
    rows = wait_for(_warn, timeout=10, message="context_warning event")
    detail = json.loads(rows[0]["detail_json"])
    assert detail["context_pct"] >= 0.65
    assert detail["last_context_tokens"] == TOKENS
    assert detail["ctx_max"] == CTX_MAX

    # the warning is advisory: the attempt finishes normally, and the
    # event is one-shot (not re-emitted on every tick)
    v = wait_job(c, jid, "succeeded")
    assert v["attempts"][0]["failure_class"] is None
    n = h.query(
        "SELECT COUNT(*) c FROM events WHERE event='context_warning'")
    assert n[0]["c"] == 1

    # the durable usage record carries the same evidence, surfaced as `usage`
    att = wait_attempts(h, jid, 1)[0]
    usage = json.loads(att["usage_json"])
    assert usage["last_context_tokens"] == TOKENS
    assert usage["ctx_limit"] == CTX_MAX
    assert usage["context_pct"] == pytest.approx(TOKENS / CTX_MAX, abs=0.001)
    assert v["attempts"][0]["usage"]["last_context_tokens"] == TOKENS


def test_no_warning_below_threshold(env):
    # default fake usage is 150 tokens: ~0.5% of the working limit
    env.backend()
    h = env.daemon(worker_mode="normal")
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="no warning",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]
    v = wait_job(c, jid, "succeeded")
    assert v["status"] == "succeeded"
    assert h.query("SELECT 1 FROM events WHERE event='context_warning'") == []
    assert v["attempts"][0]["usage"]["context_pct"] is not None
    assert v["attempts"][0]["usage"]["context_pct"] < 0.65
