"""Long-lived `mycelium serve` stdio MCP client (PRD §12).

One subprocess for the daemon's lifetime, spawned lazily on first use.
Spoken as plain newline-delimited JSON-RPC 2.0 (the MCP stdio transport)
rather than via the mcp SDK so the child's pid is visible (acceptance
test 15 kills it mid-run) and teardown is explicit.

Lifetime-1-respawn: the first death is healed by one respawn; a second
death marks the client permanently unavailable. Outage semantics (PRD §12):
packet rendering degrades to no memory section (partial results kept), and
`ensure()` is the gate for memory_required jobs.

Workers are separate: each gets its OWN `mycelium serve` via --mcp-config
(adapters/claude.py) — this client is the daemon's (primary-side) handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from .config import MyceliumConfig

log = logging.getLogger("dispatcherd.mycelium")

_PROTOCOL_VERSION = "2025-06-18"


class MyceliumClient:
    def __init__(self, myc: MyceliumConfig):
        self.myc = myc
        self._lock = asyncio.Lock()          # single-flight
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []
        self._next_id = 0
        self._pending: asyncio.Future | None = None
        self._spawn_count = 0
        self._unavailable = False

    # -- status ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """Cheap snapshot (no spawn). For authoritative state use ensure()."""
        return not self._unavailable

    @property
    def pid(self) -> int | None:
        p = self._proc
        if p is not None and p.returncode is None:
            return p.pid
        return None

    # -- lifecycle ----------------------------------------------------------------
    async def ensure(self) -> bool:
        """Spawn lazily (respawn-once) and report readiness. Never raises."""
        async with self._lock:
            return await self._ensure_locked()

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self._fail_pending()
        for t in (self._reader_task, self._stderr_task):
            if t is not None:
                t.cancel()
        self._reader_task = self._stderr_task = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    # -- API -----------------------------------------------------------------------
    async def memory_section(self, project_row: dict, job_row: dict) -> str | None:
        """Bounded recall at packet render time: ≤N queries, partial-OK on a
        mid-section outage. Returns None when nothing could be recalled."""
        key = project_row.get("mycelium_project") or project_row["id"]
        try:
            queries = json.loads(job_row.get("memory_queries") or "[]")
        except json.JSONDecodeError:
            queries = []
        if not queries:
            return None
        chunks: list[str] = []
        for q in queries[:self.myc.max_recall_queries]:
            r = await self._call("recall", {
                "query": str(q), "project": key, "limit": self.myc.recall_limit})
            if r is None:
                break  # outage mid-section: keep what we have (PRD §12)
            chunks.append(r)
        if not chunks:
            return None
        return "\n".join(chunks)[:self.myc.memory_section_cap_chars]

    async def save(self, content: str, project: str, *, pinned: bool = False,
                   confidence: float = 0.3) -> bool:
        """One provisional memory (promote / decisions). Never raises."""
        return (await self._call("save", {
            "content": content, "project": project,
            "pinned": pinned, "confidence": confidence})) is not None

    # -- internals -------------------------------------------------------------------
    async def _ensure_locked(self) -> bool:
        if self._unavailable:
            return False
        if self._proc is not None and self._proc.returncode is None:
            return True  # assume alive; an in-flight call detects loss
        if self._spawn_count >= 2:
            # lifetime-1-respawn exhausted
            self._unavailable = True
            log.warning("mycelium dead twice; permanently unavailable "
                        "(stderr: %s)", " | ".join(self._stderr_tail[-3:]))
            return False
        return await self._spawn_locked()

    async def _spawn_locked(self) -> bool:
        old, self._proc = self._proc, None
        if old is not None and old.returncode is None:
            try:
                old.kill()
            except ProcessLookupError:
                pass
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.myc.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.myc.env})
        except (OSError, FileNotFoundError) as e:
            self._proc = None
            self._unavailable = True
            log.error("mycelium spawn failed (%s): %s",
                      self.myc.command[0], e)
            return False
        self._spawn_count += 1
        self._reader_task = asyncio.get_running_loop().create_task(
            self._read_loop(), name="mycelium-reader")
        self._stderr_task = asyncio.get_running_loop().create_task(
            self._stderr_loop(), name="mycelium-stderr")
        # MCP handshake: initialize → notifications/initialized
        self._pending = asyncio.get_running_loop().create_future()
        self._send({"jsonrpc": "2.0", "id": self._next_msg_id(),
                    "method": "initialize",
                    "params": {"protocolVersion": _PROTOCOL_VERSION,
                               "capabilities": {},
                               "clientInfo": {"name": "dispatcherd",
                                              "version": "1"}}})
        init = await self._await_response(timeout=self.myc.call_timeout_s)
        self._pending = None
        if init is None or "result" not in init:
            log.error("mycelium handshake failed (stderr: %s)",
                      " | ".join(self._stderr_tail[-3:]))
            await self._kill_child()
            return False
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        log.info("mycelium ready (pid %d, spawn #%d)",
                 self._proc.pid, self._spawn_count)
        return True

    def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(obj).encode("utf-8") + b"\n")

    def _next_msg_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _fail_pending(self) -> None:
        p, self._pending = self._pending, None
        if p is not None and not p.done():
            p.set_result(None)

    async def _await_response(self, timeout: float) -> dict | None:
        """Wait for the single in-flight response. None on timeout/loss."""
        p = self._pending
        assert p is not None
        try:
            return await asyncio.wait_for(p, timeout)
        except asyncio.TimeoutError:
            return None

    async def _call(self, tool: str, arguments: dict) -> str | None:
        """One tools/call. Returns the text result, or None on any failure
        (outage, timeout, error response). Never raises."""
        async with self._lock:
            if not await self._ensure_locked():
                return None
            self._pending = asyncio.get_running_loop().create_future()
            mid = self._next_msg_id()
            try:
                self._send({"jsonrpc": "2.0", "id": mid,
                            "method": "tools/call",
                            "params": {"name": tool, "arguments": arguments}})
            except (BrokenPipeError, ConnectionResetError, OSError):
                self._fail_pending()
                await self._kill_child()
                return None
            resp = await self._await_response(timeout=self.myc.call_timeout_s)
            self._fail_pending()
        if resp is None:
            # timeout or child died mid-call: kill so the next call respawns
            await self._kill_child()
            return None
        if resp.get("id") != mid or "result" not in resp:
            return None
        result = resp["result"]
        if result.get("isError"):
            log.warning("mycelium %s returned an error: %s", tool, result)
            return None
        for block in result.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                return block["text"]
        return None

    async def _kill_child(self) -> None:
        """Mark the current child dead (it is dead or wedged) and reap it."""
        proc, self._proc = self._proc, None
        self._fail_pending()
        for t in (self._reader_task, self._stderr_task):
            if t is not None:
                t.cancel()
        self._reader_task = self._stderr_task = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass

    async def _read_loop(self) -> None:
        assert self._proc is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break  # EOF: child died
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict) or "id" not in msg:
                    continue  # notification
                fut = self._pending
                if (fut is not None and not fut.done()
                        and msg.get("id") == self._next_id):
                    fut.set_result(msg)
        except (ValueError, RuntimeError):
            pass  # stream closed under us
        finally:
            self._fail_pending()

    async def _stderr_loop(self) -> None:
        assert self._proc is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())
                if len(self._stderr_tail) > 20:
                    self._stderr_tail.pop(0)
        except (ValueError, RuntimeError):
            pass
