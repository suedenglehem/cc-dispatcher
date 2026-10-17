"""Request/response schemas and the actionable error catalog (PRD §8)."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    PAUSED = "paused"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


class FailureClass(str, Enum):
    CONTEXT_OVERFLOW = "context_overflow"
    CRASH = "crash"
    BACKEND_DISCONNECT = "backend_disconnect"
    INVALID_RESULT = "invalid_result"
    TESTS_FAILED = "tests_failed"
    SCOPE_VIOLATION = "scope_violation"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    SPAWN_LOST = "spawn_lost"
    EXIT_UNKNOWN = "exit_unknown"
    ORPHANED = "orphaned"


# EXIT_UNKNOWN (re-adopted child that died without a valid report) is
# crash-like: the process is confirmed dead before finalize, so a retry in
# the same worktree cannot duplicate execution.
RETRYABLE = {FailureClass.CONTEXT_OVERFLOW, FailureClass.CRASH,
             FailureClass.BACKEND_DISCONNECT, FailureClass.EXIT_UNKNOWN}


class ErrorCodes:
    # submit
    MODE_SINGLE = "mode_single"
    PRIMARY_BACKEND_RESERVED = "primary_backend_reserved"
    PROJECT_UNKNOWN = "project_unknown"
    PROFILE_UNKNOWN = "profile_unknown"
    BASE_NOT_A_COMMIT = "base_not_a_commit"
    SCOPE_EMPTY = "scope_empty"
    DEPENDENCY_UNKNOWN = "dependency_unknown"
    DEPENDENCY_CYCLE = "dependency_cycle"
    NO_ELIGIBLE_BACKEND = "no_eligible_backend"
    # job ops
    JOB_UNKNOWN = "job_unknown"
    JOB_NOT_PAUSED = "job_not_paused"
    JOB_NOT_CANCELLABLE = "job_not_cancellable"
    RESUME_BASE_DIVERGED = "resume_base_diverged"
    RESUME_MODE_SINGLE = "resume_mode_single"
    INTEGRATE_JOB_NOT_SUCCEEDED = "integrate_job_not_succeeded"
    INTEGRATE_BAD_COMMIT = "integrate_bad_commit"
    # backends / projects
    BACKEND_UNKNOWN = "backend_unknown"
    BACKEND_PRIMARY = "backend_primary"
    PROJECT_PATH_INVALID = "project_path_invalid"
    PROJECT_EXISTS = "project_exists"
    # transport
    DAEMON_NOT_RUNNING = "daemon_not_running"
    INTERNAL = "internal"


class ApiError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.extra:
            d["detail"] = self.extra
        return d


class SubmitParams(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)
    base_commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    allowed_scope: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    contracts_text: str = ""
    decisions_text: str = ""
    exclusions_text: str = ""
    memory_queries: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    profile_id: str
    backend_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    memory_required: bool = False
    project_id: str | None = None


class SetModeParams(BaseModel):
    mode: str = Field(pattern=r"^(single|team)$")
    policy: str | None = Field(default=None, pattern=r"^(drain|cancel)$")
    project_id: str | None = None


class JobRefParams(BaseModel):
    job_id: str
    project_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    include_result: bool = False


class WorkersParams(BaseModel):
    refresh: bool = False
    project_id: str | None = None


class IntegrateParams(BaseModel):
    job_id: str
    commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    project_id: str | None = None


class ResumeParams(BaseModel):
    job_id: str
    project_id: str | None = None


class CancelParams(BaseModel):
    job_id: str
    project_id: str | None = None


class BackendSetParams(BaseModel):
    backend_id: str
    enabled: bool | None = None
    reserved: bool | None = None
    note: str = ""


class ProjectAddParams(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    repo_path: str
    integration_path: str = ""
    integration_ref: str = "main"
    mycelium_project: str = ""
