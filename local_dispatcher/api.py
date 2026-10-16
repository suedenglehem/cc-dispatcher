"""Newline-JSON API over a Unix domain socket (chmod 600).

Wire format: one request line `{"method": ..., "params": {...}}` → one
response line `{"ok": true, "result": ...}` or
`{"ok": false, "error": {"code", "message", "detail"?}}`.

Guarantees (PRD §8): 8k response cap, credential redaction, bounded
pagination on events, artifact references instead of inline bulk content
(large results carry a `truncated_note` pointing at the file on disk).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket

from pydantic import ValidationError

from .db import Database, now_iso
from .models import (ApiError, BackendSetParams, CancelParams, ErrorCodes,
                     IntegrateParams, JobRefParams, ProjectAddParams,
                     ResumeParams, SetModeParams, SubmitParams, WorkersParams)

log = logging.getLogger("dispatcherd.api")

MAX_REQUEST_BYTES = 1_000_000


def _resolve_project(conn, project_id: str | None) -> dict:
    """Bind an optional project_id to a project: explicit, or the sole
    project; ambiguous/unknown → actionable error listing known ids."""
    if project_id:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            known = [r["id"] for r in conn.execute("SELECT id FROM projects ORDER BY id")]
            raise ApiError(ErrorCodes.PROJECT_UNKNOWN,
                           f"unknown project {project_id!r}", known=known)
        return dict(row)
    rows = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    if not rows:
        raise ApiError(ErrorCodes.PROJECT_UNKNOWN,
                       "no projects configured; add one with `ladd project add`")
    raise ApiError(ErrorCodes.PROJECT_UNKNOWN,
                   "ambiguous project; specify project_id",
                   known=[r["id"] for r in rows])


class ApiServer:
    def __init__(self, db: Database, cfg, paths, jobs, scheduler, backends):
        self.db = db
        self.cfg = cfg
        self.paths = paths
        self.jobs = jobs
        self.scheduler = scheduler
        self.backends = backends
        self._server: asyncio.base_events.UnixServer | None = None

    async def start(self) -> None:
        if os.path.exists(self.paths.socket_path):
            os.unlink(self.paths.socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.paths.socket_path)
        os.chmod(self.paths.socket_path, 0o600)
        log.info("api listening on %s", self.paths.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- transport -------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.read(MAX_REQUEST_BYTES + 1)
                if not line or not line.endswith(b"\n"):
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    await self._send(writer,
                                     {"ok": False, "error": {
                                         "code": "validation",
                                         "message": f"bad JSON: {e}"}})
                    continue
                resp = await self._dispatch(req)
                await self._send(writer, resp)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("api connection handler crashed")
        finally:
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await writer.drain()

    # -- dispatch ----------------------------------------------------------------
    async def _dispatch(self, req: dict) -> dict:
        method = req.get("method", "")
        params = req.get("params") or {}
        try:
            result = await self._call(method, params)
            return {"ok": True, "result": self._shape(result)}
        except ApiError as e:
            return {"ok": False, "error": e.to_dict()}
        except ValidationError as e:
            return {"ok": False, "error": {
                "code": "validation", "message": str(e)[:2000]}}
        except Exception:
            log.exception("api method %s failed", method)
            return {"ok": False, "error": {
                "code": ErrorCodes.INTERNAL, "message": "internal error"}}

    async def _call(self, method: str, params: dict) -> dict:
        m = {
            "status": self._m_status,
            "set_mode": self._m_set_mode,
            "submit": self._m_submit,
            "job": self._m_job,
            "cancel": self._m_cancel,
            "resume": self._m_resume,
            "integrate": self._m_integrate,
            "workers": self._m_workers,
            "reconcile": self._m_reconcile,
            "backend_list": self._m_backend_list,
            "backend_set": self._m_backend_set,
            "project_list": self._m_project_list,
            "project_add": self._m_project_add,
            "events": self._m_events,
        }
        fn = m.get(method)
        if fn is None:
            raise ApiError(ErrorCodes.INTERNAL, f"unknown method {method!r}")
        return await fn(params)

    # -- response guarantees ------------------------------------------------------
    def _secrets(self) -> list[str]:
        out: set[str] = set()
        for row in self.db.query("SELECT credential_env FROM backends"):
            try:
                env = json.loads(row["credential_env"] or "{}")
            except json.JSONDecodeError:
                continue
            for v in env.values():
                if isinstance(v, str) and v:
                    out.add(v)
        return [s for s in out if len(s) >= 8]

    def _shape(self, result: dict) -> dict:
        """Redact credential values, then enforce the 8k cap on the
        serialized response (valid JSON in, valid JSON out)."""
        s = json.dumps(result)
        for secret in self._secrets():
            for variant in (secret,
                            secret.replace("\\", "\\\\").replace('"', '\\"')):
                s = s.replace(variant, "[REDACTED]")
        cap = self.cfg.daemon.response_cap_chars
        if len(s) > cap:
            raise ApiError(ErrorCodes.INTERNAL,
                           f"response too large: {len(s)} > {cap} chars; "
                           "narrow the query (pagination, include_result)")
        return json.loads(s)

    # -- methods -------------------------------------------------------------------
    async def _m_status(self, p: dict) -> dict:
        conn = self.db.conn
        projects = [dict(r) for r in conn.execute(
            "SELECT id, repo_path, integration_path, integration_ref, "
            "mycelium_project, mode, drain_policy, mode_changed_at "
            "FROM projects ORDER BY id")]
        jobs = []
        for r in conn.execute(
                "SELECT id, project_id, objective, status, status_detail, "
                "profile_id, backend_id, attempt_count, base_commit, "
                "created_at, started_at, finished_at, deadline_expires_at, "
                "failure_class, integrated_commit, integration_disposition "
                "FROM jobs ORDER BY created_at, id"):
            d = dict(r)
            d["objective"] = d["objective"][:120]
            jobs.append(d)
        reservations = {
            "active": [dict(x) for x in conn.execute(
                "SELECT * FROM reservations WHERE status='active' "
                "ORDER BY acquired_at, attempt_id")],
            "quarantined": [dict(x) for x in conn.execute(
                "SELECT * FROM reservations WHERE status='quarantined' "
                "ORDER BY acquired_at, attempt_id")],
        }
        sv = conn.execute("SELECT MAX(v) AS v FROM schema_version").fetchone()["v"]
        return {
            "projects": projects,
            "jobs": jobs,
            "workers": self.backends.worker_availability(),
            "reservations": reservations,
            "schema_version": sv,
            "time": now_iso(),
        }

    async def _m_set_mode(self, p: dict) -> dict:
        params = SetModeParams(**p)
        project = _resolve_project(self.db.conn, params.project_id)
        return await self.scheduler.set_mode(project["id"], params.mode, params.policy)

    async def _m_submit(self, p: dict) -> dict:
        params = SubmitParams(**p)
        project = _resolve_project(self.db.conn, params.project_id)
        job_id, created = await self.jobs.submit(project["id"], params)
        return {"job_id": job_id, "created": created}

    async def _m_job(self, p: dict) -> dict:
        params = JobRefParams(**p)
        return self.jobs.job_view(params.job_id, include_result=params.include_result)

    async def _m_cancel(self, p: dict) -> dict:
        params = CancelParams(**p)
        return await self.scheduler.cancel_job(params.job_id, params.project_id)

    async def _m_resume(self, p: dict) -> dict:
        params = ResumeParams(**p)
        return await self.jobs.resume(params.job_id, params.project_id)

    async def _m_integrate(self, p: dict) -> dict:
        params = IntegrateParams(**p)
        return await self.jobs.integrate(params.job_id, params.commit, params.project_id)

    async def _m_workers(self, p: dict) -> dict:
        params = WorkersParams(**p)
        if params.refresh:
            for b in self.cfg.backends:
                if not b.is_primary:
                    try:
                        await self.backends.probe(b.id, force=True)
                    except Exception:
                        log.exception("refresh probe of %s failed", b.id)
        return {"workers": self.backends.worker_availability()}

    async def _m_reconcile(self, p: dict) -> dict:
        return await self.scheduler.reconcile()

    async def _m_backend_list(self, p: dict) -> dict:
        rows = []
        for r in self.db.query("SELECT * FROM backends ORDER BY id"):
            d = dict(r)
            # never expose credential values, only the variable names
            try:
                env = json.loads(d.get("credential_env") or "{}")
            except json.JSONDecodeError:
                env = {}
            d["credential_env"] = {k: "***" for k in env}
            rows.append(d)
        return {"backends": rows}

    async def _m_backend_set(self, p: dict) -> dict:
        params = BackendSetParams(**p)
        row = self.db.query_one("SELECT * FROM backends WHERE id=?",
                                (params.backend_id,))
        if row is None:
            known = [r["id"] for r in self.db.query("SELECT id FROM backends ORDER BY id")]
            raise ApiError(ErrorCodes.BACKEND_UNKNOWN,
                           f"unknown backend {params.backend_id!r}", known=known)
        if row["is_primary"]:
            raise ApiError(ErrorCodes.BACKEND_PRIMARY,
                           "the primary backend is reserved for the "
                           "coordinating session and cannot be modified at runtime")
        enabled = params.enabled if params.enabled is not None else row["enabled"]
        reserved = (1 if params.reserved else 0) if params.reserved is not None \
            else row["manually_reserved"]
        note = params.note if (params.note or params.reserved is not None) \
            else row["reserved_note"]
        with self.db.tx() as c:
            c.execute("UPDATE backends SET enabled=?, manually_reserved=?, "
                      "reserved_note=? WHERE id=?",
                      (1 if enabled else 0, reserved, note or None, params.backend_id))
        self.db.event("backend", params.backend_id,
                      "backend_modified",
                      {"enabled": enabled, "reserved": bool(reserved)})
        return {"backend": dict(self.db.query_one(
            "SELECT * FROM backends WHERE id=?", (params.backend_id,)))}

    async def _m_project_list(self, p: dict) -> dict:
        rows = [dict(r) for r in self.db.query("SELECT * FROM projects ORDER BY id")]
        return {"projects": rows}

    async def _m_project_add(self, p: dict) -> dict:
        params = ProjectAddParams(**p)
        repo = os.path.expanduser(params.repo_path)
        if not os.path.isdir(repo) or not (
                os.path.isdir(os.path.join(repo, ".git"))
                or os.path.isfile(os.path.join(repo, ".git"))):
            raise ApiError(ErrorCodes.PROJECT_PATH_INVALID,
                           f"{params.repo_path} is not a git repository")
        exists = self.db.query_one("SELECT id FROM projects WHERE id=?",
                                   (params.id,))
        if exists is not None:
            raise ApiError(ErrorCodes.PROJECT_EXISTS,
                           f"project {params.id!r} already exists")
        now = now_iso()
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO projects (id, repo_path, integration_path,
                       integration_ref, mycelium_project, mode, drain_policy,
                       mode_changed_at, updated_at)
                   VALUES (?,?,?,?,?, 'single', 'drain', ?, ?)""",
                (params.id, os.path.abspath(repo), params.integration_path,
                 params.integration_ref, params.mycelium_project, now, now))
        self.db.event("project", params.id, "project_added",
                      {"repo_path": os.path.abspath(repo)})
        return {"project": dict(self.db.query_one(
            "SELECT * FROM projects WHERE id=?", (params.id,)))}

    async def _m_events(self, p: dict) -> dict:
        limit = max(1, min(int(p.get("limit", 50)), 500))
        subject_type = p.get("subject_type") or None
        subject_id = p.get("subject_id") or None
        sql = ("SELECT id, ts, subject_type, subject_id, event, "
               "detail_json AS detail FROM events")
        args: list = []
        where = []
        if subject_type:
            where.append("subject_type=?")
            args.append(subject_type)
        if subject_id:
            where.append("subject_id=?")
            args.append(subject_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = []
        for r in self.db.query(sql, tuple(args)):
            d = dict(r)
            try:
                detail = json.loads(d["detail"] or "{}")
            except json.JSONDecodeError:
                detail = None
            d["detail"] = detail
            if detail is not None:
                ds = json.dumps(detail)
                if len(ds) > 500:
                    d["detail"] = {"_truncated": ds[:500]}
            rows.append(d)
        return {"events": rows, "limit": limit}


class ApiClient:
    """Synchronous client for the admin CLI (and the MCP forwarder in P2).
    Connection refused → ApiError(DAEMON_NOT_RUNNING) so callers can render
    an actionable message."""

    def __init__(self, socket_path: str, timeout_s: float = 15.0):
        self.socket_path = socket_path
        self.timeout_s = timeout_s

    def call(self, method: str, **params) -> dict:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout_s)
            s.connect(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            raise ApiError(
                ErrorCodes.DAEMON_NOT_RUNNING,
                f"dispatcher daemon is not running (no socket at "
                f"{self.socket_path}); start it (systemctl --user start "
                f"local-dispatcher or dispatcherd) — {e}") from e
        except (TimeoutError, socket.timeout) as e:
            raise ApiError(ErrorCodes.INTERNAL,
                           "timed out talking to the dispatcher daemon; it may "
                           "be stuck (check `systemctl --user status "
                           "local-dispatcher`)") from e
        try:
            with s:
                s.sendall((json.dumps({"method": method, "params": params})
                           + "\n").encode("utf-8"))
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > MAX_REQUEST_BYTES * 4:
                        raise ApiError(ErrorCodes.INTERNAL,
                                       "response from daemon exceeded size cap")
        except ApiError:
            raise
        except (TimeoutError, socket.timeout) as e:
            raise ApiError(ErrorCodes.INTERNAL,
                           "timed out waiting for the daemon's reply") from e
        if not buf:
            raise ApiError(ErrorCodes.DAEMON_NOT_RUNNING,
                           "daemon closed the connection without a reply")
        try:
            resp = json.loads(buf)
        except json.JSONDecodeError as e:
            raise ApiError(ErrorCodes.INTERNAL,
                           f"unparsable daemon reply: {e}") from e
        if resp.get("ok"):
            return resp["result"]
        err = resp.get("error") or {}
        extra = err.get("detail") or {}
        raise ApiError(err.get("code", ErrorCodes.INTERNAL),
                       err.get("message", "unknown daemon error"), **extra)

    # -- typed convenience ---------------------------------------------------------
    def status(self) -> dict:
        return self.call("status")

    def set_mode(self, mode: str, project_id: str | None = None,
                 policy: str | None = None) -> dict:
        return self.call("set_mode", mode=mode, project_id=project_id,
                         policy=policy)

    def submit(self, project_id: str | None, **params) -> dict:
        return self.call("submit", project_id=project_id, **params)

    def job(self, job_id: str, project_id: str | None = None,
            full: bool = False) -> dict:
        return self.call("job", job_id=job_id, project_id=project_id,
                         include_result=full)

    def cancel(self, job_id: str, project_id: str | None = None) -> dict:
        return self.call("cancel", job_id=job_id, project_id=project_id)

    def resume(self, job_id: str, project_id: str | None = None) -> dict:
        return self.call("resume", job_id=job_id, project_id=project_id)

    def integrate(self, job_id: str, commit: str,
                  project_id: str | None = None) -> dict:
        return self.call("integrate", job_id=job_id, commit=commit,
                         project_id=project_id)

    def workers(self, refresh: bool = False) -> dict:
        return self.call("workers", refresh=refresh)

    def reconcile(self) -> dict:
        return self.call("reconcile")

    def backend_list(self) -> dict:
        return self.call("backend_list")

    def backend_set(self, backend_id: str, enabled: bool | None = None,
                    reserved: bool | None = None, note: str = "") -> dict:
        return self.call("backend_set", backend_id=backend_id, enabled=enabled,
                         reserved=reserved, note=note)

    def project_list(self) -> dict:
        return self.call("project_list")

    def project_add(self, id: str, repo_path: str, integration_path: str = "",
                    integration_ref: str = "main",
                    mycelium_project: str = "") -> dict:
        return self.call("project_add", id=id, repo_path=repo_path,
                         integration_path=integration_path,
                         integration_ref=integration_ref,
                         mycelium_project=mycelium_project)

    def events(self, subject_type: str | None = None,
               subject_id: str | None = None, limit: int = 50) -> dict:
        return self.call("events", subject_type=subject_type,
                         subject_id=subject_id, limit=limit)
