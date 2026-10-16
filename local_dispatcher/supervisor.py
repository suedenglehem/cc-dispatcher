"""Process supervision: spawn in a new session, stream output, reap,
kill the whole process group, and re-adopt orphans after a restart.

Every worker runs in its own session (start_new_session) so pgid == pid and
the whole tree dies with `killpg`. The marker env (DISPATCHER=1,
DISPATCHER_JOB_ID, DISPATCHER_ATTEMPT_ID) is set by the adapter and is how
startup reconciliation finds orphans.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field

from .adapters.base import StreamInfo, WorkerAdapter, WorkerLaunch
from .db import Database
from .procfs import group_alive, identity_matches, kill_group, make_identity, pgid_of, pid_alive

log = logging.getLogger("dispatcherd.supervisor")


@dataclass
class Child:
    attempt_id: str
    job_id: str
    profile_id: str
    pid: int
    pgid: int
    identity: str
    proc: asyncio.subprocess.Process | None   # None = re-adopted (detached)
    info: StreamInfo = field(default_factory=StreamInfo)
    stream_task: asyncio.Task | None = None
    killed: bool = False
    deadline_deadline: float | None = None    # time.time() budget, set by watchdog


class Supervisor:
    def __init__(self, db: Database, cfg, paths, adapter_for):
        self.db = db
        self.cfg = cfg
        self.paths = paths
        self.adapter_for = adapter_for
        self.children: dict[str, Child] = {}

    # -- spawn ---------------------------------------------------------------
    async def spawn(self, *, launch: WorkerLaunch, attempt_id: str, job_id: str,
                    profile_id: str, started_at: str) -> Child:
        adapter = self.adapter_for(profile_id)
        via = launch.packet_via
        stdin = asyncio.subprocess.PIPE if via == "stdin" else asyncio.subprocess.DEVNULL
        proc = await asyncio.create_subprocess_exec(
            *launch.cmd,
            cwd=launch.cwd,
            env=launch.env,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        if via == "stdin":
            assert proc.stdin is not None
            proc.stdin.write(launch.packet.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        child = Child(
            attempt_id=attempt_id, job_id=job_id, profile_id=profile_id,
            pid=proc.pid, pgid=proc.pid, identity=make_identity(proc.pid),
            proc=proc,
        )
        self.children[attempt_id] = child
        child.stream_task = asyncio.create_task(
            self._stream(child, adapter), name=f"stream-{attempt_id}")
        log.info("spawned %s pid=%d", attempt_id, proc.pid)
        return child

    async def _stream(self, child: Child, adapter: WorkerAdapter) -> None:
        log_path = None
        try:
            d = self.paths.artifact_dir(child.job_id, _attempt_no_from_id(child.attempt_id))
            os.makedirs(d, exist_ok=True)
            log_path = os.path.join(d, "stream.log")
        except OSError:
            log_path = None
        fh = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None
        try:
            assert child.proc is not None and child.proc.stdout is not None
            async for raw in child.proc.stdout:
                line = raw.decode("utf-8", errors="replace")
                if fh is not None:
                    fh.write(line)
                    fh.flush()
                try:
                    adapter.parse_line(line, child.info)
                except Exception:
                    log.exception("parse_line failed for %s", child.attempt_id)
        finally:
            if fh is not None:
                fh.close()

    # -- re-adoption / detached tracking --------------------------------------
    def attach(self, *, attempt_id: str, job_id: str, profile_id: str,
               pid: int, pgid: int, identity: str) -> Child:
        """Track a live worker the daemon did not spawn (restart adoption).
        Its stream is already gone — liveness/kill go through procfs."""
        child = Child(attempt_id=attempt_id, job_id=job_id, profile_id=profile_id,
                      pid=pid, pgid=pgid, identity=identity, proc=None)
        self.children[attempt_id] = child
        log.info("re-adopted %s pid=%d (detached, no stream)", attempt_id, pid)
        return child

    def alive(self, child: Child) -> bool:
        if child.proc is not None:
            return child.proc.returncode is None
        return pid_alive(child.pid)

    def exited(self, child: Child) -> int | None:
        """Exit code if the (spawned) process has exited, else None."""
        if child.proc is not None:
            return child.proc.returncode
        if not pid_alive(child.pid):
            return None  # caller treats as exit_unknown
        return None

    async def wait(self, child: Child, timeout: float | None = None) -> int | None:
        if child.proc is None:
            return None
        try:
            return await asyncio.wait_for(child.proc.wait(), timeout)
        except asyncio.TimeoutError:
            return None

    # -- termination -----------------------------------------------------------
    async def kill_group(self, child: Child) -> bool:
        """SIGTERM → grace → SIGKILL. Returns True when the whole group is
        confirmed dead (the only state in which reservations may be released)."""
        grace = self.cfg.kill_grace_s
        confirm = self.cfg.kill_confirm_s
        if not group_alive(child.pgid):
            return True
        kill_group(child.pgid, signal.SIGTERM)
        child.killed = True
        await self._wait_group_dead(child.pgid, grace)
        if not group_alive(child.pgid):
            await asyncio.sleep(min(confirm, 2.0))
            return not group_alive(child.pgid)
        log.warning("group %d of %s survived SIGTERM; SIGKILL", child.pgid, child.attempt_id)
        kill_group(child.pgid, signal.SIGKILL)
        await self._wait_group_dead(child.pgid, 2.0)
        await asyncio.sleep(min(confirm, 2.0))
        return not group_alive(child.pgid)

    async def _wait_group_dead(self, pgid: int, deadline_s: float) -> None:
        end = time.monotonic() + deadline_s
        while time.monotonic() < end and group_alive(pgid):
            await asyncio.sleep(0.2)

    def forget(self, attempt_id: str) -> None:
        self.children.pop(attempt_id, None)


def _attempt_no_from_id(attempt_id: str) -> int:
    # attempt ids are "<job_id>-a<n>"
    try:
        return int(attempt_id.rsplit("-a", 1)[1])
    except (ValueError, IndexError):
        return 1


def group_dead_quiet(pgid: int) -> bool:
    return not group_alive(pgid)
