"""Claude Code worker adapter (CLI 2.1.301 — flags verified via --help).

The task packet is passed as the positional prompt argument (no shell is
involved — exec with an argv list — so multi-KB packets need no escaping).
stream-json output gives per-turn usage for the 65% context warning and
overflow classification; plain json is the fallback.

Bounded permission policy (PRD §9): bypassPermissions (the user's own
runner-script convention for unattended local work) + --permission-prompts
none (nothing may hang) + a restricted --allowedTools allowlist that grants
only the tools the worker needs plus read/write mycelium tools — never
dispatcher tools (enforced by --strict-mcp-config listing mycelium only).
"""

from __future__ import annotations

import json
import re

from ..models import FailureClass
from .base import StreamInfo, WorkerAdapter, WorkerLaunch, worker_base_env


class ClaudeAdapter(WorkerAdapter):
    name = "claude"

    def build(self, *, cfg, profile_row: dict, backend_cfg, backend_row: dict,
              job: dict, attempt_no: int, worktree: str, artifact_dir: str,
              packet: str, mycelium_mcp_json: str) -> WorkerLaunch:
        import os
        w = cfg.worker
        exe = profile_row["executable"]
        ctx_limit = backend_row.get("ctx_max")
        cmd = [
            exe, "-p",
            "--output-format", w.output_format,
            # claude 2.1.301: print mode rejects stream-json without --verbose
            *(["--verbose"] if w.output_format == "stream-json" else []),
            "--permission-mode", w.permission_mode,
            "--permission-prompts", "none",
            "--mcp-config", mycelium_mcp_json,
            "--strict-mcp-config",
            "--add-dir", worktree,
            "--add-dir", artifact_dir,
            "--append-system-prompt", job.get("worker_policy", ""),
            "--no-session-persistence",
            "--model", backend_cfg.model_label or backend_cfg.model_id,
        ]
        allowed = profile_row.get("allowed_tools") or "[]"
        tools = json.loads(allowed)
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        cmd += list(json.loads(profile_row.get("args") or "[]"))
        if w.prompt_via == "stdin":
            pass  # packet goes to the process's stdin
        else:
            cmd.append(packet)

        env = worker_base_env()
        env["ANTHROPIC_BASE_URL"] = backend_cfg.endpoint
        env["ANTHROPIC_API_KEY"] = backend_cfg.credential_env.get(
            "ANTHROPIC_API_KEY", w.anthropic_api_key)
        env.update({
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(w.max_output_tokens),
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(w.autocompact_pct),
            "MAX_MCP_OUTPUT_TOKENS": str(w.max_mcp_output_tokens),
            "DISPATCHER": "1",
            "DISPATCHER_JOB_ID": job["id"],
            "DISPATCHER_ATTEMPT_ID": job["id"] + f"-a{attempt_no}",
        })
        if ctx_limit:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(ctx_limit)
        env.update(json.loads(profile_row.get("env_overrides") or "{}"))
        # Never let profile overrides re-introduce primary session vars.
        for bad in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(bad, None)
        return WorkerLaunch(cmd=cmd, env=env, cwd=worktree, packet=packet,
                            packet_via="stdin" if w.prompt_via == "stdin" else "argv")

    # -- stream parsing ----------------------------------------------------
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
        if not isinstance(obj, dict):
            return
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            info.session_id = obj.get("session_id")
        elif t == "assistant":
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            toks = (usage.get("input_tokens") or 0) + \
                   (usage.get("cache_read_input_tokens") or 0) + \
                   (usage.get("cache_creation_input_tokens") or 0)
            if toks:
                if info.last_context_tokens is None or toks > info.last_context_tokens:
                    info.last_context_tokens = toks
            if obj.get("parent_tool_use_id") is None:
                info.num_turns = (info.num_turns or 0) + 1
        elif t == "result":
            info.result = obj
            info.num_turns = obj.get("num_turns", info.num_turns)

    # -- failure classification ---------------------------------------------
    def classify(self, *, exit_code: int, info: StreamInfo,
                 ctx_limit: int | None,
                 overflow_pct: float = 0.95) -> str | None:
        result = info.result or {}
        is_error = bool(result.get("is_error")) or str(
            result.get("subtype", "")).startswith("error")
        if exit_code == 0 and not is_error:
            return None
        text_parts = [str(result.get("result", "")), str(result.get("subtype", ""))]
        text_parts += info.raw_text[-50:]
        text = "\n".join(text_parts)
        for pat in _overflow_patterns:
            if pat.search(text):
                return FailureClass.CONTEXT_OVERFLOW.value
        if ctx_limit and info.last_context_tokens is not None \
                and info.last_context_tokens >= overflow_pct * ctx_limit:
            return FailureClass.CONTEXT_OVERFLOW.value
        lowered = text.lower()
        if any(s in lowered for s in ("econnrefused", "connection refused",
                                      "connection reset", "connecttimeout",
                                      "failed to connect", "api error",
                                      "overloaded", "503", "502")):
            return FailureClass.BACKEND_DISCONNECT.value
        return FailureClass.CRASH.value


_overflow_patterns = [
    re.compile(p, re.IGNORECASE) for p in [
        r"prompt is too long",
        r"context length",
        r"maximum context",
        r"too many tokens",
        r"exceeds (the )?(model )?context",
        r"context_window",
    ]
]


def mycelium_mcp_json(cfg) -> str:
    """Isolated MCP config for workers: mycelium only, never the dispatcher."""
    m = cfg.mycelium
    return json.dumps({
        "mcpServers": {
            "mycelium": {
                "command": m.command[0],
                "args": list(m.command[1:]),
                "env": dict(m.env),
            }
        }
    })
