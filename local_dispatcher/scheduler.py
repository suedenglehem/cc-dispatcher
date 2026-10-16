"""Scheduler: the 1s tick (probe, reap, deadlines, FIFO launches) and the
launch gate — the no-new-launch guarantee (PRD §7.4, acceptance test 20).

Launch commits and mode commits are serialized by one asyncio.Lock, and the
launch COMMIT re-reads the project's mode inside its own BEGIN IMMEDIATE
transaction. A launch therefore can never commit after a switch-to-single
has committed; launches that committed before it are governed by the
drain/cancel policy. flock guarantees exactly one such lock system exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone

from .adapters import get_adapter
from .backends import BackendRegistry
from .db import now_iso
from .models import ApiError, ErrorCodes, FailureClass
from .packets import render_packet
from .procfs import group_alive, identity_matches, kill_group, make_identity, pid_alive, scan_marker_processes
from .results import (build_recovery_context, evaluate_attempt, format_recovery_context,
                      load_result, parse_checkpoint, validate_result)
from .supervisor import Supervisor, _attempt_no_from_id
from .worktrees import capture_changes, check_scope, ensure_worktree, is_ancestor, resolve_ref

log = logging.getLogger("dispatcherd.scheduler")

# prefilter details that block a job (need a decision) vs. merely wait
_BLOCKING_DETAILS = {"dependency_failed", "base_diverged",
                     "memory_required_unavailable", "deadline_before_start"}


def _iso_add(s: str | None, seconds: int) -> str | None:
    if not s:
        return None
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _iso_to_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _remaining_s(expires_at: str | None) -> float | None:
    if not expires_at:
        return None
    return (_iso_to_dt(expires_at) - datetime.now(timezone.utc)).total_seconds()


class Scheduler:
    def __init__(self, db, cfg, paths, backends: BackendRegistry,
                 supervisor: Supervisor, jobs, mycelium):
        self.db = db
        self.cfg = cfg
        self.paths = paths
        self.backends = backends
        self.supervisor = supervisor
        self.jobs = jobs
        self.mycelium = mycelium
        self.launch_gate = asyncio.Lock()
        self._stopping = False
        self._tick_task: asyncio.Task | None = None
        self._last_prune = 0.0
        from .adapters.claude import mycelium_mcp_json
        try:
            self._mcp_json: str = mycelium_mcp_json(cfg)
        except Exception:
            self._mcp_json = "{}"

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        self._tick_task = asyncio.create_task(self._run(), name="tick")

    async def stop(self) -> None:
        """Stop the tick. Workers are intentionally left running: they carry
        the dispatcher marker env and are re-adopted on the next start."""
        self._stopping = True
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while not self._stopping:
            t0 = time.monotonic()
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            try:
                await asyncio.sleep(max(0.05, self.cfg.daemon.tick_s - (time.monotonic() - t0)))
            except asyncio.CancelledError:
                raise

    async def tick(self) -> None:
        await self._probe_due()
        await self._reap()
        await self._deadline_check()
        await self._launch_queued()
        now = time.time()
        if now - self._last_prune > 3600:
            try:
                self.db.prune_events()
            except Exception:
                log.exception("event prune failed")
            self._last_prune = now

    # -- probes ------------------------------------------------------------------
    async def _probe_due(self) -> None:
        due = []
        for b in self.cfg.backends:
            row = self.backends.health_row(b.id)
            if row is None or not row["probed_at"]:
                due.append(b.id)
                continue
            probed = _iso_to_dt(row["probed_at"])
            age = (datetime.now(timezone.utc) - probed).total_seconds()
            if age > self.cfg.daemon.probe_ttl_s:
                due.append(b.id)
        if due:
            await asyncio.gather(*(self.backends.probe(bid) for bid in due),
                                 return_exceptions=True)

    # -- reap ----------------------------------------------------------------------
    async def _reap(self) -> None:
        for child in list(self.supervisor.children.values()):
            if child.proc is not None:
                if child.proc.returncode is not None:
                    await self._finalize(child, child.proc.returncode)
            else:
                if not pid_alive(child.pid):
                    await self._finalize(child, None)

    # -- deadline ------------------------------------------------------------------
    async def _deadline_check(self) -> None:
        now = now_iso()
        rows = self.db.query(
            "SELECT * FROM jobs WHERE status IN ('starting','running') "
            "AND deadline_expires_at IS NOT NULL AND deadline_expires_at <= ?", (now,))
        for job in rows:
            if job["status_detail"] == "deadline_killing":
                continue  # kill in flight; finalize will finish it
            self.db.execute("UPDATE jobs SET status_detail='deadline_killing', "
                            "updated_at=? WHERE id=?", (now, job["id"]))
            self.db.event("job", job["id"], "deadline_exceeded", {})
            child = self.supervisor.children.get(self._current_attempt_id(job["id"]))
            if child is not None:
                await self.supervisor.kill_group(child)
            else:
                a = self._current_attempt(job["id"])
                if a and a["pgid"]:
                    kill_group(a["pgid"], signal.SIGTERM)
                    await asyncio.sleep(min(self.cfg.daemon.kill_grace_s, 5.0))
                    if group_alive(a["pgid"]):
                        kill_group(a["pgid"], signal.SIGKILL)

    # -- launches --------------------------------------------------------------------
    async def _launch_queued(self) -> None:
        rows = self.db.query(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, id")
        for job in rows:
            detail = await self._launch_prefilter(dict(job))
            if detail is not None:
                status = "blocked" if detail in _BLOCKING_DETAILS else "queued"
                self.db.execute(
                    "UPDATE jobs SET status=?, status_detail=?, updated_at=? "
                    "WHERE id=? AND status='queued' AND IFNULL(status_detail,'')<>?",
                    (status, detail, now_iso(), job["id"], detail))
                if status == "blocked":
                    self.db.event("job", job["id"], "blocked", {"detail": detail})
                continue
            res = await self._try_launch(dict(job))
            if res in ("not_team", "no_capacity", "backend_offline"):
                break  # nothing else can launch this tick
            if res == "launched":
                continue
            # spawn_failed / base_diverged: re-evaluate next tick

    async def _launch_prefilter(self, job: dict) -> str | None:
        """Waiting/blocking reason, or None when the job may attempt a launch.
        Cheap pre-filter only — the launch commit re-checks mode and capacity
        inside the transaction."""
        conn = self.db.conn
        deps = json.loads(job["dependencies"] or "[]")
        for d in deps:
            drow = conn.execute(
                "SELECT status, integrated_commit FROM jobs WHERE id=?", (d,)).fetchone()
            if drow is None:
                return "dependency_failed"
            if drow["status"] != "succeeded":
                if drow["status"] in ("failed", "cancelled", "blocked", "interrupted"):
                    return "dependency_failed"
                return "waiting_dependency"
            if not drow["integrated_commit"]:
                return "waiting_integration"
        if deps:
            project = dict(conn.execute(
                "SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone())
            head = await resolve_ref(project["repo_path"],
                                     project["integration_ref"] or "main")
            if head is None or not await is_ancestor(
                    project["repo_path"], job["base_commit"], head):
                return "base_diverged"
        if job["memory_required"] and self.mycelium is not None \
                and not self.mycelium.available:
            return "memory_required_unavailable"
        remaining = _remaining_s(job["deadline_expires_at"])
        if remaining is not None and remaining < self.cfg.daemon.min_start_budget_s:
            return "deadline_before_start"
        return None

    def _profile_row_dict(self, profile_id: str) -> dict:
        p = self.cfg.profile(profile_id)
        return {
            "id": p.id,
            "adapter": p.adapter,
            "executable": self.cfg.effective_executable(p),
            "args": p.args or "[]",
            "env_overrides": p.env_overrides or "{}",
            "allowed_tools": json.dumps(self.cfg.effective_allowed_tools(p)),
            "deadline_s": self.cfg.effective_deadline(p),
            "max_attempts": self.cfg.effective_max_attempts(p),
            "output_limit_chars": self.cfg.effective_output_limit(p),
            "allow_test_repair": 1 if p.allow_test_repair else 0,
        }

    async def _try_launch(self, job: dict) -> str:
        async with self.launch_gate:
            conn = self.db.conn
            project = dict(conn.execute(
                "SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone())
            profile_id = job["profile_id"]
            profile = self.cfg.profile(profile_id)
            if profile is None:
                self.db.execute("UPDATE jobs SET status='blocked', "
                                "status_detail='profile_unknown', updated_at=? WHERE id=?",
                                (now_iso(), job["id"]))
                return "profile_unknown"

            # 1. effective base (PRD §7.9: dependent jobs start from the
            #    current integration commit, not merely the recorded base)
            base = job["base_commit"]
            deps = json.loads(job["dependencies"] or "[]")
            if deps:
                head = await resolve_ref(project["repo_path"],
                                         project["integration_ref"] or "main")
                if head is None or not await is_ancestor(
                        project["repo_path"], base, head):
                    self.db.execute("UPDATE jobs SET status='blocked', "
                                    "status_detail='base_diverged', updated_at=? "
                                    "WHERE id=?", (now_iso(), job["id"]))
                    self.db.event("job", job["id"], "blocked", {"detail": "base_diverged"})
                    return "base_diverged"
                base = head

            # 2. worktree (idempotent; created from the resolved base)
            try:
                worktree = await ensure_worktree(
                    job["id"], project["repo_path"], self.paths.worktree_root, base)
            except Exception as e:
                self.db.execute("UPDATE jobs SET status='blocked', "
                                "status_detail=? , updated_at=? WHERE id=?",
                                (f"worktree_error: {e}"[:200], now_iso(), job["id"]))
                return "worktree_error"

            # 3. the launch commit: re-read mode, pick capacity, insert
            #    attempt + reservations, flip the job — all atomic.
            with self.db.tx(immediate=True) as c:
                mode = c.execute("SELECT mode FROM projects WHERE id=?",
                                 (project["id"],)).fetchone()
                if mode["mode"] != "team":
                    return "not_team"
                backend_id = self.backends.pick_backend(
                    json.loads(profile.backend_ids or "[]"), job.get("backend_id"))
                if backend_id is None:
                    detail = self._no_backend_detail(
                        json.loads(profile.backend_ids or "[]"), job.get("backend_id"))
                    self.db.execute(
                        "UPDATE jobs SET status_detail=?, updated_at=? WHERE id=?",
                        (detail, now_iso(), job["id"]))
                    return detail
                backend_cfg = self.cfg.backend(backend_id)
                attempt_no = job["attempt_count"] + 1
                attempt_id = f"{job['id']}-a{attempt_no}"
                now = now_iso()
                deadline_start = job["deadline_start_at"] or now
                deadline_expires = job["deadline_expires_at"] or \
                    _iso_add(deadline_start, self.cfg.effective_deadline(profile))
                remaining = _remaining_s(deadline_expires)
                if remaining is not None and remaining < self.cfg.daemon.min_start_budget_s:
                    c.execute("UPDATE jobs SET status='blocked', "
                              "status_detail='deadline_before_start', updated_at=? "
                              "WHERE id=?", (now, job["id"]))
                    self.db.event("job", job["id"], "blocked",
                                  {"detail": "deadline_before_start"})
                    return "deadline_before_start"
                c.execute(
                    """INSERT INTO attempts (id, job_id, attempt_no, status, backend_id,
                              worktree_path, branch, started_at, recovery_from,
                              recovery_context)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, job["id"], attempt_no, "starting", backend_id,
                     worktree, f"dispatch/{job['id']}", now,
                     job.get("recovery_from_attempt"), job.get("recovery_context")))
                c.execute("INSERT INTO reservations (attempt_id, resource_id, kind) "
                          "VALUES (?,?, 'backend')", (attempt_id, backend_id))
                if backend_cfg.resource_group:
                    c.execute("INSERT INTO reservations (attempt_id, resource_id, kind) "
                              "VALUES (?,?, 'group')",
                              (attempt_id, backend_cfg.resource_group))
                c.execute(
                    """UPDATE jobs SET status='starting', attempt_count=?,
                           deadline_start_at=?, deadline_expires_at=?,
                           status_detail=NULL, updated_at=?
                       WHERE id=? AND status='queued'""",
                    (attempt_no, deadline_start, deadline_expires, now, job["id"]))

            # 4. build + spawn (after the commit — the attempt row exists for
            #    reconciliation if we crash between here and a successful Popen)
            job_fresh = dict(self.db.query_one("SELECT * FROM jobs WHERE id=?",
                                               (job["id"],)))
            artifact_dir = self.paths.artifact_dir(job["id"], attempt_no)
            recovery_ctx = None
            if job_fresh.get("recovery_context"):
                try:
                    recovery_ctx = json.loads(job_fresh["recovery_context"])
                except json.JSONDecodeError:
                    recovery_ctx = None
                recovery_ctx["from_attempt"] = job_fresh.get("recovery_from_attempt")
            memory_section = None
            if self.mycelium is not None:
                memory_section = await self.mycelium.memory_section(project, job_fresh)
            packet = render_packet(
                project_row=project,
                job={**job_fresh,
                     "allowed_scope": json.loads(job_fresh["allowed_scope"] or "[]"),
                     "relevant_files": json.loads(job_fresh["relevant_files"] or "[]"),
                     "acceptance_criteria": json.loads(
                         job_fresh["acceptance_criteria"] or "[]"),
                     "memory_queries": json.loads(job_fresh["memory_queries"] or "[]")},
                attempt_no=attempt_no, worktree=worktree, base_commit=base,
                artifact_dir=artifact_dir,
                deadline_expires_at=job_fresh["deadline_expires_at"],
                deadline_remaining_s=int(_remaining_s(
                    job_fresh["deadline_expires_at"]) or 0),
                output_limit_chars=self.cfg.effective_output_limit(profile),
                memory_section=memory_section,
                recovery_context=format_recovery_context(recovery_ctx))
            backend_row = self.backends.health_row(backend_id) or {}
            adapter = get_adapter(profile.adapter)
            launch = adapter.build(
                cfg=self.cfg, profile_row=self._profile_row_dict(profile_id),
                backend_cfg=backend_cfg, backend_row=backend_row,
                job=job_fresh, attempt_no=attempt_no, worktree=worktree,
                artifact_dir=artifact_dir, packet=packet,
                mycelium_mcp_json=self._mcp_json)
            try:
                child = await self.supervisor.spawn(
                    launch=launch, attempt_id=attempt_id, job_id=job["id"],
                    profile_id=profile_id, started_at=now_iso())
            except Exception as e:
                log.exception("spawn failed for %s", attempt_id)
                with self.db.tx(immediate=True) as c:
                    c.execute("UPDATE attempts SET status='failed', "
                              "failure_class=?, ended_at=? WHERE id=?",
                              (FailureClass.SPAWN_LOST.value, now_iso(), attempt_id))
                    c.execute("UPDATE reservations SET status='released', released_at=? "
                              "WHERE attempt_id=?", (now_iso(), attempt_id))
                    c.execute("UPDATE jobs SET status='queued', "
                              "status_detail='spawn_failed', updated_at=? WHERE id=?",
                              (now_iso(), job["id"]))
                self.db.event("attempt", attempt_id, "spawn_failed", {"error": str(e)[:200]})
                return "spawn_failed"
            with self.db.tx(immediate=True) as c:
                c.execute("UPDATE attempts SET pid=?, pgid=?, start_identity=? "
                          "WHERE id=?", (child.pid, child.pgid, child.identity,
                                         attempt_id))
            self.db.event("attempt", attempt_id, "launched",
                          {"backend": backend_id, "worktree": worktree})
            return "launched"

    def _no_backend_detail(self, candidate_ids: list[str], preferred: str | None) -> str:
        """Distinguish offline from merely busy for waiting jobs."""
        ids = [preferred] if preferred and preferred in candidate_ids \
            else list(candidate_ids)
        for bid in ids:
            b = self.cfg.backend(bid)
            if b is None or b.is_primary:
                continue
            row = self.backends.health_row(bid)
            if row is None or row["health"] != "online":
                return "backend_offline"
        return "no_capacity"

    # -- finalize -------------------------------------------------------------------
    def _current_attempt_id(self, job_id: str) -> str | None:
        r = self.db.query_one(
            "SELECT id FROM attempts WHERE job_id=? AND status IN ('starting','running') "
            "ORDER BY attempt_no DESC LIMIT 1", (job_id,))
        return r["id"] if r else None

    def _current_attempt(self, job_id: str):
        r = self.db.query_one(
            "SELECT * FROM attempts WHERE job_id=? AND status IN ('starting','running') "
            "ORDER BY attempt_no DESC LIMIT 1", (job_id,))
        return dict(r) if r else None

    async def _finalize(self, child, exit_code: int | None) -> None:
        attempt_id = child.attempt_id
        job_id = child.job_id
        self.supervisor.forget(attempt_id)
        conn = self.db.conn
        attempt = dict(conn.execute("SELECT * FROM attempts WHERE id=?",
                                    (attempt_id,)).fetchone())
        job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        project = dict(conn.execute("SELECT * FROM projects WHERE id=?",
                                    (job["project_id"],)).fetchone())
        profile_id = child.profile_id
        profile = self.cfg.profile(profile_id)
        adapter = get_adapter(profile.adapter if profile else "claude")
        backend_id = attempt["backend_id"]
        backend_row = self.backends.health_row(backend_id) or {}
        ctx_limit = backend_row.get("ctx_max")

        # stream fully drained?
        if child.stream_task and not child.stream_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(child.stream_task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

        artifact_dir = self.paths.artifact_dir(job_id, _attempt_no_from_id(attempt_id))
        # capture work (tracked changes since the job's base, whether or not the
        # worker committed, plus untracked paths)
        try:
            diff, changed = await capture_changes(attempt["worktree_path"],
                                                  job["base_commit"])
        except Exception:
            log.exception("capture_changes failed for %s", attempt_id)
            diff, changed = "", []
        try:
            os.makedirs(artifact_dir, exist_ok=True)
            with open(os.path.join(artifact_dir, "diff.patch"), "w",
                      encoding="utf-8", errors="replace") as f:
                f.write(diff)
        except OSError:
            pass

        # the worker's report (raw text preserved for forensics either way)
        result_path = os.path.join(artifact_dir, "result.json")
        result_obj, result_err = load_result(result_path)
        result_validation = validate_result(result_obj, job_id=job_id,
                                            attempt_id=attempt_id) \
            if result_obj is not None else []
        scope_violations: list[str] = []
        allowed = json.loads(job["allowed_scope"] or "[]")
        scope_violations |= set(check_scope(changed, allowed))
        if result_obj:
            scope_violations |= set(check_scope(
                result_obj.get("changed_files") or [], allowed))
        scope_violations = sorted(set(scope_violations))

        # process-level class (adapter evidence)
        if exit_code is None:
            adapter_class = FailureClass.EXIT_UNKNOWN.value
        else:
            adapter_class = adapter.classify(
                exit_code=exit_code, info=child.info, ctx_limit=ctx_limit)

        # reservation release is gated on confirmed group termination
        group_ok = not group_alive(child.pgid)

        cancelled = job["status"] == "cancelled"
        deadline = job["status_detail"] == "deadline_killing"
        outcome = None
        if not cancelled and not deadline:
            outcome = evaluate_attempt(
                adapter_class=adapter_class, result_obj=result_obj,
                result_error=result_err, result_validation_errors=result_validation,
                scope_violations=scope_violations,
                allow_test_repair=bool(profile and profile.allow_test_repair))

        usage = {"last_context_tokens": child.info.last_context_tokens,
                 "num_turns": child.info.num_turns, "session_id": child.info.session_id}

        with self.db.tx(immediate=True) as c:
            if cancelled:
                a_status, a_fc = "cancelled", FailureClass.CANCELLED.value
            elif deadline:
                a_status, a_fc = "failed", FailureClass.DEADLINE_EXCEEDED.value
            else:
                a_status = "succeeded" if outcome.success else "failed"
                a_fc = outcome.failure_class
            c.execute("UPDATE attempts SET status=?, exit_code=?, ended_at=?, "
                      "failure_class=?, usage_json=? WHERE id=?",
                      (a_status, exit_code, now_iso(), a_fc, json.dumps(usage),
                       attempt_id))
            res_status = "released" if group_ok else "quarantined"
            c.execute("UPDATE reservations SET status=?, released_at=? "
                      "WHERE attempt_id=? AND status='active'",
                      (res_status, now_iso() if group_ok else None, attempt_id))

            if cancelled:
                c.execute("UPDATE jobs SET finished_at=?, updated_at=? WHERE id=?",
                          (now_iso(), now_iso(), job_id))
            elif deadline:
                c.execute("UPDATE jobs SET status='failed', "
                          "failure_class=?, status_detail='deadline_exceeded', "
                          "finished_at=?, updated_at=?, scope_violations=? WHERE id=?",
                          (FailureClass.DEADLINE_EXCEEDED.value, now_iso(), now_iso(),
                           json.dumps(scope_violations), job_id))
            else:
                self._apply_job_outcome(c, job, outcome, result_obj,
                                        scope_violations, attempt_id,
                                        artifact_dir)

        if not group_ok:
            self.db.event("attempt", attempt_id, "quarantined",
                          {"reason": "process group not confirmed dead",
                           "hint": "ladd reconcile"})
        self.db.event("attempt", attempt_id, "finished",
                      {"exit_code": exit_code, "class": a_fc,
                       "group_confirmed": group_ok})
        log.info("attempt %s finished: %s class=%s", attempt_id, a_status, a_fc)

    def _apply_job_outcome(self, c, job: dict, outcome, result_obj,
                           scope_violations: list[str], attempt_id: str,
                           artifact_dir: str) -> None:
        now = now_iso()
        max_attempts = self.cfg.effective_max_attempts(self.cfg.profile(job["profile_id"]))
        raw_result = None
        if outcome.success:
            rp = os.path.join(artifact_dir, "result.json")
            try:
                with open(rp, "r", encoding="utf-8") as f:
                    raw_result = f.read()
            except OSError:
                raw_result = json.dumps(result_obj)
            c.execute("UPDATE jobs SET status='succeeded', result_json=?, "
                      "failure_class=?, status_detail=?, scope_violations=?, "
                      "finished_at=?, updated_at=? WHERE id=?",
                      (raw_result, outcome.failure_class, outcome.status_detail,
                       json.dumps(scope_violations), now, now, job["id"]))
            return
        if outcome.retryable and job["attempt_count"] < max_attempts:
            # fresh-session recovery in the SAME worktree; never --resume
            diff_path = os.path.join(artifact_dir, "diff.patch")
            diff = ""
            try:
                with open(diff_path, "r", encoding="utf-8") as f:
                    diff = f.read()
            except OSError:
                pass
            cp_path = os.path.join(artifact_dir, "checkpoint.json")
            checkpoint, _ = parse_checkpoint(cp_path)
            stream_path = os.path.join(artifact_dir, "stream.log")
            tail = ""
            try:
                with open(stream_path, "r", encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-self.cfg.daemon.log_tail_lines:])
            except OSError:
                pass
            rctx = build_recovery_context(
                checkpoint=checkpoint, diff=diff, log_tail=tail,
                diff_cap=self.cfg.daemon.diff_cap_bytes)
            rctx["from_attempt"] = attempt_id
            c.execute("UPDATE jobs SET status='queued', status_detail=?, "
                      "recovery_from_attempt=?, recovery_context=?, "
                      "failure_class=?, updated_at=? WHERE id=?",
                      (f"recovery_{outcome.failure_class}", attempt_id,
                       json.dumps(rctx), outcome.failure_class, now, job["id"]))
            return
        if outcome.retryable:
            c.execute("UPDATE jobs SET status='blocked', "
                      "status_detail='recovery_exhausted', failure_class=?, "
                      "scope_violations=?, updated_at=? WHERE id=?",
                      (outcome.failure_class, json.dumps(scope_violations), now, job["id"]))
            return
        if outcome.job_status == "blocked":
            c.execute("UPDATE jobs SET status='blocked', status_detail=?, "
                      "failure_class=?, scope_violations=?, updated_at=? WHERE id=?",
                      (outcome.status_detail, outcome.failure_class,
                       json.dumps(scope_violations), now, job["id"]))
            return
        # terminal failure — preserve whatever output exists
        raw = None
        try:
            with open(os.path.join(artifact_dir, "result.json"), "r",
                      encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raw = json.dumps({"note": outcome.status_detail or "failed",
                              "error": "no result file"})
        c.execute("UPDATE jobs SET status='failed', result_json=?, failure_class=?, "
                  "status_detail=?, scope_violations=?, finished_at=?, updated_at=? "
                  "WHERE id=?",
                  (raw, outcome.failure_class, outcome.status_detail,
                   json.dumps(scope_violations), now, now, job["id"]))

    # -- cancel -----------------------------------------------------------------------
    async def cancel_job(self, job_id: str, project_id: str | None = None) -> dict:
        conn = self.db.conn
        job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if job is None:
            raise ApiError(ErrorCodes.JOB_UNKNOWN, f"unknown job {job_id!r}")
        if project_id and job["project_id"] != project_id:
            raise ApiError(ErrorCodes.JOB_UNKNOWN, f"unknown job {job_id!r}")
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return self.jobs.job_view(job_id)
        if job["status"] in ("queued", "paused", "blocked"):
            with self.db.tx(immediate=True) as c:
                c.execute("UPDATE jobs SET status='cancelled', "
                          "status_detail='user_cancel', finished_at=?, updated_at=? "
                          "WHERE id=?", (now_iso(), now_iso(), job_id))
            self.db.event("job", job_id, "cancelled", {})
            return self.jobs.job_view(job_id)
        # starting / running: mark first (finalize must not recover it), then kill
        with self.db.tx(immediate=True) as c:
            c.execute("UPDATE jobs SET status='cancelled', status_detail='user_cancel', "
                      "updated_at=? WHERE id=?", (now_iso(), job_id))
        self.db.event("job", job_id, "cancel_requested", {})
        attempt_id = self._current_attempt_id(job_id)
        if attempt_id:
            child = self.supervisor.children.get(attempt_id)
            if child is not None:
                await self.supervisor.kill_group(child)
            else:
                a = self._current_attempt(job_id)
                if a and a["pgid"] and group_alive(a["pgid"]):
                    kill_group(a["pgid"], signal.SIGTERM)
                    await asyncio.sleep(min(self.cfg.daemon.kill_grace_s, 5.0))
                    if group_alive(a["pgid"]):
                        kill_group(a["pgid"], signal.SIGKILL)
        return self.jobs.job_view(job_id)

    # -- mode ---------------------------------------------------------------------------
    async def set_mode(self, project_id: str, mode: str, policy: str | None) -> dict:
        async with self.launch_gate:
            conn = self.db.conn
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise ApiError(ErrorCodes.PROJECT_UNKNOWN,
                               f"unknown project {project_id!r}")
            if mode == "single" and not policy:
                policy = "drain"
            now = now_iso()
            cancelled_running: list[str] = []
            with self.db.tx(immediate=True) as c:
                c.execute("UPDATE projects SET mode=?, drain_policy=?, "
                          "mode_changed_at=?, updated_at=? WHERE id=?",
                          (mode, policy, now, now, project_id))
                if mode == "single":
                    if policy == "cancel":
                        running = c.execute(
                            "SELECT id FROM jobs WHERE project_id=? AND "
                            "status IN ('starting','running')", (project_id,)).fetchall()
                        for r in running:
                            cancelled_running.append(r["id"])
                        c.execute("UPDATE jobs SET status='cancelled', "
                                  "status_detail='mode_cancel', finished_at=?, "
                                  "updated_at=? WHERE project_id=? AND "
                                  "status IN ('starting','running')",
                                  (now, now, project_id))
                    c.execute("UPDATE jobs SET status='paused', updated_at=? "
                              "WHERE project_id=? AND status='queued'",
                              (now, project_id))
            self.db.event("project", project_id, "mode_changed",
                          {"mode": mode, "policy": policy})
            for jid in cancelled_running:
                attempt_id = self._current_attempt_id(jid)
                if attempt_id:
                    child = self.supervisor.children.get(attempt_id)
                    if child is not None:
                        await self.supervisor.kill_group(child)
                    else:
                        a = self._current_attempt(jid)
                        if a and a["pgid"] and group_alive(a["pgid"]):
                            kill_group(a["pgid"], signal.SIGTERM)
                            await asyncio.sleep(min(self.cfg.daemon.kill_grace_s, 5.0))
                            if group_alive(a["pgid"]):
                                kill_group(a["pgid"], signal.SIGKILL)
            return dict(self.db.query_one(
                "SELECT id, mode, drain_policy, mode_changed_at FROM projects WHERE id=?",
                (project_id,)))

    # -- reconciliation --------------------------------------------------------------------
    async def startup_reconcile(self) -> dict:
        """Adopt or finalize every attempt that is still open after a restart.
        Re-adopted attempts are attached, never re-spawned ⇒ no duplicate
        execution (PRD §13, acceptance test 10)."""
        markers = scan_marker_processes()
        open_attempts = [dict(r) for r in self.db.query(
            "SELECT * FROM attempts WHERE status IN ('starting','running')")]
        seen: set[str] = set()
        adopted = finalized = quarantined = 0
        for a in open_attempts:
            seen.add(a["id"])
            job = dict(self.db.query_one("SELECT * FROM jobs WHERE id=?",
                                         (a["job_id"],)))
            m = markers.get(a["id"])
            if m and (not a["start_identity"]
                      or identity_matches(m["pid"], m["pgid"], a["start_identity"])):
                # live and ours — re-adopt (or backfill identity for the
                # commit-before-Popen crash window)
                ident = a["start_identity"] or make_identity(m["pid"])
                self.supervisor.attach(attempt_id=a["id"], job_id=a["job_id"],
                                       profile_id=job["profile_id"],
                                       pid=m["pid"], pgid=m["pgid"], identity=ident)
                if not a["start_identity"]:
                    self.db.execute(
                        "UPDATE attempts SET pid=?, pgid=?, start_identity=? "
                        "WHERE id=?", (m["pid"], m["pgid"], ident, a["id"]))
                self.db.event("attempt", a["id"], "re_adopted",
                              {"pid": m["pid"], "pgid": m["pgid"]})
                adopted += 1
            elif m and pid_alive(m["pid"]):
                # live process under this attempt id but a different
                # starttime: PID reuse. Never kill a stranger — quarantine.
                with self.db.tx(immediate=True) as c:
                    c.execute("UPDATE attempts SET status='interrupted', "
                              "failure_class='orphaned', ended_at=? WHERE id=?",
                              (now_iso(), a["id"]))
                    c.execute("UPDATE reservations SET status='quarantined', "
                              "released_at=NULL WHERE attempt_id=? AND "
                              "status='active'", (a["id"],))
                    c.execute("UPDATE jobs SET status='interrupted', "
                              "status_detail='pid_reuse_quarantine', updated_at=? "
                              "WHERE id=? AND status IN ('starting','running')",
                              (now_iso(), a["job_id"]))
                self.db.event("attempt", a["id"], "quarantined_pid_reuse",
                              {"pid": m["pid"]})
                quarantined += 1
            else:
                await self._finalize_dead_attempt(a, job)
                finalized += 1
        # marker processes with no open attempt: the daemon was down while they
        # ran — there is no other owner, so stop them.
        for attempt_id, m in markers.items():
            if attempt_id in seen:
                continue
            if group_alive(m["pgid"]):
                kill_group(m["pgid"], signal.SIGTERM)
                end = time.monotonic() + self.cfg.daemon.kill_grace_s
                while time.monotonic() < end and group_alive(m["pgid"]):
                    await asyncio.sleep(0.2)
                if group_alive(m["pgid"]):
                    kill_group(m["pgid"], signal.SIGKILL)
            self.db.event("attempt", attempt_id, "orphan_killed",
                          {"pid": m["pid"], "pgid": m["pgid"]})
        return {"adopted": adopted, "finalized": finalized,
                "quarantined": quarantined}

    async def _finalize_dead_attempt(self, a: dict, job: dict) -> None:
        """Open attempt whose process is gone (or never spawned). A valid
        result file means the worker finished while we were down; otherwise
        the job is interrupted and needs an explicit resume decision."""
        attempt_no = _attempt_no_from_id(a["id"])
        artifact_dir = self.paths.artifact_dir(a["job_id"], attempt_no)
        result_obj, result_err = load_result(
            os.path.join(artifact_dir, "result.json"))
        validation = validate_result(result_obj, job_id=a["job_id"],
                                     attempt_id=a["id"]) if result_obj else []
        ok = (result_obj is not None and not validation
              and result_obj.get("outcome") == "completed")
        if a["pgid"] and group_alive(a["pgid"]):
            group_ok = False  # shouldn't happen (no marker) but be strict
        else:
            group_ok = True
        with self.db.tx(immediate=True) as c:
            if ok:
                c.execute("UPDATE attempts SET status='succeeded', ended_at=? "
                          "WHERE id=?", (now_iso(), a["id"]))
                raw = None
                try:
                    with open(os.path.join(artifact_dir, "result.json"),
                              "r", encoding="utf-8") as f:
                        raw = f.read()
                except OSError:
                    raw = json.dumps(result_obj)
                c.execute("UPDATE jobs SET status='succeeded', result_json=?, "
                          "finished_at=?, updated_at=? WHERE id=?",
                          (raw, now_iso(), now_iso(), a["job_id"]))
            else:
                fc = FailureClass.EXIT_UNKNOWN.value if a["pid"] else \
                    FailureClass.SPAWN_LOST.value
                c.execute("UPDATE attempts SET status='interrupted', "
                          "failure_class=?, ended_at=? WHERE id=?",
                          (fc, now_iso(), a["id"]))
                c.execute("UPDATE jobs SET status='interrupted', "
                          "status_detail=?, failure_class=?, updated_at=? "
                          "WHERE id=? AND status IN ('starting','running')",
                          ("restart_no_result" if a["pid"]
                           else "restart_never_spawned", fc, now_iso(), a["job_id"]))
            c.execute("UPDATE reservations SET status=?, released_at=? "
                      "WHERE attempt_id=? AND status='active'",
                      ("released" if group_ok else "quarantined",
                       now_iso() if group_ok else None, a["id"]))
        self.db.event("attempt", a["id"], "reconciled",
                      {"outcome": "succeeded" if ok else "interrupted",
                       "result_error": result_err})

    async def reconcile(self) -> dict:
        """Manual `ladd reconcile`: re-check quarantined reservations against
        live process groups; release what is confirmed dead."""
        released = still = 0
        rows = self.db.query(
            """SELECT r.attempt_id, r.resource_id, a.job_id, a.pgid, a.pid
               FROM reservations r JOIN attempts a ON a.id = r.attempt_id
               WHERE r.status='quarantined'""")
        for r in rows:
            pgid = r["pgid"] or r["pid"]
            if pgid and group_alive(pgid):
                still += 1
                continue
            with self.db.tx(immediate=True) as c:
                c.execute("UPDATE reservations SET status='released', "
                          "released_at=? WHERE attempt_id=? AND status='quarantined'",
                          (now_iso(), r["attempt_id"]))
            self.db.event("reservation", r["attempt_id"], "released_after_reconcile",
                          {"resource": r["resource_id"]})
            released += 1
        return {"released": released, "still_quarantined": still}
