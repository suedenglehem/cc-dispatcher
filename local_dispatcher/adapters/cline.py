"""Cline worker adapter (CLI 3.0.56 — headless interface verified via --help).

Status: CONSTRUCTED, NOT SMOKE-VERIFIED. Cline's stored provider config
(openai-compatible → :8080/v1) references a model id that does not match the
currently served model, so the real smoke test is expected to fail until the
user updates Cline's model entry. Per PRD §9 we report that clearly rather
than fake support; the backend stays usable for manual work meanwhile.
"""

from __future__ import annotations

import json

from ..models import FailureClass
from .base import StreamInfo, WorkerAdapter, WorkerLaunch, worker_base_env


class ClineAdapter(WorkerAdapter):
    name = "cline"
    smoke_verified = False  # flipped by docs/cline-status.md evidence, not by code

    def build(self, *, cfg, profile_row: dict, backend_cfg, backend_row: dict,
              job: dict, attempt_no: int, worktree: str, artifact_dir: str,
              packet: str, mycelium_mcp_json: str) -> WorkerLaunch:
        w = cfg.worker
        exe = profile_row["executable"]
        cmd = [
            exe, "-p",
            "--json",
            "--cwd", worktree,
            "--auto-approve", "false",
            "--retries", "1",
            "--thinking", "low",
            "--compaction", "basic",
            "--provider", "openai-compatible",
            "--model", backend_cfg.model_id,
        ]
        cmd += list(json.loads(profile_row.get("args") or "[]"))
        cmd.append(packet)

        env = worker_base_env()
        env["OPENAI_API_BASE"] = backend_cfg.openai_base_url or backend_cfg.endpoint + "/v1"
        env["OPENAI_API_KEY"] = backend_cfg.credential_env.get(
            "OPENAI_API_KEY", w.anthropic_api_key)
        env.update({
            "DISPATCHER": "1",
            "DISPATCHER_JOB_ID": job["id"],
            "DISPATCHER_ATTEMPT_ID": job["id"] + f"-a{attempt_no}",
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
            # Cline --json shape varies by version; no stable usage channel
            # → context tracking stays "unknown" (PRD: never fabricate).
            if obj.get("type") in ("done", "final", "result") and not info.result:
                info.result = obj

    def classify(self, *, exit_code: int, info: StreamInfo,
                 ctx_limit: int | None) -> str | None:
        if exit_code == 0 and not (info.result or {}).get("is_error"):
            return None
        text = "\n".join(info.raw_text[-50:]).lower()
        for pat in ("context", "token limit", "exceeds"):
            if "exceed" in text or "token limit" in text:
                return FailureClass.CONTEXT_OVERFLOW.value
        if any(s in text for s in ("econnrefused", "connection refused", "connect")):
            return FailureClass.BACKEND_DISCONNECT.value
        return FailureClass.CRASH.value
