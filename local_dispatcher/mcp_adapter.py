"""dispatcher-mcp — stdio MCP forwarder over the daemon socket (PRD §8).

Stateless bridge between a Claude Code session and the dispatcherd socket:
each dispatch_* tool opens one Unix-socket round trip and returns the
daemon's structured result as JSON text. No in-memory state, no
subprocesses. The process environment is reduced at startup to a small
allowlist, so credentials or session state from the launching session
(ANTHROPIC_*, provider keys, shell state) never live in this process.

Socket resolution (first match wins):
  LOCAL_DISPATCHER_SOCKET  direct socket path (used by tests)
  LOCAL_DISPATCHER_CONFIG  path to a dispatcher config TOML
  ~/.config/local-dispatcher/config.toml   (default)

Project binding: an explicit project_id parameter wins; otherwise the
forwarder binds its cwd to a configured project via longest-prefix over
repo_path; otherwise the daemon falls back to the sole project or returns
an actionable error listing the known project ids.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .api import ApiError, ApiClient
from .config import load_config

# Environment this process keeps; everything else inherited from the
# launching session is dropped.
_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL",
                  "LOCAL_DISPATCHER_SOCKET", "LOCAL_DISPATCHER_CONFIG")

_config = None  # Config, lazily loaded (read-only; project binding only)
_socket_path: str | None = None


def _resolve_socket_path() -> str:
    global _socket_path
    if _socket_path is None:
        direct = os.environ.get("LOCAL_DISPATCHER_SOCKET")
        if direct:
            _socket_path = direct
        else:
            _socket_path = load_config(
                os.environ.get("LOCAL_DISPATCHER_CONFIG")).paths.socket_path
    return _socket_path


def _bind_project(explicit: str | None) -> str | None:
    """Explicit project_id, else the project containing cwd, else None
    (the daemon then falls back to the sole project / actionable error)."""
    if explicit:
        return explicit
    global _config
    if _config is None:
        _config = load_config(os.environ.get("LOCAL_DISPATCHER_CONFIG"))
    p = _config.project_for_cwd(os.getcwd())
    return p.id if p else None


def _call(fn) -> str:
    """Run one ApiClient round trip, return its result as JSON text.

    Daemon errors surface as tool errors carrying the daemon's error code
    and actionable message (plus detail, e.g. known project ids).
    """
    try:
        result = fn()
    except ApiError as e:
        d = e.to_dict()
        extra = (f" {json.dumps(d['detail'], sort_keys=True)}"
                 if "detail" in d else "")
        raise RuntimeError(f"[{d['code']}] {d['message']}{extra}") from e
    return json.dumps(result)


def build_server() -> FastMCP:
    mcp = FastMCP("dispatcher")

    @mcp.tool()
    def dispatch_status() -> str:
        """Dispatcher overview: project modes, compact job list, worker
        availability (online/offline/disabled/reserved states), active and
        quarantined reservations, schema version."""
        return _call(lambda: ApiClient(_resolve_socket_path()).status())

    @mcp.tool()
    def dispatch_set_mode(mode: Literal["single", "team"],
                          policy: Literal["drain", "cancel"] | None = None,
                          project_id: str | None = None) -> str:
        """Set a project to single or team mode. Switching to single with
        policy 'drain' (default) pauses queued jobs and lets running jobs
        finish; 'cancel' cancels running jobs too. Team mode permits new
        launches; single mode never launches new work."""
        return _call(lambda: ApiClient(_resolve_socket_path()).set_mode(
            mode, project_id=_bind_project(project_id), policy=policy))

    @mcp.tool()
    def dispatch_submit(objective: str,
                        base_commit: str,
                        profile_id: str,
                        allowed_scope: list[str],
                        acceptance_criteria: list[str] | None = None,
                        relevant_files: list[str] | None = None,
                        contracts_text: str = "",
                        decisions_text: str = "",
                        exclusions_text: str = "",
                        memory_queries: list[str] | None = None,
                        dependencies: list[str] | None = None,
                        backend_id: str | None = None,
                        idempotency_key: str | None = None,
                        memory_required: bool = False,
                        project_id: str | None = None) -> str:
        """Submit a job for a supervised worker agent; returns the job id
        immediately (the scheduler launches it when capacity and
        dependencies allow). base_commit must resolve in the project repo;
        allowed_scope lists the only path prefixes the worker may change.
        Task fields are data, never shell commands."""
        return _call(lambda: ApiClient(_resolve_socket_path()).submit(
            _bind_project(project_id),
            objective=objective,
            base_commit=base_commit,
            profile_id=profile_id,
            allowed_scope=allowed_scope,
            acceptance_criteria=acceptance_criteria or [],
            relevant_files=relevant_files or [],
            contracts_text=contracts_text,
            decisions_text=decisions_text,
            exclusions_text=exclusions_text,
            memory_queries=memory_queries or [],
            dependencies=dependencies or [],
            backend_id=backend_id,
            idempotency_key=idempotency_key,
            memory_required=memory_required))

    @mcp.tool()
    def dispatch_job(job_id: str,
                     include_result: bool = False,
                     project_id: str | None = None) -> str:
        """Bounded view of one job: status, status_detail, attempts,
        failure class, artifact references (stream.log, diff.patch,
        checkpoint.json, result.json). include_result=true also returns
        the full result JSON (worker summary, changed files, tests)."""
        return _call(lambda: ApiClient(_resolve_socket_path()).job(
            job_id, project_id=_bind_project(project_id),
            full=include_result))

    @mcp.tool()
    def dispatch_cancel(job_id: str, project_id: str | None = None) -> str:
        """Cancel a job: running attempts are killed (whole process
        group), queued attempts are paused. Worktrees, logs, and
        checkpoints are retained for review."""
        return _call(lambda: ApiClient(_resolve_socket_path()).cancel(
            job_id, project_id=_bind_project(project_id)))

    @mcp.tool()
    def dispatch_resume(job_id: str, project_id: str | None = None) -> str:
        """Resume a paused or interrupted job. The daemon re-validates the
        base commit and scope before re-queueing; a diverged base is
        reported as blocked, never silently re-run."""
        return _call(lambda: ApiClient(_resolve_socket_path()).resume(
            job_id, project_id=_bind_project(project_id)))

    @mcp.tool()
    def dispatch_workers(refresh: bool = False) -> str:
        """Worker availability: every backend with its state (online,
        offline, disabled, reserved, busy), capacity, and current
        reservations. refresh=true forces a fresh health probe of the
        non-primary backends."""
        return _call(lambda: ApiClient(_resolve_socket_path()).workers(
            refresh=refresh))

    @mcp.tool()
    def dispatch_integrate(job_id: str, commit: str,
                           project_id: str | None = None) -> str:
        """Record the commit at which the primary merged the worker's
        branch (the primary is the integration authority). Dependent jobs
        launch from this commit; the commit must resolve in the project
        repo. Call it after you have merged and reviewed the work."""
        return _call(lambda: ApiClient(_resolve_socket_path()).integrate(
            job_id, commit, project_id=_bind_project(project_id)))

    return mcp


def main() -> int:
    for k in [k for k in os.environ if k not in _ENV_ALLOWLIST]:
        del os.environ[k]
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
