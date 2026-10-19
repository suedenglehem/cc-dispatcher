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
import re
import socket
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .config import DEFAULT_CONFIG_PATH, BackendConfig, load_config
from .db import Database, now_iso
from .models import (ApiError, BackendAddSaladParams, BackendRemoveSaladParams,
                     BackendSetParams, CancelParams, ErrorCodes,
                     IntegrateParams, JobRefParams, ProjectAddParams,
                     PromoteParams, ResumeParams, SetModeParams, SubmitParams,
                     WorkersParams)
from .salad import (SALAD_KEYFILE_DEFAULT, SALAD_PROXY_PORT_BASE,
                    SALAD_PROXY_PORT_SPAN, allocate_salad_port,
                    append_salad_backend, append_to_profile_backend_lists,
                    normalize_gateway, read_salad_key, remove_salad_backend)

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
    def __init__(self, db: Database, cfg, paths, jobs, scheduler, backends,
                 mycelium=None, salad=None):
        self.db = db
        self.cfg = cfg
        self.paths = paths
        self.jobs = jobs
        self.scheduler = scheduler
        self.backends = backends
        self.mycelium = mycelium
        self.salad = salad
        # The daemon is the single writer of config.toml; runtime salad
        # add/remove rewrites this exact file.
        self.config_path = getattr(cfg, "config_path", None) or DEFAULT_CONFIG_PATH
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
            # status?all_jobs is an explicit local full dump (ladd status
            # --all-jobs). The MCP forwarder never sends it — dispatch_status
            # always uses the bounded default — so the opt-in dump is exempt
            # from the cap that keeps the default surface bounded forever.
            capped = not (method == "status" and params.get("all_jobs"))
            return {"ok": True, "result": self._shape(result, capped)}
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
            "promote": self._m_promote,
            "workers": self._m_workers,
            "reconcile": self._m_reconcile,
            "backend_list": self._m_backend_list,
            "backend_set": self._m_backend_set,
            "backend_add_salad": self._m_backend_add_salad,
            "backend_remove_salad": self._m_backend_remove_salad,
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

    def _shape(self, result: dict, capped: bool = True) -> dict:
        """Redact credential values, then (unless exempted) enforce the 8k
        cap on the serialized response (valid JSON in, valid JSON out).

        `capped=False` is for explicit opt-in full dumps (status?all_jobs)
        that a local human requests; the cap still guards every response the
        primary session / MCP surface can trigger."""
        s = json.dumps(result)
        for secret in self._secrets():
            for variant in (secret,
                            secret.replace("\\", "\\\\").replace('"', '\\"')):
                s = s.replace(variant, "[REDACTED]")
        if capped:
            cap = self.cfg.daemon.response_cap_chars
            if len(s) > cap:
                raise ApiError(ErrorCodes.INTERNAL,
                               f"response too large: {len(s)} > {cap} chars; "
                               "narrow the query (pagination, include_result)")
        return json.loads(s)

    # -- methods -------------------------------------------------------------------
    async def _m_status(self, p: dict) -> dict:
        conn = self.db.conn
        all_jobs = bool(p.get("all_jobs"))
        projects = [dict(r) for r in conn.execute(
            "SELECT id, repo_path, integration_path, integration_ref, "
            "mycelium_project, mode, drain_policy, mode_changed_at "
            "FROM projects ORDER BY id")]
        jobs = []
        terminal = []
        for r in conn.execute(
                "SELECT id, project_id, objective, status, status_detail, "
                "profile_id, backend_id, attempt_count, base_commit, "
                "created_at, started_at, finished_at, deadline_expires_at, "
                "failure_class, integrated_commit, integration_disposition "
                "FROM jobs ORDER BY created_at, id"):
            d = dict(r)
            d["objective"] = d["objective"][:120]
            # Default view stays bounded as the history grows: terminal jobs
            # (succeeded/failed/cancelled) collapse into a count + last five;
            # `all_jobs=true` (ladd status --all-jobs) returns them in full.
            if not all_jobs and d["status"] in ("succeeded", "failed", "cancelled"):
                terminal.append(d)
            else:
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
        out = {
            "projects": projects,
            "jobs": jobs,
            "workers": self.backends.worker_availability(),
            "reservations": reservations,
            "schema_version": sv,
            "time": now_iso(),
        }
        if not all_jobs:
            out["jobs_terminal"] = {
                "count": len(terminal),
                "recent": [
                    {k: t[k] for k in ("id", "status", "failure_class",
                                       "finished_at")}
                    for t in sorted(terminal,
                                     key=lambda t: (t["finished_at"] or "",
                                                    t["id"]),
                                     reverse=True)[:5]
                ],
            }
        return out

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
        view = self.jobs.job_view(params.job_id, include_result=params.include_result)
        # live usage for in-flight attempts (durable usage_json covers the rest)
        for a in view.get("attempts") or []:
            if a.get("status") in ("running", "starting"):
                lc = self.scheduler.live_context(a["id"])
                if lc:
                    a.update(lc)
        return view

    async def _m_cancel(self, p: dict) -> dict:
        params = CancelParams(**p)
        return await self.scheduler.cancel_job(params.job_id, params.project_id)

    async def _m_resume(self, p: dict) -> dict:
        params = ResumeParams(**p)
        return await self.jobs.resume(params.job_id, params.project_id)

    async def _m_integrate(self, p: dict) -> dict:
        params = IntegrateParams(**p)
        return await self.jobs.integrate(params.job_id, params.commit, params.project_id)

    async def _m_promote(self, p: dict) -> dict:
        """Record the accepted decision as a pinned Mycelium memory (PRD §12).
        The primary calls this after acceptance + integrate; the daemon is
        the only component that can reach the memory store."""
        params = PromoteParams(**p)
        view = self.jobs.promote(params.job_id, params.project_id)
        if self.mycelium is None:
            raise ApiError(ErrorCodes.MYCELIUM_UNAVAILABLE,
                           "daemon has no mycelium configured "
                           "(missing [mycelium] section)")
        content = (f"[decision] project={view['mycelium_project']} "
                   f"job={view['job_id']} base={view['base_commit']} | "
                   f"accepted: {view['summary'] or 'worker work accepted'} "
                   f"(commit {view['integrated_commit']})")
        ok = await self.mycelium.save(content, project=view["mycelium_project"],
                                      pinned=True, confidence=0.9)
        if not ok:
            raise ApiError(ErrorCodes.MYCELIUM_UNAVAILABLE,
                           "mycelium save failed (outage) — the decision was "
                           "not recorded; retry `ladd promote`")
        return view

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
        # pick_backend/worker_availability gate on the in-memory config, not
        # the DB row — keep the two in lockstep or runtime changes would need
        # a daemon restart to take effect (config still reasserts on start).
        b = self.cfg.backend(params.backend_id)
        if b is not None:
            b.enabled = bool(enabled)
            b.manually_reserved = bool(reserved)
            b.reserved_note = note or None
        self.db.event("backend", params.backend_id,
                      "backend_modified",
                      {"enabled": enabled, "reserved": bool(reserved)})
        return {"backend": dict(self.db.query_one(
            "SELECT * FROM backends WHERE id=?", (params.backend_id,)))}

    # -- salad backends (runtime config edits) ----------------------------------
    def _commit_config(self, text: str) -> None:
        """Write config.toml atomically: tmp file, full re-validation, then
        os.replace. On validation failure nothing is changed."""
        path = self.config_path
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            load_config(tmp)      # raise BEFORE touching the live file
        except Exception as e:
            os.unlink(tmp)
            raise ApiError(ErrorCodes.SALAD_CONFIG_INVALID,
                           f"refusing to install config (nothing was "
                           f"changed): {e}")
        try:
            mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def _reload_config(self) -> None:
        """Re-read config from disk and splice the live lists in place:
        every component (scheduler, supervisor, api, salad manager) shares
        this one Config instance."""
        new = load_config(self.config_path)
        self.cfg.backends[:] = new.backends
        self.cfg.groups[:] = new.groups
        self.cfg.profiles[:] = new.profiles
        self.cfg.projects[:] = new.projects

    async def _m_backend_add_salad(self, p: dict) -> dict:
        params = BackendAddSaladParams(**p)
        try:
            gw = normalize_gateway(params.url)
        except ValueError as e:
            raise ApiError(ErrorCodes.SALAD_URL_INVALID, str(e))
        host = gw.split("://", 1)[1].split(":", 1)[0]
        sid = params.id or host.split(".", 1)[0]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", sid):
            raise ApiError(ErrorCodes.SALAD_URL_INVALID,
                           f"derived backend id {sid!r} is invalid; pass an "
                           "explicit id (--id / id=)")
        if self.cfg.backend(sid) is not None:
            raise ApiError(ErrorCodes.SALAD_ID_EXISTS,
                           f"backend {sid!r} already exists",
                           endpoint=self.cfg.backend(sid).endpoint)
        keyfile = params.keyfile or SALAD_KEYFILE_DEFAULT
        if not read_salad_key(keyfile):
            raise ApiError(ErrorCodes.SALAD_KEYFILE_MISSING,
                           f"cannot read a Salad API key from {keyfile}; "
                           "write the key there or pass a different path "
                           "(--keyfile / keyfile=)")
        if params.port is not None:
            port = params.port
            for other in self.cfg.backends:
                if (other.kind == "salad" and other.id != sid
                        and other.salad_proxy_port == port):
                    raise ApiError(ErrorCodes.SALAD_PORT_IN_USE,
                                   f"port {port} is already used by salad "
                                   f"backend {other.id!r}")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                raise ApiError(ErrorCodes.SALAD_PORT_IN_USE,
                               f"port {port} is bound by another local "
                               "process")
            finally:
                s.close()
        else:
            port = allocate_salad_port(self.cfg)
            if port is None:
                raise ApiError(ErrorCodes.SALAD_PORT_EXHAUSTED,
                               "no free port in the salad proxy range "
                               f"{SALAD_PROXY_PORT_BASE}-"
                               f"{SALAD_PROXY_PORT_BASE + SALAD_PROXY_PORT_SPAN - 1}; "
                               "pass an explicit port (--port / port=)")
        if params.resource_group and not any(
                g.id == params.resource_group for g in self.cfg.groups):
            raise ApiError(ErrorCodes.SALAD_CONFIG_INVALID,
                           f"unknown resource_group {params.resource_group!r}",
                           groups=[g.id for g in self.cfg.groups])
        b = BackendConfig(id=sid, endpoint=gw, model_id=params.model,
                          model_label=params.model, kind="salad",
                          salad_keyfile=keyfile, salad_proxy_port=port,
                          salad_thinking=params.thinking,
                          concurrency=params.concurrency,
                          resource_group=params.resource_group,
                          notes=params.notes)
        text = ""
        if os.path.exists(self.config_path):
            text = Path(self.config_path).read_text(encoding="utf-8")
        new_text = append_to_profile_backend_lists(
            append_salad_backend(text, b), sid, adapters={"claude"})
        self._commit_config(new_text)
        self._reload_config()
        self.backends.sync()
        # Bringing the proxy up is best-effort here: a group the user hasn't
        # started yet is a normal offline state, not an error. Every probe
        # re-calls ensure(), so the proxy self-heals when the group comes up.
        up, detail = (await self.salad.ensure(self.cfg.backend(sid))
                      if self.salad is not None
                      else (False, "daemon has no salad proxy manager"))
        res = await self.backends.probe(sid, force=True)
        self.db.event("backend", sid, "salad_backend_added",
                      {"endpoint": gw, "proxy_port": port,
                       "model": params.model, "proxy_up": up,
                       "health": res.health})
        row = dict(self.db.query_one("SELECT * FROM backends WHERE id=?",
                                     (sid,)))
        return {
            "backend": row,
            "proxy": f"127.0.0.1:{port}",
            "proxy_up": up,
            "proxy_detail": detail,
            "health": res.health,
            "key_rejected": res.error == "HTTP 403",
            "offline_detail": "" if res.health == "online" else res.error,
        }

    async def _m_backend_remove_salad(self, p: dict) -> dict:
        params = BackendRemoveSaladParams(**p)
        b = self.cfg.backend(params.backend_id)
        if b is None or b.kind != "salad":
            raise ApiError(ErrorCodes.SALAD_NOT_FOUND,
                           f"{params.backend_id!r} is not a registered salad "
                           "backend",
                           salad_backends=[x.id for x in self.cfg.backends
                                           if x.kind == "salad"])
        busy = (self.backends.active_reservations(b.id)
                + self.backends.quarantined_reservations(b.id))
        if busy:
            raise ApiError(ErrorCodes.SALAD_BACKEND_BUSY,
                           f"backend {b.id} still has {busy} running or "
                           "quarantined worker reservation(s); cancel its "
                           "jobs first (ladd job cancel), then remove again")
        if self.salad is not None:
            self.salad.stop(b.id)
        text = ""
        if os.path.exists(self.config_path):
            text = Path(self.config_path).read_text(encoding="utf-8")
        try:
            new_text = remove_salad_backend(text, b.id)
        except ValueError as e:
            raise ApiError(ErrorCodes.SALAD_REMOVE_FAILED, str(e))
        self._commit_config(new_text)
        self._reload_config()
        self.backends.sync()   # tombstones the row: history stays resolvable
        self.db.event("backend", b.id, "salad_backend_removed",
                      {"endpoint": b.endpoint})
        row = self.db.query_one("SELECT * FROM backends WHERE id=?", (b.id,))
        return {"backend": dict(row) if row else None, "removed": True}

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
    def status(self, all_jobs: bool = False) -> dict:
        return self.call("status", all_jobs=all_jobs)

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

    def promote(self, job_id: str, project_id: str | None = None) -> dict:
        return self.call("promote", job_id=job_id, project_id=project_id)

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

    def backend_add_salad(self, url: str, id: str | None = None,
                          port: int | None = None, keyfile: str = "",
                          model: str = "qwen", thinking: bool = False,
                          concurrency: int = 1, resource_group: str = "",
                          notes: str = "") -> dict:
        return self.call("backend_add_salad", url=url, id=id, port=port,
                         keyfile=keyfile, model=model, thinking=thinking,
                         concurrency=concurrency, resource_group=resource_group,
                         notes=notes)

    def backend_remove_salad(self, backend_id: str) -> dict:
        return self.call("backend_remove_salad", backend_id=backend_id)

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
