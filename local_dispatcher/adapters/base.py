"""WorkerAdapter protocol: build a supervised launch, parse its stream,
classify process-level failures.

The adapter is per-profile (adapter='claude'|'cline') while the executable
is per-profile-configurable — tests run a fake worker binary through the
real claude adapter so the stream/result contract is exercised exactly as
in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkerLaunch:
    cmd: list[str]
    env: dict[str, str]
    cwd: str
    packet: str
    packet_via: str = "argv"  # "argv" (packet already in cmd) or "stdin"


@dataclass
class StreamInfo:
    """Mutable per-attempt stream state, updated line by line."""
    last_context_tokens: int | None = None
    result: dict[str, Any] | None = None
    raw_text: list[str] = field(default_factory=list)   # non-JSON lines, bounded
    num_turns: int | None = None
    session_id: str | None = None

    def raw_tail(self, n: int = 200) -> str:
        return "\n".join(self.raw_text[-n:])


class WorkerAdapter:
    name = "base"

    def build(self, *, cfg, profile_row: dict, backend_cfg, backend_row: dict,
              job: dict, attempt_no: int, worktree: str, artifact_dir: str,
              packet: str, mycelium_mcp_json: str) -> WorkerLaunch:
        raise NotImplementedError

    def parse_line(self, line: str, info: StreamInfo) -> None:
        raise NotImplementedError

    def classify(self, *, exit_code: int, info: StreamInfo,
                 ctx_limit: int | None,
                 overflow_pct: float = 0.95) -> str | None:
        """Return a failure class string, or None when the process level is
        clean (the result-file contract is validated separately).

        `overflow_pct` is the fraction of the context budget at which a
        near-full `last_context_tokens` is classified as context_overflow
        (config: `daemon.context_overflow_usage_pct`)."""
        raise NotImplementedError


def worker_base_env() -> dict[str, str]:
    """Minimal, explicit environment for workers: never inherit the primary
    session's credentials, endpoints, or dispatcher variables."""
    import os
    keep = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env
