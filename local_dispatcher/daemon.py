"""dispatcherd entrypoint (PRD §5): single-instance flock → migrate →
startup reconcile → socket bind → tick loop.

SIGTERM/SIGINT stops the scheduler and the API but leaves workers running
(decision #8: they are re-adopted on restart; `ladd cancel` is the hard stop).
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path

from .adapters import get_adapter
from .api import ApiServer
from .backends import BackendRegistry
from .config import load_config
from .db import Database, now_iso
from .jobs import Jobs
from .mycelium import MyceliumClient
from .salad import SaladProxyManager
from .scheduler import Scheduler
from .supervisor import Supervisor

log = logging.getLogger("dispatcherd")

POLICY_PATH = Path(__file__).resolve().parent.parent / "prompts" / "worker-policy.md"


def worker_policy_text() -> str:
    try:
        return POLICY_PATH.read_text(encoding="utf-8")
    except OSError:
        log.warning("worker policy %s not found; workers get an empty policy "
                    "(run before enabling team mode)", POLICY_PATH)
        return ""


def acquire_lock(lock_path: str) -> int:
    """flock EX|NB: exit 1 with an actionable message if another daemon holds it."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        log.error("another dispatcherd is already running (lock %s is held); "
                  "exactly one daemon may run (PRD §5)", lock_path)
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd  # kept open for the process lifetime; released on exit


def sync_projects_from_config(cfg, db: Database) -> None:
    """Config is the source of topology: insert projects not yet in the DB.
    Existing rows keep their runtime state (mode, drain_policy, timestamps)
    so a restart never silently flips a project back to single mode."""
    with db.tx() as c:
        for p in cfg.projects:
            exists = c.execute("SELECT id FROM projects WHERE id=?",
                                (p.id,)).fetchone()
            if exists is not None:
                continue
            now_ts = now_iso()
            c.execute(
                """INSERT INTO projects (id, repo_path, integration_path,
                       integration_ref, mycelium_project, mode, drain_policy,
                       mode_changed_at, updated_at)
                   VALUES (?,?,?,?,?, 'single', 'drain', ?, ?)""",
                (p.id, os.path.abspath(os.path.expanduser(p.repo_path)),
                 p.integration_path, p.integration_ref, p.mycelium_project,
                 now_ts, now_ts))
            log.info("project %s synced from config (%s)", p.id, p.repo_path)


async def serve(cfg) -> None:
    cfg.paths.ensure()
    lock_fd = acquire_lock(cfg.paths.lock_path)
    db = Database(cfg.paths.db_path)
    db.migrate()
    salad = SaladProxyManager(cfg, cfg.paths)
    backends = BackendRegistry(cfg, db, salad)
    backends.sync()
    sync_projects_from_config(cfg, db)

    jobs = Jobs(db, cfg, cfg.paths, worker_policy_text())
    supervisor = Supervisor(
        db, cfg, cfg.paths,
        adapter_for=lambda profile_id: get_adapter(
            cfg.profile(profile_id).adapter))
    mycelium = (MyceliumClient(cfg.mycelium)
                if cfg.mycelium.enabled else None)
    scheduler = Scheduler(db, cfg, cfg.paths, backends, supervisor, jobs,
                          mycelium)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
        except (NotImplementedError, RuntimeError):
            # non-UNIX platform / already-running loop quirk: fall back to default
            signal.signal(sig, signal.SIG_DFL)

    await scheduler.startup_reconcile()
    api = ApiServer(db, cfg, cfg.paths, jobs, scheduler, backends, mycelium,
                    salad=salad)
    await api.start()
    await scheduler.start()
    log.info("dispatcherd ready (pid %d, socket %s)", os.getpid(),
             cfg.paths.socket_path)

    # Salad proxies come up at startup (orphan adoption by nonce covers
    # proxies from a crashed generation). A dead group is fine: the probe
    # just marks the backend offline.
    for b in cfg.backends:
        if b.kind == "salad":
            try:
                up, detail = await salad.ensure(b)
            except Exception:
                # A bad backend must never crash the daemon into a restart
                # loop; the probe reports it offline with the log detail.
                log.exception("salad proxy for %s failed to start", b.id)
                continue
            if not up:
                log.warning("salad proxy for %s not up at startup: %s",
                            b.id, detail)

    # Initial probe of worker backends so the first status is not stale.
    for b in cfg.backends:
        if not b.is_primary:
            try:
                await backends.probe(b.id, force=True)
            except Exception:
                log.exception("initial probe of %s failed", b.id)

    await stop
    log.info("stopping (workers and salad proxies left running; both are "
             "re-adopted on restart)")
    await scheduler.stop()
    await api.stop()
    if mycelium is not None:
        await mycelium.close()
    os.close(lock_fd)


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="dispatcherd",
                                 description="local coding agent dispatcher daemon")
    ap.add_argument("--config", default=None,
                    help="TOML config path (default: "
                         "~/.config/local-dispatcher/config.toml if present)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="debug logging to stderr")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config(args.config)
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
