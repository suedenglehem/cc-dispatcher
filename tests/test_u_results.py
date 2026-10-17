"""Unit tests — result loading/validation, checkpoints, the attempt-outcome
decision table, and recovery-context bounding."""

import json

import pytest

from local_dispatcher.results import (
    build_recovery_context,
    evaluate_attempt,
    load_result,
    parse_checkpoint,
    validate_result,
)


def _valid_result(job_id="j1", attempt_id="j1-a1", **over) -> dict:
    obj = {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "outcome": "completed",
        "summary": "done",
        "changed_files": ["src/app.py"],
        "commit": None,
        "tests": [{"command": "pytest", "status": "passed",
                   "evidence_path": "artifacts/test.log"}],
        "acceptance_criteria": [{"criterion": "it works", "status": "met",
                                 "evidence": "test passes"}],
        "remaining_work": [],
        "blockers": [],
        "assumptions": [],
        "memory_references": [],
    }
    obj.update(over)
    return obj


# -- load_result -----------------------------------------------------------

def test_load_result_missing(tmp_path):
    obj, err = load_result(str(tmp_path / "nope.json"))
    assert obj is None and err == "result file not found"


def test_load_result_malformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{not json", encoding="utf-8")
    obj, err = load_result(str(p))
    assert obj is None and "unreadable" in err


def test_load_result_not_object(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("[1, 2]", encoding="utf-8")
    obj, err = load_result(str(p))
    assert obj is None and "not a JSON object" in err


def test_load_result_valid(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    obj, err = load_result(str(p))
    assert obj == {"a": 1} and err is None


# -- validate_result ---------------------------------------------------------

def test_validate_result_ok():
    assert validate_result(_valid_result(), job_id="j1",
                           attempt_id="j1-a1") == []


def test_validate_result_id_mismatch():
    errs = validate_result(_valid_result(job_id="other"), job_id="j1",
                           attempt_id="j1-a1")
    assert any("job_id" in e for e in errs)


def test_validate_result_bad_commit_and_statuses():
    errs = validate_result(_valid_result(commit="not-a-sha",
                                         tests=[{"command": "t",
                                                 "status": "meh",
                                                 "evidence_path": "e"}],
                                         acceptance_criteria=[
                                             {"criterion": "c", "status": "nope",
                                              "evidence": "e"}]),
                          job_id="j1", attempt_id="j1-a1")
    assert any("commit" in e for e in errs)
    assert any("tests[0].status" in e for e in errs)
    assert any("acceptance_criteria[0].status" in e for e in errs)


def test_validate_result_empty_summary_rejected():
    errs = validate_result(_valid_result(summary="   "), job_id="j1",
                           attempt_id="j1-a1")
    assert any("summary" in e for e in errs)


def test_memory_references_numeric_ids_accepted():
    # Regression: a worker cited real Mycelium ids [190, 201] as ints and was
    # rejected as "must be a list of strings" — a numeric id is a valid ref.
    errs = validate_result(_valid_result(memory_references=[190, 201]),
                           job_id="j1", attempt_id="j1-a1")
    assert errs == []


def test_memory_references_string_and_mixed_accepted():
    for mr in (["190", "201"], [190, "201"], []):
        errs = validate_result(_valid_result(memory_references=mr),
                               job_id="j1", attempt_id="j1-a1")
        assert errs == [], mr


def test_memory_references_garbage_rejected():
    for mr in ([True], [1.5], [{}, 1], ["   "], "not-a-list", [None]):
        errs = validate_result(_valid_result(memory_references=mr),
                               job_id="j1", attempt_id="j1-a1")
        assert any("memory_references" in e for e in errs), mr


def test_memory_references_absent_is_fine():
    obj = _valid_result()
    del obj["memory_references"]
    assert validate_result(obj, job_id="j1", attempt_id="j1-a1") == []


# -- parse_checkpoint --------------------------------------------------------

def test_parse_checkpoint_valid_and_defaulted(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"status": "done", "completed": "x"}),
                 encoding="utf-8")
    cp, err = parse_checkpoint(str(p))
    assert cp == {"status": "done", "completed": "x"} and err is None

    p2 = tmp_path / "c2.json"
    p2.write_text(json.dumps({"completed": "x"}), encoding="utf-8")
    cp2, _ = parse_checkpoint(str(p2))
    assert cp2["status"] == "in_progress"  # unknown/missing → in_progress


def test_parse_checkpoint_garbage_is_advisory_only(tmp_path):
    for text in ("{nope", "[1]", '"just a string"'):
        p = tmp_path / "c.json"
        p.write_text(text, encoding="utf-8")
        assert parse_checkpoint(str(p)) == (None, None)
    assert parse_checkpoint(str(tmp_path / "missing.json")) == (None, None)


# -- evaluate_attempt decision table ------------------------------------------

def _ev(result_obj=None, **kw):
    return evaluate_attempt(
        adapter_class=kw.get("adapter_class"),
        result_obj=result_obj,
        result_error=kw.get("result_error"),
        result_validation_errors=kw.get("errs", []),
        scope_violations=kw.get("scope", []),
        allow_test_repair=kw.get("allow_test_repair", False))


def test_adapter_class_authoritative_and_retryable():
    o = _ev(adapter_class="context_overflow")
    assert (o.success, o.failure_class, o.job_status, o.retryable) == \
        (False, "context_overflow", "queued", True)


def test_adapter_class_non_retryable():
    o = _ev(adapter_class="spawn_lost")
    assert (o.failure_class, o.job_status, o.retryable) == \
        ("spawn_lost", "failed", False)


def test_missing_report_is_invalid_not_success():
    o = _ev(result_error="result file not found")
    assert (o.failure_class, o.job_status, o.retryable) == \
        ("invalid_result", "failed", False)
    assert "invalid_result" in o.status_detail


def test_validation_errors_truncated_in_detail():
    # a loaded-but-invalid report: the errors list drives the detail
    o = _ev(result_obj={"job_id": "j1"}, errs=[f"e{i}" for i in range(7)])
    assert o.failure_class == "invalid_result"
    assert "e0" in o.status_detail and "e4" in o.status_detail
    assert "+2 more" in o.status_detail
    assert "e6" not in o.status_detail


def test_outcome_blocked():
    o = _ev(result_obj=_valid_result(outcome="blocked",
                                     blockers=["need a decision"]))
    assert (o.failure_class, o.job_status) == (None, "blocked")
    assert "need a decision" in o.status_detail


def test_outcome_failed_with_failed_tests():
    r = _valid_result(outcome="failed",
                      tests=[{"command": "pytest", "status": "failed",
                              "evidence_path": "t.log"}])
    o = _ev(result_obj=r)
    assert (o.failure_class, o.job_status, o.retryable) == \
        ("tests_failed", "failed", False)
    # with the profile flag, test failures become retryable
    o2 = _ev(result_obj=r, allow_test_repair=True)
    assert (o2.job_status, o2.retryable) == ("queued", True)


def test_outcome_failed_without_failed_tests_is_crash_retry():
    o = _ev(result_obj=_valid_result(outcome="failed"))
    assert (o.failure_class, o.job_status, o.retryable) == \
        ("crash", "queued", True)


def test_outcome_incomplete_retries():
    o = _ev(result_obj=_valid_result(outcome="incomplete"))
    assert (o.failure_class, o.job_status, o.retryable) == \
        ("crash", "queued", True)


def test_completed_with_unmet_criterion_retries():
    r = _valid_result(acceptance_criteria=[
        {"criterion": "a", "status": "met", "evidence": "e"},
        {"criterion": "b", "status": "unmet", "evidence": "e"}])
    o = _ev(result_obj=r)
    assert (o.failure_class, o.job_status, o.retryable) == \
        ("crash", "queued", True)
    assert "1 acceptance criterion" in o.status_detail


def test_completed_scope_violation_flagged_not_failed():
    o = _ev(result_obj=_valid_result(), scope=["out/scope.py"])
    assert (o.success, o.failure_class, o.job_status, o.status_detail) == \
        (True, "scope_violation", "succeeded", "scope_review_required")


def test_completed_clean():
    o = _ev(result_obj=_valid_result())
    assert (o.success, o.failure_class, o.job_status,
            o.status_detail, o.retryable) == \
        (True, None, "succeeded", None, False)


# -- build_recovery_context ----------------------------------------------------

def test_recovery_context_with_checkpoint_is_certain():
    ctx = build_recovery_context(checkpoint={"status": "done"},
                                 diff="diff", log_tail="log")
    assert ctx["uncertain"] is False
    assert ctx["checkpoint"] == {"status": "done"}
    assert ctx["diff_truncated"] is False


def test_recovery_context_without_checkpoint_is_uncertain():
    ctx = build_recovery_context(checkpoint=None, diff="diff", log_tail="log")
    assert ctx["uncertain"] is True
    assert ctx["checkpoint"] is None


def test_recovery_context_bounds():
    big_diff = "d" * 50000
    big_log = "x" * 20000
    ctx = build_recovery_context(checkpoint=None, diff=big_diff,
                                 log_tail=big_log)
    assert ctx["diff"] == "d" * 40960
    assert ctx["diff_truncated"] is True
    assert ctx["log_tail"] == "x" * 8000  # the LAST 8000 chars
