"""Configuration: TOML at ~/.config/local-dispatcher/config.toml.

The daemon runs under systemd and does not source the user's shell, so every
value it needs (endpoints, model labels, token budgets) is explicit here.
ctx budgets follow the user's claude-ctx.sh convention: 90% of the probed
model maximum, overridable per backend.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/local-dispatcher/config.toml")
DEFAULT_STATE_ROOT = os.path.expanduser("~/.local/share/local-dispatcher")


@dataclass
class Paths:
    root: str
    worktree_root: str
    socket_path: str
    lock_path: str

    @property
    def db_path(self) -> str:
        return os.path.join(self.root, "dispatch.db")

    @property
    def artifacts_root(self) -> str:
        return os.path.join(self.root, "artifacts")

    @property
    def run_dir(self) -> str:
        return os.path.join(self.root, "run")

    def artifact_dir(self, job_id: str, attempt_no: int) -> str:
        return os.path.join(self.artifacts_root, job_id, f"attempt-{attempt_no}")

    def ensure(self) -> None:
        for d in (self.root, self.run_dir, self.artifacts_root, self.worktree_root):
            os.makedirs(d, exist_ok=True)
        os.chmod(self.run_dir, 0o700)

    def socket_owner_umask(self) -> None:
        os.chmod(self.socket_path, 0o600) if os.path.exists(self.socket_path) else None


@dataclass
class DaemonConfig:
    tick_s: float = 1.0
    probe_ttl_s: float = 30.0
    probe_timeout_s: float = 3.0
    kill_grace_s: float = 10.0
    kill_confirm_s: float = 5.0
    min_start_budget_s: float = 60.0
    attach_poll_s: float = 1.0
    events_max_rows: int = 50000
    events_max_age_days: int = 30
    response_cap_chars: int = 8000
    context_warn_pct: float = 0.65
    context_overflow_usage_pct: float = 0.95
    diff_cap_bytes: int = 40960
    log_tail_lines: int = 200


@dataclass
class WorkerConfig:
    deadline_s: int = 1800
    max_attempts: int = 2
    output_limit_chars: int = 8000
    claude_executable: str = os.path.expanduser("~/.local/bin/claude")
    cline_executable: str = "/usr/local/bin/cline"
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Bash", "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
        "mcp__mycelium__recall", "mcp__mycelium__save",
        "mcp__mycelium__context", "mcp__mycelium__recent",
    ])
    output_format: str = "stream-json"   # fallback: "json"
    prompt_via: str = "stdin"            # "argv" (positional prompt) or "stdin"
    max_context_pct: float = 0.90
    max_output_tokens: int = 32000
    autocompact_pct: int = 70
    max_mcp_output_tokens: int = 16000
    anthropic_api_key: str = "local"
    permission_mode: str = "bypassPermissions"
    overflow_patterns: list[str] = field(default_factory=lambda: [
        r"prompt is too long",
        r"context length",
        r"maximum context",
        r"too many tokens",
        r"exceeds (the )?(model )?context",
        r"context_window",
        r"range [^\n]*context",
    ])


@dataclass
class MyceliumConfig:
    command: list[str] = field(default_factory=lambda: [
        os.path.expanduser("~/.venvs/mycelium/bin/mycelium"), "serve"])
    env: dict[str, str] = field(default_factory=dict)
    call_timeout_s: float = 10.0
    max_recall_queries: int = 3
    recall_limit: int = 5
    memory_section_cap_chars: int = 4000
    # Derived from the presence of the [mycelium] table (not a TOML key):
    # absent section ⇒ no daemon-side mycelium client at all, so nothing
    # can ever spawn against the user's store by accident.
    enabled: bool = False


@dataclass
class BackendConfig:
    id: str
    endpoint: str
    openai_base_url: str = ""
    model_id: str = ""
    model_label: str = ""
    credential_env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    is_primary: bool = False
    manually_reserved: bool = False
    reserved_note: str = ""
    concurrency: int = 1
    resource_group: str = ""
    ctx_override: int = 0
    notes: str = ""


@dataclass
class ProfileConfig:
    id: str
    adapter: str = "claude"
    executable: str = ""
    args: list[str] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)
    backend_ids: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    deadline_s: int = 0
    max_attempts: int = 0
    output_limit_chars: int = 0
    allow_test_repair: bool = False


@dataclass
class ProjectConfig:
    id: str
    repo_path: str
    integration_path: str = ""
    integration_ref: str = "main"
    mycelium_project: str = ""


@dataclass
class GroupConfig:
    id: str
    max_concurrency: int = 1


@dataclass
class Config:
    paths: Paths
    daemon: DaemonConfig
    worker: WorkerConfig
    mycelium: MyceliumConfig
    backends: list[BackendConfig]
    groups: list[GroupConfig]
    profiles: list[ProfileConfig]
    projects: list[ProjectConfig]

    def backend(self, backend_id: str) -> BackendConfig | None:
        return next((b for b in self.backends if b.id == backend_id), None)

    def profile(self, profile_id: str) -> ProfileConfig | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def project(self, project_id: str) -> ProjectConfig | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def project_for_cwd(self, cwd: str) -> ProjectConfig | None:
        """Longest-prefix match of cwd over configured repo paths."""
        best = None
        for p in self.projects:
            rp = os.path.realpath(p.repo_path)
            if cwd == rp or cwd.startswith(rp + os.sep):
                if best is None or len(rp) > len(os.path.realpath(best.repo_path)):
                    best = p
        return best

    def effective_allowed_tools(self, profile: ProfileConfig) -> list[str]:
        return profile.allowed_tools or self.worker.allowed_tools

    def effective_deadline(self, profile: ProfileConfig) -> int:
        return profile.deadline_s or self.worker.deadline_s

    def effective_max_attempts(self, profile: ProfileConfig) -> int:
        return profile.max_attempts or self.worker.max_attempts

    def effective_output_limit(self, profile: ProfileConfig) -> int:
        return profile.output_limit_chars or self.worker.output_limit_chars

    def effective_executable(self, profile: ProfileConfig) -> str:
        if profile.executable:
            return profile.executable
        if profile.adapter == "cline":
            return self.worker.cline_executable
        return self.worker.claude_executable


def derive_max_context(ctx_max: int, pct: float = 0.90) -> int:
    """90%-of-detected-max rule (claude-ctx.sh convention)."""
    return int(ctx_max * pct)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH)
    raw: dict = tomllib.loads(path.read_text()) if path.exists() else {}

    state_root = os.path.expanduser(raw.get("state", {}).get("root", DEFAULT_STATE_ROOT))
    worktree_root = os.path.expanduser(
        raw.get("state", {}).get("worktree_root") or os.path.join(state_root, "worktrees"))
    socket_path = os.path.expanduser(
        raw.get("state", {}).get("socket") or os.path.join(state_root, "run", "dispatcher.sock"))
    paths = Paths(
        root=state_root,
        worktree_root=worktree_root,
        socket_path=socket_path,
        lock_path=os.path.join(state_root, "run", "dispatcher.lock"),
    )

    d = raw.get("daemon", {})
    daemon = DaemonConfig(**{k: v for k, v in d.items() if k in DaemonConfig.__dataclass_fields__})

    w = raw.get("worker", {})
    worker = WorkerConfig(**{k: v for k, v in w.items() if k in WorkerConfig.__dataclass_fields__})

    # Presence of the [mycelium] table — not a TOML key — is what enables the
    # daemon-side client. raw.get("mycelium", {}) would hand back {} when the
    # section is absent and `is not None` would then be True, silently
    # spawning a mycelium child against the user's real store in every test.
    m = raw.get("mycelium")
    mycelium = MyceliumConfig(
        **{k: v for k, v in (m or {}).items()
           if k in MyceliumConfig.__dataclass_fields__ and k != "enabled"},
        enabled=m is not None)

    def _backend(b: dict) -> BackendConfig:
        if not b.get("id") or not b.get("endpoint"):
            raise ValueError("every [[backends]] entry needs 'id' and "
                             "'endpoint'; drop the empty [[backends]] table "
                             "when it has no fields")
        return BackendConfig(
            id=b["id"], endpoint=b["endpoint"],
            openai_base_url=b.get("openai_base_url", ""),
            model_id=b.get("model_id", ""), model_label=b.get("model_label", ""),
            credential_env=b.get("credential_env", {}) or {},
            enabled=b.get("enabled", True), is_primary=b.get("is_primary", False),
            manually_reserved=b.get("manually_reserved", False),
            reserved_note=b.get("reserved_note", ""),
            concurrency=b.get("concurrency", 1),
            resource_group=b.get("resource_group", ""),
            ctx_override=b.get("ctx_override", 0), notes=b.get("notes", ""),
        )

    backends = [_backend(b) for b in raw.get("backends", [])]
    groups = []
    for g in raw.get("resource_groups", []):
        if not g.get("id"):
            raise ValueError("every [[resource_groups]] entry needs 'id'; "
                             "drop the empty table when it has no fields")
        groups.append(GroupConfig(id=g["id"],
                                  max_concurrency=g.get("max_concurrency", 1)))
    profiles = []
    for p in raw.get("profiles", []):
        if not p.get("id"):
            raise ValueError("every [[profiles]] entry needs 'id'; drop the "
                             "empty [[profiles]] table when it has no fields")
        profiles.append(ProfileConfig(
            id=p["id"], adapter=p.get("adapter", "claude"),
            executable=p.get("executable", ""), args=p.get("args", []) or [],
            env_overrides=p.get("env_overrides", {}) or {},
            backend_ids=p.get("backend_ids", []) or [],
            allowed_tools=p.get("allowed_tools", []) or [],
            deadline_s=p.get("deadline_s", 0), max_attempts=p.get("max_attempts", 0),
            output_limit_chars=p.get("output_limit_chars", 0),
            allow_test_repair=p.get("allow_test_repair", False),
        ))
    projects = []
    for p in raw.get("projects", []):
        if not p.get("id") or not p.get("repo_path"):
            raise ValueError("every [[projects]] entry needs 'id' and "
                             "'repo_path'; drop the empty [[projects]] table "
                             "when it has no fields")
        projects.append(ProjectConfig(
            id=p["id"], repo_path=os.path.expanduser(p["repo_path"]),
            integration_path=os.path.expanduser(
                p.get("integration_path", p["repo_path"])),
            integration_ref=p.get("integration_ref", "main"),
            mycelium_project=p.get("mycelium_project", ""),
        ))

    cfg = Config(paths=paths, daemon=daemon, worker=worker, mycelium=mycelium,
                 backends=backends, groups=groups, profiles=profiles, projects=projects)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    group_ids = {g.id for g in cfg.groups}
    backend_ids = {b.id for b in cfg.backends}
    for b in cfg.backends:
        if b.resource_group and b.resource_group not in group_ids:
            raise ValueError(f"backend {b.id}: unknown resource_group {b.resource_group!r}")
        if b.concurrency < 1:
            raise ValueError(f"backend {b.id}: concurrency must be >= 1")
    for p in cfg.profiles:
        if p.adapter not in ("claude", "cline"):
            raise ValueError(f"profile {p.id}: unknown adapter {p.adapter!r}")
        unknown = set(p.backend_ids) - backend_ids
        if unknown:
            raise ValueError(f"profile {p.id}: unknown backend_ids {sorted(unknown)}")
    proj_ids = {p.id for p in cfg.projects}
    if len(proj_ids) != len(cfg.projects):
        raise ValueError("duplicate project ids in config")
    if not any(b.is_primary for b in cfg.backends):
        # Not fatal: a config may run without a designated primary, but the
        # PRD topology always has one. Warn-level is handled by callers.
        pass
