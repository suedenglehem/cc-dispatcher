#!/usr/bin/env python3
"""Fake worker: speaks the exact argv/stdin contract the claude adapter
builds (see local_dispatcher/adapters/claude.py) and emits stream-json lines.

Behavior is selected by FAKE_WORKER_MODE plus the attempt number parsed from
DISPATCHER_ATTEMPT_ID ("...-a<n>"), so multi-attempt scenarios (overflow →
recovery, crash → retry) need no config change between attempts.

Launch log: when FAKE_LAUNCH_LOG is set, each run appends two JSON lines
(phase=start / phase=end with a monotonic clock) — the oracle for
no-overlap (test 3) and no-duplication (test 10) assertions.

Modes:
  normal                 valid result, exit 0
  hold                   sleep FAKE_HOLD_S, then normal (drain/cancel tests)
  hang                   sleep ~10min (deadline/kill tests)
  overflow               attempt 1: context-overflow error, exit 1; later: normal
  high_context           emits input_tokens=FAKE_USAGE_TOKENS, holds
                         FAKE_CTX_HOLD_S with the usage live, then normal
  crash_once             attempt 1: no checkpoint, exit 3; later: normal
  checkpoint_then_crash  attempt 1: checkpoint saved, exit 3; later: normal
  invalid_report         always: exit 0 with no result file
  tests_fail             attempt 1: outcome=failed (tests failed); later: normal
  scope_violation        completed, but writes + reports a file outside scope
  echo_packet            dumps the packet to <artifacts>/echo_packet.txt
  echo_env               dumps marker env to <artifacts>/echo_env.json
  commit_change          edits src/app.py, commits, reports the sha
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _attempt_no() -> int:
    a = os.environ.get("DISPATCHER_ATTEMPT_ID", "")
    try:
        return int(a.rsplit("-a", 1)[1])
    except (ValueError, IndexError):
        return 1


def _log_phase(phase: str) -> None:
    path = os.environ.get("FAKE_LAUNCH_LOG")
    if not path:
        return
    line = json.dumps({
        "phase": phase, "pid": os.getpid(),
        "job": os.environ.get("DISPATCHER_JOB_ID", ""),
        "attempt": os.environ.get("DISPATCHER_ATTEMPT_ID", ""),
        "ts": round(time.monotonic(), 3),
    }) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:  # O_APPEND semantics via 'a'
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _stream_lines() -> None:
    aid = os.environ.get("DISPATCHER_ATTEMPT_ID", "?-a?")
    _emit({"type": "system", "subtype": "init", "session_id": f"fake-{aid}"})
    big = os.environ.get("FAKE_USAGE_TOKENS")
    if big:
        usage = {"input_tokens": int(big)}
    else:
        usage = {"input_tokens": 120, "cache_read_input_tokens": 30}
    _emit({"type": "assistant", "message": {"usage": usage}})


def _valid_result(job_id: str, attempt_id: str, checkpoint_path: str,
                  outcome: str = "completed", changed: list[str] | None = None,
                  tests: list[dict] | None = None,
                  criteria: list[dict] | None = None,
                  commit: str | None = None,
                  summary: str = "fake worker finished") -> dict:
    return {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "outcome": outcome,
        "summary": summary,
        "changed_files": changed or [],
        "commit": commit,
        "tests": tests or [{"command": "true", "status": "passed",
                            "evidence_path": "stdout"}],
        "acceptance_criteria": criteria or [
            {"criterion": "objective achieved", "status": "met",
             "evidence": "result.json"}],
        "remaining_work": [],
        "blockers": [],
        "assumptions": [],
        "checkpoint_path": checkpoint_path or "",
        "memory_references": [],
    }


def _checkpoint(status: str = "done") -> dict:
    return {
        "status": status,
        "completed": "fake worker did its thing",
        "changed_paths": [],
        "tests": [{"command": "true", "status": "passed"}],
        "remaining": [] if status == "done" else ["finish the rest"],
        "blockers": [],
        "assumptions": [],
        "next_action": "report the result" if status == "done" else "continue",
    }


def main() -> int:
    _log_phase("start")
    try:
        return _run()
    finally:
        _log_phase("end")


def _run() -> int:
    packet = ""
    if not sys.stdin.isatty():
        packet = sys.stdin.read()

    result_path = checkpoint_path = None
    for line in packet.splitlines():
        if line.startswith("RESULT_PATH: "):
            result_path = line.split(":", 1)[1].strip()
        elif line.startswith("CHECKPOINT_PATH: "):
            checkpoint_path = line.split(":", 1)[1].strip()

    job_id = os.environ.get("DISPATCHER_JOB_ID", "j_fake")
    attempt_id = os.environ.get("DISPATCHER_ATTEMPT_ID", "j_fake-a1")
    mode = os.environ.get("FAKE_WORKER_MODE", "normal")
    n = _attempt_no()
    hold = float(os.environ.get("FAKE_HOLD_S", "0"))

    if mode == "hang":
        time.sleep(600)
        return 143

    if hold:
        time.sleep(hold)

    _stream_lines()

    if mode == "high_context":
        # hold AFTER the usage line so the live 65% warning is observable
        ctx_hold = float(os.environ.get("FAKE_CTX_HOLD_S", "0"))
        if ctx_hold:
            time.sleep(ctx_hold)

    if mode == "overflow" and n == 1:
        _emit({"type": "result", "subtype": "error_max_tokens",
               "is_error": True,
               "result": ("API Error: prompt is too long: 98000 tokens "
                          "exceed the 92768 context length")})
        return 1

    if mode in ("crash_once", "checkpoint_then_crash") and n == 1:
        if mode == "checkpoint_then_crash" and checkpoint_path:
            _atomic_write(checkpoint_path,
                          json.dumps(_checkpoint("in_progress")))
        return 3

    if mode == "invalid_report":
        _emit({"type": "result", "subtype": "success", "num_turns": 1})
        return 0  # exit 0, no result file → invalid_result, NOT success

    if mode == "tests_fail" and n == 1:
        if checkpoint_path:
            _atomic_write(checkpoint_path, json.dumps(_checkpoint()))
        if result_path:
            _atomic_write(result_path, json.dumps(_valid_result(
                job_id, attempt_id, checkpoint_path or "", outcome="failed",
                tests=[{"command": "pytest", "status": "failed",
                        "evidence_path": "test.log"}],
                summary="tests failed")))
        _emit({"type": "result", "subtype": "success", "num_turns": 1})
        return 0

    if mode == "scope_violation":
        os.makedirs("out", exist_ok=True)
        with open("out/scope.py", "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        _emit({"type": "result", "subtype": "success", "num_turns": 1})
        if result_path:
            _atomic_write(result_path, json.dumps(_valid_result(
                job_id, attempt_id, checkpoint_path or "",
                changed=["out/scope.py"], summary="done, but touched out/")))
        return 0

    if mode == "commit_change":
        with open(os.path.join("src", "app.py"), "a", encoding="utf-8") as f:
            f.write("# worker edit\n")
        subprocess.run(["git", "add", "-A"], check=False, cwd=".")
        subprocess.run(["git", "commit", "-m", "worker: fake change",
                        "--author", "fake <fake@example.com>"],
                       check=False, cwd=".")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=".",
                             capture_output=True, text=True).stdout.strip()
        _emit({"type": "result", "subtype": "success", "num_turns": 1})
        if result_path:
            _atomic_write(result_path, json.dumps(_valid_result(
                job_id, attempt_id, checkpoint_path or "",
                changed=["src/app.py"], commit=sha or None)))
        return 0

    if mode == "echo_packet":
        if result_path:
            _atomic_write(os.path.join(os.path.dirname(result_path),
                                       "echo_packet.txt"), packet)
    if mode == "echo_env":
        if result_path:
            keys = ("DISPATCHER", "DISPATCHER_JOB_ID", "DISPATCHER_ATTEMPT_ID",
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_SESSION_ID",
                    "CLAUDE_CODE_ENTRYPOINT", "PATH")
            _atomic_write(os.path.join(os.path.dirname(result_path),
                                       "echo_env.json"),
                          json.dumps({k: os.environ.get(k) for k in keys}))

    if checkpoint_path:
        _atomic_write(checkpoint_path, json.dumps(_checkpoint()))
    _emit({"type": "result", "subtype": "success", "num_turns": 1})
    if result_path:
        _atomic_write(result_path, json.dumps(
            _valid_result(job_id, attempt_id, checkpoint_path or "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
