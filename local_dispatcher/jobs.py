"""Job state operations: submit (with full validation), resume, integrate,
and the compact job view used by status responses.

Cancellation is orchestrated by the Scheduler (it owns the supervisor); the
DB half of a cancel lives there too so state and process stay consistent.
"""

from __future__ import annotations

import json
import uuid

from .db import now_iso
from .models import ApiError, ErrorCodes, SubmitParams
from .worktrees import is_ancestor, resolve_ref


def new_job_id() -> str:
    return "j_" + uuid.uuid4().hex[:10]


class Jobs:
    def __init__(self, db, cfg, paths, worker_policy_text: str):
        self.db = db
        self.cfg = cfg
        self.paths = paths
        self.worker_policy_text = worker_policy_text

    # -- submit ----------------------------------------------------------------
    async def submit(self, project_id: str, params: SubmitParams) -> tuple[str, bool]:
        """Validate and persist a job. Returns (job_id, created).
        A duplicate idempotency key returns the existing job (PRD §7.6)."""
        conn = self.db.conn
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            known = [r["id"] for r in conn.execute(
                "SELECT id FROM projects ORDER BY id")]
            raise ApiError(ErrorCodes.PROJECT_UNKNOWN,
                           f"unknown project {project_id!r}", known=known)
        project = dict(row)

        # Idempotency first: a duplicate is a no-op regardless of current mode.
        if params.idempotency_key:
            dup = conn.execute(
                "SELECT id FROM jobs WHERE project_id=? AND idempotency_key=?",
                (project_id, params.idempotency_key)).fetchone()
            if dup is not None:
                self.db.event("job", dup["id"], "submit_duplicate_returned",
                              {"key": params.idempotency_key})
                return dup["id"], False

        if project["mode"] != "team":
            raise ApiError(ErrorCodes.MODE_SINGLE,
                           f"project {project_id} is in single mode; submissions "
                           "are disabled. Set team mode (dispatch_set_mode / "
                           "ladd mode <project> team) and retry.")

        profile_id = params.profile_id
        prof = self.cfg.profile(profile_id)
        if prof is None:
            known = [p.id for p in self.cfg.profiles]
            raise ApiError(ErrorCodes.PROFILE_UNKNOWN,
                           f"unknown profile {profile_id!r}", known=known)

        if params.backend_id:
            b = self.cfg.backend(params.backend_id)
            if b is None:
                known = [x.id for x in self.cfg.backends]
                raise ApiError(ErrorCodes.BACKEND_UNKNOWN,
                               f"unknown backend {params.backend_id!r}", known=known)
            if b.is_primary:
                raise ApiError(ErrorCodes.PRIMARY_BACKEND_RESERVED,
                               "the primary backend is reserved for the "
                               "coordinating session and never receives worker "
                               "jobs (PRD §7.7)")

        if not params.allowed_scope:
            raise ApiError(ErrorCodes.SCOPE_EMPTY, "allowed_scope must be non-empty")

        # Dependencies: must exist in the same project and form no cycle.
        deps: list[str] = []
        for d in params.dependencies:
            drow = conn.execute(
                "SELECT id FROM jobs WHERE project_id=? AND id=?",
                (project_id, d)).fetchone()
            if drow is None:
                raise ApiError(ErrorCodes.DEPENDENCY_UNKNOWN,
                               f"dependency {d!r} is not a job of project {project_id}")
            deps.append(d)
        if self._has_cycle(project_id, deps, conn):
            raise ApiError(ErrorCodes.DEPENDENCY_CYCLE,
                           "dependencies form a cycle; each dependency must "
                           "point to pre-existing jobs")

        # Base must be a real commit in the repo; store the full sha.
        full = await resolve_ref(project["repo_path"], params.base_commit)
        if full is None:
            raise ApiError(ErrorCodes.BASE_NOT_A_COMMIT,
                           f"{params.base_commit} is not a commit in "
                           f"{project['repo_path']}")

        mycelium_project = project.get("mycelium_project") or ""
        job_id = new_job_id()
        now = now_iso()
        conn.execute(
            """INSERT INTO jobs (id, project_id, idempotency_key, objective,
                   acceptance_criteria, base_commit, allowed_scope, relevant_files,
                   contracts_text, decisions_text, exclusions_text, worker_policy,
                   memory_queries, dependencies, profile_id, backend_id,
                   memory_required, mycelium_project, status, status_detail,
                   created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, project_id, params.idempotency_key, params.objective,
             json.dumps(params.acceptance_criteria), full,
             json.dumps(params.allowed_scope), json.dumps(params.relevant_files),
             params.contracts_text, params.decisions_text, params.exclusions_text,
             self.worker_policy_text, json.dumps(params.memory_queries),
             json.dumps(deps), profile_id, params.backend_id,
             1 if params.memory_required else 0, mycelium_project,
             "queued", None, now, now))
        self.db.event("job", job_id, "submitted", {
            "objective": params.objective[:200], "base": full,
            "profile": profile_id, "backend": params.backend_id,
        })
        return job_id, True

    @staticmethod
    def _has_cycle(project_id: str, roots: list[str], conn) -> bool:
        """Walk the dependency graph from the new job's roots; a cycle means
        some predecessor depends (transitively) on one of the roots' chains
        back to itself. Deps point at pre-existing jobs, so this is cheap."""
        seen: set[str] = set()
        frontier = list(roots)
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            row = conn.execute(
                "SELECT dependencies FROM jobs WHERE project_id=? AND id=?",
                (project_id, cur)).fetchone()
            if row is None:
                continue
            frontier.extend(json.loads(row["dependencies"] or "[]"))
        # A cycle among *existing* jobs would already have been rejected when
        # its last member was submitted; here we only guard the new job, which
        # by construction has no outgoing edge yet. The walk above therefore
        # cannot loop, and the real check is: no root depends on itself.
        for r in roots:
            row = conn.execute(
                "SELECT dependencies FROM jobs WHERE project_id=? AND id=?",
                (project_id, r)).fetchone()
            if row and r in json.loads(row["dependencies"] or "[]"):
                return True
        return False

    # -- resume ------------------------------------------------------------------
    async def resume(self, job_id: str, project_id: str | None = None) -> dict:
        """Explicitly resume a paused (or restart-interrupted) job. Team mode
        is required and the base is re-validated against the integration ref
        (PRD §7.8: team mode never auto-resumes)."""
        conn = self.db.conn
        job = self._job(conn, job_id, project_id)
        if job["status"] not in ("paused", "interrupted"):
            raise ApiError(ErrorCodes.JOB_NOT_PAUSED,
                           f"job {job_id} is {job['status']}; only paused or "
                           "interrupted jobs can be resumed")
        project = dict(conn.execute(
            "SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone())
        if project["mode"] != "team":
            raise ApiError(ErrorCodes.RESUME_MODE_SINGLE,
                           f"project {project['id']} is in single mode; switch it "
                           "to team mode before resuming")
        full = await resolve_ref(project["repo_path"], job["base_commit"])
        if full is None:
            raise ApiError(ErrorCodes.RESUME_BASE_DIVERGED,
                           "the job's base commit no longer exists in the repo; "
                           "submit a revised job from the current base")
        if project.get("integration_ref"):
            head = await resolve_ref(project["repo_path"], project["integration_ref"])
            if head is not None and full != head \
                    and not await is_ancestor(project["repo_path"], full, head):
                raise ApiError(ErrorCodes.RESUME_BASE_DIVERGED,
                               "the job's base is no longer an ancestor of "
                               f"{project['integration_ref']} (base diverged); "
                               "submit a revised job from the current integration "
                               "commit")
        self.db.event("job", job_id, "resumed", {"base": full})
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET status='queued', status_detail=NULL, "
                      "updated_at=? WHERE id=? AND status IN ('paused','interrupted')",
                      (now_iso(), job_id))
        return self.job_view(job_id)

    # -- integrate -----------------------------------------------------------------
    async def integrate(self, job_id: str, commit: str,
                        project_id: str | None = None) -> dict:
        """Record that the primary merged the worker's work: sets
        integrated_commit. Required before dependent jobs may launch (PRD §7.9)."""
        conn = self.db.conn
        job = self._job(conn, job_id, project_id)
        if job["status"] != "succeeded":
            raise ApiError(ErrorCodes.INTEGRATE_JOB_NOT_SUCCEEDED,
                           f"job {job_id} is {job['status']}; only succeeded jobs "
                           "can be integrated")
        project = dict(conn.execute(
            "SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone())
        full = await resolve_ref(project["repo_path"], commit)
        if full is None:
            raise ApiError(ErrorCodes.INTEGRATE_BAD_COMMIT,
                           f"{commit} is not a commit in {project['repo_path']}")
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET integrated_commit=?, "
                      "integration_disposition='accepted', updated_at=? WHERE id=?",
                      (full, now_iso(), job_id))
        self.db.event("job", job_id, "integrated", {"commit": full})
        return self.job_view(job_id)

    def promote(self, job_id: str, project_id: str | None = None) -> dict:
        """Pre-checks for the `ladd promote` API call (PRD §12): the job
        must be succeeded AND integrated — promotion records the accepted
        decision, so the primary's merge must already be on record. Returns
        the fields the daemon needs to write the [decision] memory."""
        conn = self.db.conn
        job = self._job(conn, job_id, project_id)
        if job["status"] != "succeeded":
            raise ApiError(ErrorCodes.INTEGRATE_JOB_NOT_SUCCEEDED,
                           f"job {job_id} is {job['status']}; only succeeded "
                           "jobs can be promoted")
        if not job["integrated_commit"]:
            raise ApiError(ErrorCodes.PROMOTE_NOT_INTEGRATED,
                           f"job {job_id} has no integrated_commit; run "
                           f"`ladd integrate {job_id} --commit <sha>` first "
                           "(the primary is the integration authority)")
        project = dict(conn.execute(
            "SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone())
        summary = ""
        try:
            summary = str(json.loads(job["result_json"] or "{}").get("summary") or "")
        except json.JSONDecodeError:
            pass
        return {
            "job_id": job_id,
            "integrated_commit": job["integrated_commit"],
            "base_commit": job["base_commit"],
            "summary": summary[:200],
            "mycelium_project": (job["mycelium_project"]
                                 or project["mycelium_project"]
                                 or project["id"]),
        }

    # -- views ---------------------------------------------------------------------
    @staticmethod
    def _job(conn, job_id: str, project_id: str | None) -> dict:
        q = "SELECT * FROM jobs WHERE id=?"
        args: tuple = (job_id,)
        if project_id:
            q += " AND project_id=?"
            args = (job_id, project_id)
        row = conn.execute(q, args).fetchone()
        if row is None:
            raise ApiError(ErrorCodes.JOB_UNKNOWN, f"unknown job {job_id!r}")
        return dict(row)

    def job_view(self, job_id: str, *, include_result: bool = False) -> dict:
        conn = self.db.conn
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ApiError(ErrorCodes.JOB_UNKNOWN, f"unknown job {job_id!r}")
        j = dict(row)
        view = {
            "id": j["id"],
            "project_id": j["project_id"],
            "objective": j["objective"],
            "status": j["status"],
            "status_detail": j["status_detail"],
            "profile_id": j["profile_id"],
            "backend_id": j["backend_id"],
            "base_commit": j["base_commit"],
            "attempt_count": j["attempt_count"],
            "created_at": j["created_at"],
            "started_at": j["started_at"],
            "finished_at": j["finished_at"],
            "deadline_expires_at": j["deadline_expires_at"],
            "failure_class": j["failure_class"],
            "integrated_commit": j["integrated_commit"],
            "integration_disposition": j["integration_disposition"],
            "scope_violations": json.loads(j["scope_violations"] or "[]"),
            "artifacts": self.paths.artifact_dir(job_id, max(j["attempt_count"], 1)),
        }
        attempts = conn.execute(
            "SELECT id, attempt_no, status, backend_id, exit_code, failure_class, "
            "started_at, ended_at, worktree_path, branch, usage_json FROM attempts "
            "WHERE job_id=? ORDER BY attempt_no", (job_id,)).fetchall()
        view["attempts"] = [dict(a) for a in attempts]
        for a in view["attempts"]:
            raw = a.pop("usage_json", None)
            a["usage"] = json.loads(raw) if raw else None
        if include_result:
            res = j["result_json"]
            view["result"] = json.loads(res) if res else None
            view["result_truncated"] = bool(res and len(res) > 4000)
            if view["result_truncated"]:
                r = view["result"]
                keep = {k: r.get(k) for k in
                        ("job_id", "attempt_id", "outcome", "summary", "commit")}
                keep["acceptance_criteria"] = r.get("acceptance_criteria")
                keep["blockers"] = r.get("blockers")
                keep["remaining_work"] = r.get("remaining_work")
                keep["truncated_note"] = (
                    "full result at "
                    f"{self.paths.artifact_dir(job_id, max(j['attempt_count'], 1))}/result.json")
                view["result"] = keep
        return view
