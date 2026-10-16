"""PRD §20 result-file validation, checkpoint parsing, and the
attempt-outcome decision table.

Precedence in `evaluate_attempt`:
  1. adapter process-level class (overflow/crash/disconnect) is authoritative
     when the process itself failed;
  2. missing/invalid report → invalid_result (exit 0 with no valid report is
     NOT success — PRD §14 test 17);
  3. the worker's own outcome field drives the rest;
  4. scope violations on a clean completion are flagged for review (PRD §13),
     not silently passed and not silently failed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .models import FailureClass, RETRYABLE

OUTCOMES = ("completed", "incomplete", "blocked", "failed")
TEST_STATUSES = ("passed", "failed", "not_run")
CRITERION_STATUSES = ("met", "unmet", "unknown")
CHECKPOINT_STATUSES = ("in_progress", "done", "blocked")

_SHA = set("0123456789abcdefABCDEF")


def _is_sha(v) -> bool:
    return isinstance(v, str) and 7 <= len(v) <= 64 and all(c in _SHA for c in v)


def load_result(path: str) -> tuple[dict | None, str | None]:
    """Return (parsed dict, None) or (None, error string). Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            obj = json.load(f)
    except FileNotFoundError:
        return None, "result file not found"
    except (json.JSONDecodeError, OSError) as e:
        return None, f"result file unreadable: {e}"
    if not isinstance(obj, dict):
        return None, "result file is not a JSON object"
    return obj, None


def validate_result(obj: dict, *, job_id: str, attempt_id: str) -> list[str]:
    """Schema/identifier validation. Returns a list of errors (empty = valid)."""
    errs: list[str] = []

    def need_str(key: str, allow_empty=False) -> None:
        v = obj.get(key)
        if not isinstance(v, str) or (not allow_empty and not v.strip()):
            errs.append(f"{key}: missing or not a non-empty string")

    if obj.get("job_id") != job_id:
        errs.append(f"job_id: {obj.get('job_id')!r} != {job_id!r}")
    if obj.get("attempt_id") != attempt_id:
        errs.append(f"attempt_id: {obj.get('attempt_id')!r} != {attempt_id!r}")
    if obj.get("outcome") not in OUTCOMES:
        errs.append(f"outcome: {obj.get('outcome')!r} not in {OUTCOMES}")
    need_str("summary")
    cf = obj.get("changed_files")
    if not isinstance(cf, list) or not all(isinstance(x, str) for x in cf):
        errs.append("changed_files: must be a list of strings")
    c = obj.get("commit", None)
    if c is not None and not _is_sha(c):
        errs.append(f"commit: {c!r} is not a git sha")
    tests = obj.get("tests")
    if not isinstance(tests, list):
        errs.append("tests: must be a list")
    else:
        for i, t in enumerate(tests):
            if not isinstance(t, dict) or not isinstance(t.get("command"), str):
                errs.append(f"tests[{i}].command: missing string")
            elif t.get("status") not in TEST_STATUSES:
                errs.append(f"tests[{i}].status: {t.get('status')!r} not in {TEST_STATUSES}")
            elif not isinstance(t.get("evidence_path"), str):
                errs.append(f"tests[{i}].evidence_path: missing string")
    crits = obj.get("acceptance_criteria")
    if not isinstance(crits, list):
        errs.append("acceptance_criteria: must be a list")
    else:
        for i, c in enumerate(crits):
            if not isinstance(c, dict) or not isinstance(c.get("criterion"), str):
                errs.append(f"acceptance_criteria[{i}].criterion: missing string")
            elif c.get("status") not in CRITERION_STATUSES:
                errs.append(f"acceptance_criteria[{i}].status: {c.get('status')!r} "
                            f"not in {CRITERION_STATUSES}")
            elif not isinstance(c.get("evidence"), str):
                errs.append(f"acceptance_criteria[{i}].evidence: missing string")
    for key in ("remaining_work", "blockers", "assumptions", "memory_references"):
        v = obj.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            errs.append(f"{key}: must be a list of strings")
    cp = obj.get("checkpoint_path", None)
    if cp is not None and not isinstance(cp, str):
        errs.append("checkpoint_path: must be a string")
    return errs


def parse_checkpoint(path: str) -> tuple[dict | None, str | None]:
    """Lenient checkpoint parse. JSON object → dict; anything else → None with
    an error note. A checkpoint is advisory; garbage never breaks recovery."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    if obj.get("status") not in CHECKPOINT_STATUSES:
        obj = dict(obj)
        obj["status"] = "in_progress"
    return obj, None


@dataclass
class AttemptOutcome:
    success: bool
    failure_class: str | None      # FailureClass value
    job_status: str                # succeeded | failed | blocked | queued (retry)
    status_detail: str | None
    retryable: bool


def evaluate_attempt(*, adapter_class: str | None,
                     result_obj: dict | None, result_error: str | None,
                     result_validation_errors: list[str],
                     scope_violations: list[str],
                     allow_test_repair: bool = False) -> AttemptOutcome:
    if adapter_class:
        retryable = adapter_class in RETRYABLE
        status = "queued" if retryable else "failed"
        return AttemptOutcome(False, adapter_class, status,
                              f"process {adapter_class}", retryable)
    if result_obj is None:
        return AttemptOutcome(False, FailureClass.INVALID_RESULT.value, "failed",
                              f"invalid_result: {result_error}", False)
    if result_validation_errors:
        detail = "; ".join(result_validation_errors[:5])
        if len(result_validation_errors) > 5:
            detail += f" (+{len(result_validation_errors) - 5} more)"
        return AttemptOutcome(False, FailureClass.INVALID_RESULT.value, "failed",
                              f"invalid_result: {detail}", False)

    outcome = result_obj.get("outcome")
    if outcome == "blocked":
        blockers = "; ".join(result_obj.get("blockers") or [])[:300] or "no detail"
        return AttemptOutcome(False, None, "blocked", f"worker_blocked: {blockers}", False)
    if outcome == "failed":
        tests_failed = any(isinstance(t, dict) and t.get("status") == "failed"
                           for t in (result_obj.get("tests") or []))
        fc = FailureClass.TESTS_FAILED.value if tests_failed \
            else FailureClass.CRASH.value
        retryable = fc == FailureClass.CRASH.value or \
            (fc == FailureClass.TESTS_FAILED.value and allow_test_repair)
        status = "queued" if retryable else "failed"
        return AttemptOutcome(False, fc, status, "worker_reported_failed", retryable)
    if outcome == "incomplete":
        return AttemptOutcome(False, FailureClass.CRASH.value, "queued",
                              "worker_reported_incomplete", True)
    # outcome == "completed"
    unmet = [c for c in (result_obj.get("acceptance_criteria") or [])
             if isinstance(c, dict) and c.get("status") == "unmet"]
    if unmet:
        return AttemptOutcome(False, FailureClass.CRASH.value, "queued",
                              f"{len(unmet)} acceptance criterion(ia) unmet", True)
    if scope_violations:
        # PRD §13: flag for review — the execution is complete and reported,
        # the review requirement is carried on the job record.
        return AttemptOutcome(True, FailureClass.SCOPE_VIOLATION.value,
                              "succeeded", "scope_review_required", False)
    return AttemptOutcome(True, None, "succeeded", None, False)


def build_recovery_context(*, checkpoint: dict | None, diff: str, log_tail: str,
                           diff_cap: int = 40960, log_cap: int = 8000) -> dict:
    """RECOVERY_CONTEXT for the next attempt: latest checkpoint + bounded
    diff. Without a checkpoint, recovery relies on task data + diff + bounded
    logs and is marked uncertain (PRD §11)."""
    diff = diff or ""
    return {
        "checkpoint": checkpoint,
        "diff": diff[:diff_cap],
        "diff_truncated": len(diff) > diff_cap,
        "log_tail": (log_tail or "")[-log_cap:],
        "uncertain": checkpoint is None,
    }


def format_recovery_context(ctx: dict | None) -> str | None:
    """Render the stored recovery context into the packet's section."""
    if not ctx:
        return None
    lines = []
    cp = ctx.get("checkpoint")
    if cp:
        lines.append("PREVIOUS CHECKPOINT (from attempt "
                     f"{ctx.get('from_attempt', '?')}):")
        lines.append(json.dumps(cp, indent=2)[:6000])
    else:
        lines.append("NO CHECKPOINT WAS SAVED — inspect the current worktree "
                     "state before editing; the preserved diff and log tail "
                     "below are the only evidence (uncertain).")
    if ctx.get("diff"):
        lines.append("PRESERVED DIFF vs BASE (bounded):")
        lines.append(ctx["diff"])
        if ctx.get("diff_truncated"):
            lines.append("[diff truncated]")
    if ctx.get("log_tail"):
        lines.append("PREVIOUS RUN LOG TAIL (bounded):")
        lines.append(ctx["log_tail"])
    text = "\n".join(lines)
    return text[:48000]  # hard cap: the packet itself must stay bounded
