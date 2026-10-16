"""Shared fixtures: scratch git repos, fake backends, and an in-process
dispatcherd (its own event loop in a thread — signal handlers are
main-thread-only, so components are constructed directly, mirroring
daemon.serve() minus flock/signals).

Tests are synchronous: they talk to the daemon through ApiClient over the
Unix socket (the real wire) and inspect internals via handle.query()
(a fresh WAL reader connection) or handle.scheduler (same thread as the
event loop only via loop.call_soon_threadsafe when needed — tests instead
set test seams like pre_gate_hook through the public API where possible).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from local_dispatcher.adapters import get_adapter
from local_dispatcher.api import ApiClient, ApiServer
from local_dispatcher.backends import BackendRegistry
from local_dispatcher.config import load_config
from local_dispatcher.daemon import sync_projects_from_config, worker_policy_text
from local_dispatcher.db import Database
from local_dispatcher.jobs import Jobs
from local_dispatcher.scheduler import Scheduler
from local_dispatcher.supervisor import Supervisor

from tests.fakes.fake_backend import FakeBackend

HERE = Path(__file__).resolve().parent
FAKE_WORKER = os.path.abspath(str(HERE / "fakes" / "fake_worker.py"))


# -- repos -------------------------------------------------------------------

def make_repo(path: Path, dirty: bool = False) -> Path:
    """Fresh git repo: main branch + initial commit (README, src/app.py).
    dirty=True leaves an uncommitted, untracked file (test 12)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=path, check=True,
                           capture_output=True, text=True)
        return r.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "dispatcher-test@example.invalid")
    git("config", "user.name", "Dispatcher Test")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text(
        "def main():\n    return 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "initial")
    if dirty:
        (path / "src" / "dirty_note.txt").write_text(
            "local uncommitted work\n", encoding="utf-8")
    return path


def repo_head(path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                       capture_output=True, text=True)
    return r.stdout.strip()


# -- daemon handle ------------------------------------------------------------

@dataclass
class DaemonHandle:
    root: Path
    cfg: object
    stop_event: threading.Event
    thread: threading.Thread
    ready: threading.Event
    state: dict = field(default_factory=dict)   # db/backends/jobs/supervisor/scheduler/api
    errors: list = field(default_factory=list)

    def __post_init__(self):
        self._stopped = False

    @property
    def db(self) -> Database:
        return self.state["db"]

    @property
    def backends(self) -> BackendRegistry:
        return self.state["backends"]

    @property
    def jobs(self) -> Jobs:
        return self.state["jobs"]

    @property
    def supervisor(self) -> Supervisor:
        return self.state["supervisor"]

    @property
    def scheduler(self) -> Scheduler:
        return self.state["scheduler"]

    @property
    def api(self) -> ApiServer:
        return self.state["api"]

    def client(self) -> ApiClient:
        return ApiClient(self.cfg.paths.socket_path)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Fresh WAL reader connection — safe from the test thread."""
        conn = sqlite3.connect(self.cfg.paths.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def wait_ready(self, timeout: float = 30.0) -> None:
        if not self.ready.wait(timeout):
            raise RuntimeError(f"daemon did not become ready; errors={self.errors}")
        if self.errors:
            raise RuntimeError(f"daemon thread failed: {self.errors[0]}")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise RuntimeError("daemon thread did not stop in 20s")
        if self.errors:
            raise RuntimeError(f"daemon thread failed: {self.errors[0]}")


def _daemon_body(cfg, state: dict, stop_event: threading.Event,
                 ready: threading.Event, errors: list) -> None:
    async def serve() -> None:
        cfg.paths.ensure()
        db = Database(cfg.paths.db_path)
        db.migrate()
        backends = BackendRegistry(cfg, db)
        backends.sync()
        sync_projects_from_config(cfg, db)
        jobs = Jobs(db, cfg, cfg.paths, worker_policy_text())
        supervisor = Supervisor(
            db, cfg, cfg.paths,
            adapter_for=lambda pid: get_adapter(cfg.profile(pid).adapter))
        scheduler = Scheduler(db, cfg, cfg.paths, backends, supervisor, jobs, None)
        state.update(db=db, backends=backends, jobs=jobs, supervisor=supervisor,
                     scheduler=scheduler)
        await scheduler.startup_reconcile()
        api = ApiServer(db, cfg, cfg.paths, jobs, scheduler, backends)
        await api.start()
        state.update(api=api)
        await scheduler.start()
        ready.set()
        for b in cfg.backends:
            if not b.is_primary:
                try:
                    await backends.probe(b.id, force=True)
                except Exception:
                    pass
        await asyncio.get_running_loop().run_in_executor(None, stop_event.wait)
        await scheduler.stop()
        await api.stop()

    try:
        asyncio.run(serve())
    except BaseException as e:  # surface in the test thread
        errors.append(e)
        ready.set()


# -- env fixture ---------------------------------------------------------------

@dataclass
class Env:
    tmp_path: Path
    root: Path = None
    repo: Path = None
    backends: list = field(default_factory=list)     # backend TOML specs
    _fakes: list = field(default_factory=list)
    _handles: list = field(default_factory=list)

    def __post_init__(self):
        self.root = self.tmp_path / "state"
        self.repo = make_repo(self.tmp_path / "repo")
        self.repo_head = repo_head(self.repo)

    # backends ----------------------------------------------------------------

    def backend(self, id: str | None = None, primary: bool = False,
                ctx_max: int = 32000, shape: str = "llamacpp", **extra) -> dict:
        n = len(self.backends)
        bid = id or f"b{n + 1}"
        be = FakeBackend(served_id=f"model-{bid}", ctx_max=ctx_max,
                         shape=shape).start()
        self._fakes.append(be)
        spec = {"id": bid, "endpoint": be.endpoint,
                "model_id": f"model-{bid}", "model_label": f"model-{bid}",
                "is_primary": primary}
        spec.update(extra)
        self.backends.append(spec)
        return spec

    # projects ------------------------------------------------------------------

    def project(self, id: str = "p1", repo=None, **extra) -> dict:
        return {"id": id, "repo_path": str(repo or self.repo),
                "integration_ref": "main", **extra}

    # daemon ----------------------------------------------------------------------

    def daemon(self, backends=None, profiles=None, projects=None,
               groups=(), worker_mode: str = "normal") -> DaemonHandle:
        """Build config + start the in-process daemon; return the handle.

        backends: list of specs from Env.backend() (default: all started)
        profiles: list of dicts (id required); missing backend_ids default
                  to every non-primary backend; FAKE_WORKER_MODE /
                  FAKE_LAUNCH_LOG are injected into env_overrides.
        projects: list of Env.project() dicts (default: p1 on self.repo)
        worker_mode: FAKE_WORKER_MODE for every profile.
        """
        specs = backends if backends is not None else self.backends
        bids = [b["id"] for b in specs if not b.get("is_primary")]
        profiles = [dict(p) for p in (
            profiles if profiles is not None else [dict(id="w")])]
        for p in profiles:
            p.setdefault("backend_ids", bids)
            p.setdefault("adapter", "claude")
            p.setdefault("deadline_s", 600)
            p.setdefault("max_attempts", 2)
            ov = dict(p.get("env_overrides") or {})
            ov.setdefault("FAKE_WORKER_MODE", worker_mode)
            ov.setdefault("FAKE_LAUNCH_LOG", str(self.root / "launch.log"))
            p["env_overrides"] = ov
        projects = projects if projects is not None else [self.project()]

        cfg = load_config(self._write_toml(specs, groups, profiles, projects))
        stop_event = threading.Event()
        ready = threading.Event()
        errors: list = []
        handle = DaemonHandle(root=self.root, cfg=cfg, stop_event=stop_event,
                              thread=None, ready=ready, errors=errors)
        t = threading.Thread(
            target=_daemon_body,
            args=(cfg, handle.state, stop_event, ready, errors),
            name=f"dispatcherd-{self.root.name}", daemon=True)
        handle.thread = t
        t.start()
        handle.wait_ready()
        self._handles.append(handle)
        return handle

    def _write_toml(self, backends, groups, profiles, projects) -> Path:
        L: list[str] = []
        L += ["[state]",
              f"root = {json.dumps(str(self.root))}",
              f"worktree_root = {json.dumps(str(self.root / 'worktrees'))}",
              f"socket = {json.dumps(str(self.root / 'run' / 'dispatcher.sock'))}",
              "",
              "[daemon]",
              "tick_s = 0.2",
              "probe_ttl_s = 3600",
              "probe_timeout_s = 2.0",
              "kill_grace_s = 1.0",
              "kill_confirm_s = 0.2",
              "",
              "[worker]",
              f"claude_executable = {json.dumps(FAKE_WORKER)}",
              'prompt_via = "stdin"',
              "deadline_s = 600",
              "max_attempts = 2",
              'output_format = "stream-json"',
              'anthropic_api_key = "local"',
              "permission_mode = \"bypassPermissions\"",
              ""]
        for g in groups:
            L += ["[[resource_groups]]",
                  f"id = {json.dumps(g['id'])}",
                  f"max_concurrency = {json.dumps(g['max_concurrency'])}", ""]
        for b in backends:
            L.append("[[backends]]")
            for k, v in b.items():
                L.append(f"{k} = {json.dumps(v)}")
            L.append("")
        for p in profiles:
            L.append("[[profiles]]")
            for k, v in p.items():
                if k == "env_overrides":
                    continue
                L.append(f"{k} = {json.dumps(v)}")
            L.append("")
            if p.get("env_overrides"):
                L.append("[profiles.env_overrides]")
                for k, v in p["env_overrides"].items():
                    L.append(f"{k} = {json.dumps(v)}")
                L.append("")
        for pr in projects:
            L.append("[[projects]]")
            for k, v in pr.items():
                L.append(f"{k} = {json.dumps(v)}")
            L.append("")
        path = self.root / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(L), encoding="utf-8")
        return path

    # teardown --------------------------------------------------------------------

    def stop_daemon(self) -> None:
        for h in self._handles:
            try:
                h.stop()
            except RuntimeError:
                pass  # already failed; errors were raised at wait_ready
        for be in self._fakes:
            be.stop()


@pytest.fixture
def env(tmp_path) -> Env:
    e = Env(tmp_path)
    yield e
    e.stop_daemon()
