"""Cline worker adapter (CLI 3.0.56 — headless interface verified via
`cline --help` on this machine, 2026-10-17).

Invocation (the task packet is the POSITIONAL prompt; a bare prompt runs
in act mode with auto-approve enabled, which is what a worker needs):

    cline --json --cwd <worktree> --auto-approve true --retries 1
          --thinking low --compaction basic -t <remaining budget>
          --data-dir <artifact_dir>/cline-data
          --provider openai-compatible --model <served id>  <packet>

Flag notes (all from `cline --help` 3.0.56):
- NO `-p`: that flag is PLAN mode (read-only planning). The first draft of
  this adapter passed it and would have spawned planning-only workers.
- `--auto-approve true`: unattended; `false` blocks on tool approval.
- `-t <s>`: Cline's own timeout = the job's remaining deadline budget
  (the supervisor's deadline watchdog is the backstop).
- The executable is the `ccline` launcher (bin/ccline, installed by
  install.sh to /usr/local/bin/ccline; default `cline_executable`). Cline
  reads the openai-compatible provider's baseUrl ONLY from the provider
  config stored in its --data-dir — env OPENAI_API_BASE/OPENAI_BASE_URL
  are NOT honored (strace-proven 2026-10-17: a fresh isolated data-dir
  dials api.openai.com), and a run in a fresh data-dir with no stored
  config fails. ccline therefore runs
  `cline auth openai-compatible -b "$OPENAI_API_BASE" -m <model> -k ...
  --data-dir <dir>` (the sanctioned baseUrl setter) into the run's own
  isolated data-dir and then execs cline with the unchanged argv. This
  adapter supplies exactly what ccline consumes: OPENAI_API_BASE /
  OPENAI_API_KEY in env, --model and --data-dir in argv.
- `--data-dir <artifact_dir>/cline-data`: per-attempt isolated state, so
  the worker never writes into the user's ~/.cline/data (where the live
  interactive session's state lives) and never needs it — the provider
  config (including the provider key) is written into the isolated
  data-dir by ccline itself. The dispatcher never reads ~/.cline
  (API-layer redaction keeps artifacts clean regardless).
- NO `--worktree`: the dispatcher owns worktree placement via `--cwd`.
- No `-s` system-prompt override: Cline's native system prompt is tuned
  to its own tool protocol; the worker policy and result contract travel
  inside the task packet instead.

Status: VERIFIED. Fake-binary e2e (tests/test_i_cline_adapter.py) plus a
REAL model-level smoke on 2026-10-17: job j_bba5405826 on tr4-secondary
(strata, swift-1.5-iq3_xxs) — ccline seeded the per-attempt baseUrl, the
run completed (finishReason "completed", 15 iterations, exit 0, valid
result.json, tests passed). Verbatim evidence in docs/cline-status.md,
per PRD §9. Known caveat: the worker created an out-of-scope .venv/ in
the worktree to run pytest, which the scope check correctly flagged
(scope_review_required) — harmless, but a profile could ship a
pre-provisioned venv or `--scope .venv` to keep the flag clean.
"""

from __future__ import annotations

import datetime
import json
import os

from ..models import FailureClass
from .base import StreamInfo, WorkerAdapter, WorkerLaunch, worker_base_env


def _timeout_s(profile_row: dict, cfg, job: dict) -> int:
    """Cline's own `-t` timeout: the job's remaining deadline budget,
    falling back to the effective profile deadline when the budget is
    unknown or nearly spent (the supervisor kill is the backstop)."""
    fallback = int(profile_row.get("deadline_s") or cfg.worker.deadline_s)
    exp = job.get("deadline_expires_at")
    if exp:
        try:
            dt = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            rem = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if rem > 30:
                return int(rem)
        except ValueError:
            pass
    return fallback


class ClineAdapter(WorkerAdapter):
    name = "cline"
    smoke_verified = True  # real pass 2026-10-17, j_bba5405826 — docs/cline-status.md

    def build(self, *, cfg, profile_row: dict, backend_cfg, backend_row: dict,
              job: dict, attempt_no: int, worktree: str, artifact_dir: str,
              packet: str, mycelium_mcp_json: str) -> WorkerLaunch:
        cmd = [
            profile_row["executable"],
            "--json",
            "--cwd", worktree,
            "--auto-approve", "true",
            "--retries", "1",
            "--thinking", "low",
            "--compaction", "basic",
            "-t", str(_timeout_s(profile_row, cfg, job)),
            "--data-dir", os.path.join(artifact_dir, "cline-data"),
            "--provider", "openai-compatible",
            "--model", backend_cfg.model_id,
        ]
        cmd += list(json.loads(profile_row.get("args") or "[]"))
        # the task packet is the positional prompt (act mode)
        cmd.append(packet)

        env = worker_base_env()
        env["OPENAI_API_BASE"] = (backend_cfg.openai_base_url
                                  or backend_cfg.endpoint + "/v1")
        env["OPENAI_API_KEY"] = backend_cfg.credential_env.get(
            "OPENAI_API_KEY", cfg.worker.anthropic_api_key)
        env.update({
            "DISPATCHER": "1",
            "DISPATCHER_JOB_ID": job["id"],
            "DISPATCHER_ATTEMPT_ID": job["id"] + f"-a{attempt_no}",
            # which instance spawned us (see adapters/claude.py)
            "DISPATCHER_STATE_DIR": cfg.paths.root,
        })
        env.update(json.loads(profile_row.get("env_overrides") or "{}"))
        return WorkerLaunch(cmd=cmd, env=env, cwd=worktree, packet=packet)

    def parse_line(self, line: str, info: StreamInfo) -> None:
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if len(info.raw_text) < 2000:
                info.raw_text.append(line[:2000])
            return
        if isinstance(obj, dict):
            text = obj.get("text") or obj.get("message") or ""
            if isinstance(text, str) and text:
                if len(info.raw_text) < 2000:
                    info.raw_text.append(text[:2000])
            # The exact --json message shape varies by Cline version; the
            # result contract is the RESULT_PATH file, not the stream, so
            # only terminal-looking objects are captured.
            if obj.get("type") in ("done", "final", "result") and not info.result:
                info.result = obj

    def classify(self, *, exit_code: int, info: StreamInfo,
                 ctx_limit: int | None,
                 overflow_pct: float = 0.95) -> str | None:
        # Cline --json has no stable usage channel, so overflow_pct never
        # applies here; the kwarg keeps the protocol uniform.
        if exit_code == 0 and not (info.result or {}).get("is_error"):
            return None
        text = "\n".join(info.raw_text[-50:]).lower()
        if "token limit" in text or "exceed" in text or "context_length" in text:
            return FailureClass.CONTEXT_OVERFLOW.value
        if any(s in text for s in ("econnrefused", "connection refused",
                                   "fetch failed", "etimedout")):
            return FailureClass.BACKEND_DISCONNECT.value
        return FailureClass.CRASH.value
