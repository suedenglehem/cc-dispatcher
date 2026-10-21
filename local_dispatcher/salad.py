"""Salad backend support: one local proxy per salad.cloud gateway.

A salad group exposes an OpenAI-only gateway that authenticates every
request with a Salad-Api-Key header. Claude Code speaks Anthropic Messages
and cannot set that header, so the daemon runs a stdlib-only
salad_proxy.py (vendored from the user's cl_salad runner) on a local port:

    claude worker --ANTHROPIC_BASE_URL--> 127.0.0.1:<port> (salad_proxy)
        --+ Salad-Api-Key on every request--> https://<group>.salad.cloud

The USER starts/stops the Salad group themselves. The dispatcher only:
  * runs + reaps the local proxy (this module),
  * probes the gateway with the key for liveness/ctx (backends.probe),
  * keeps heartbeat-mode groups alive while a worker is actually running on
    them (heartbeat_tick — mirrors cl_salad's own keepalive),
  * registers/removes the backend in config.toml (TOML helpers below).

The key is read from a file (default /usr/local/bin/salad_api.txt) and never
printed, logged, or stored in the DB.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import BackendConfig, normalize_gateway

log = logging.getLogger("dispatcherd.salad")

SALAD_KEYFILE_DEFAULT = "/usr/local/bin/salad_api.txt"
SALAD_PROXY_PORT_BASE = 18100
SALAD_PROXY_PORT_SPAN = 100
HEARTBEAT_INTERVAL_S = 30.0
PROXY_SCRIPT = Path(__file__).resolve().parent / "salad_proxy.py"


def read_salad_key(keyfile: str) -> str:
    """Key, whitespace-stripped. Never logged. '' when unreadable/empty."""
    try:
        return Path(keyfile).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def find_salad_backend(rows: list[dict], url: str) -> dict | None:
    """The salad-kind row whose normalized endpoint equals
    normalize_gateway(url) — the `ladd -d URL` → backend id resolution.
    Raises ValueError when url itself is malformed."""
    gw = normalize_gateway(url)
    return next((b for b in rows if b.get("kind") == "salad"
                 and normalize_gateway(b.get("endpoint") or "") == gw), None)


def proxy_nonce(b: BackendConfig) -> str:
    """Stable per-backend identity for the proxy's /healthz echo.

    Stable across daemon restarts (so an orphaned proxy from a crashed
    generation is adopted, not duplicated) but changes when ANYTHING the
    proxy was spawned with changes — gateway URL, model, thinking, token
    cap, or the key itself — so a stale proxy is never adopted after a
    config edit or key rotation. Hashing the key locally is safe: only 8
    hex chars of a SHA-256 digest are used and they are never logged.
    """
    keyfile = b.salad_keyfile or SALAD_KEYFILE_DEFAULT
    try:
        key_digest = hashlib.sha256(
            Path(keyfile).read_bytes().strip()).hexdigest()[:8]
    except OSError:
        key_digest = "nokey"
    ident = "|".join((
        b.endpoint, b.model_id or "", b.model_label or "",
        "1" if b.salad_thinking else "0", str(b.salad_proxy_port), key_digest))
    digest = hashlib.md5(ident.encode("utf-8")).hexdigest()[:8]
    return f"dispatcherd-{b.id}-{b.salad_proxy_port}-{digest}"


# --------------------------------------------------------------------------- #
# proxy process manager
# --------------------------------------------------------------------------- #

class SaladProxyManager:
    """Launches/adopts/reaps one salad_proxy.py per salad backend."""

    def __init__(self, cfg, paths):
        self.cfg = cfg
        self.paths = paths
        self._procs: dict[str, subprocess.Popen] = {}
        self._last_beat: dict[str, float] = {}
        # One lock per backend: add-salad and the scheduler probe can call
        # ensure() concurrently; without this both see "not up" and spawn.
        self._locks: dict[str, asyncio.Lock] = {}

    # -- lifecycle -----------------------------------------------------------

    def _log_path(self, b: BackendConfig) -> Path:
        return Path(self.paths.run_dir) / f"salad_proxy_{b.id}.log"

    async def _healthz_nonce(self, port: int, timeout: float = 2.0) -> str | None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(f"http://127.0.0.1:{port}/healthz")
            return r.json().get("nonce", "")
        except Exception:
            return None

    def _spawn(self, b: BackendConfig, keyfile: str) -> subprocess.Popen:
        gw = normalize_gateway(b.endpoint)
        model = b.model_label or b.model_id
        cap = self.cfg.worker.max_output_tokens
        logf = open(self._log_path(b), "a", encoding="utf-8")
        args = [sys.executable, str(PROXY_SCRIPT),
                "--port", str(b.salad_proxy_port),
                "--host", "127.0.0.1",
                "--upstream", gw + "/v1/chat/completions",
                "--model", model,
                "--keyfile", keyfile,
                "--thinking", "1" if b.salad_thinking else "0",
                "--max-tokens-cap", str(cap),
                "--nonce", proxy_nonce(b)]
        if gw.startswith("http://"):
            # loopback fake gateway (tests); the proxy refuses non-https
            # upstreams otherwise
            args.append("--allow-http-upstream")
        proc = subprocess.Popen(
            args,
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group: killable at stop
            cwd=str(self.paths.root),
        )
        logf.close()
        self._procs[b.id] = proc
        return proc

    async def ensure(self, b: BackendConfig) -> tuple[bool, str]:
        """Make sure the local proxy for this backend answers with our nonce.

        Returns (up, detail). detail explains WHY it is down — it is surfaced
        in the probe result so `ladd workers` / status are actionable.
        Serialized per backend: concurrent callers never spawn duplicates.
        """
        if b.kind != "salad":
            return True, ""
        lock = self._locks.setdefault(b.id, asyncio.Lock())
        async with lock:
            return await self._ensure_locked(b)

    async def _ensure_locked(self, b: BackendConfig) -> tuple[bool, str]:
        keyfile = b.salad_keyfile or SALAD_KEYFILE_DEFAULT
        if not read_salad_key(keyfile):
            return False, f"cannot read Salad key from {keyfile}"
        port = b.salad_proxy_port
        nonce = proxy_nonce(b)

        current = await self._healthz_nonce(port)
        if current == nonce:
            proc = self._procs.get(b.id)
            if proc is not None and proc.poll() is None:
                return True, ""            # ours, alive
            # orphaned by a previous daemon generation (crash / kill -9):
            # same identity in the nonce => still valid, adopt instead of
            # duplicating. stop() can find it again by nonce when needed.
            log.info("salad backend %s: adopting orphaned proxy on 127.0.0.1:%d",
                     b.id, port)
            return True, ""

        # Nothing of ours is answering. A tracked process may still be alive
        # but hung (it owns the port and would block a second bind): kill it.
        tracked = self._procs.pop(b.id, None)
        if tracked is not None:
            if tracked.poll() is None:
                log.warning("salad backend %s: proxy unresponsive; killing it",
                            b.id)
                await asyncio.to_thread(self._terminate, tracked, b.id)
                current = await self._healthz_nonce(port)
            else:
                log.warning("salad backend %s: proxy exited (rc=%s); restarting",
                            b.id, tracked.returncode)

        if current is not None:
            if current.startswith(f"dispatcherd-{b.id}-"):
                # OUR proxy, but spawned with an older identity (model,
                # thinking, cap, or key changed). Replace it; adopting it
                # would keep using stale flags/credentials.
                log.info("salad backend %s: replacing stale proxy (identity "
                         "changed)", b.id)
                await asyncio.to_thread(self._kill_by_nonce, current)
                for _ in range(25):
                    current = await self._healthz_nonce(port)
                    if current is None:
                        break
                    await asyncio.sleep(0.2)
            if current is not None:
                return False, (f"127.0.0.1:{port} is held by another process; "
                               f"free the port or set a different salad_proxy_port")

        try:
            self._spawn(b, keyfile)
        except (ValueError, OSError) as e:
            return False, f"cannot start salad proxy: {e}"
        for _ in range(25):
            if await self._healthz_nonce(port) == nonce:
                return True, ""
            await asyncio.sleep(0.4)
        proc = self._procs.get(b.id)
        if proc is not None and proc.poll() is not None:
            rc = proc.returncode
            self._procs.pop(b.id, None)
            return False, (f"proxy exited on startup (rc={rc}); "
                           f"log: {self._log_path(b)}")
        if proc is not None:
            await asyncio.to_thread(self.stop, b.id)   # kill + drop the record
        return False, f"proxy did not come up on 127.0.0.1:{port} " \
                      f"(log: {self._log_path(b)})"

    @staticmethod
    def _find_proxy_pids(pattern: str) -> list[int]:
        """PIDs whose argv contains a nonce starting with `pattern` and runs
        salad_proxy.py.

        A full nonce is unique per backend identity; the id+port prefix also
        matches an OLD nonce left behind by a key/model change. Either way
        only our proxy matches, so nothing else is touched. Same-user /proc
        only; failures are ignored.
        """
        pids = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return pids
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    argv = [p.decode("utf-8", "surrogateescape")
                            for p in f.read().split(b"\0") if p]
            except OSError:
                continue
            if (any(a.startswith(pattern) for a in argv)
                    and any(a.endswith("salad_proxy.py") for a in argv)):
                pids.append(int(name))
        return pids

    def _kill_by_nonce(self, nonce: str) -> None:
        for pid in self._find_proxy_pids(nonce):
            self._terminate_pid(pid)

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _terminate(self, proc: subprocess.Popen, backend_id: str) -> None:
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(50):
            if proc.poll() is not None:
                return
            time.sleep(0.2)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("salad proxy for %s did not exit after SIGKILL",
                        backend_id)

    def stop(self, backend_id: str) -> None:
        """Terminate the proxy for this backend (TERM group, then KILL).

        Also reaps an orphan adopted from a previous daemon generation: the
        Popen handle is gone, but the process still carries our nonce in its
        argv, so it is found and killed by that.
        """
        self._last_beat.pop(backend_id, None)
        proc = self._procs.pop(backend_id, None)
        if proc is not None:
            self._terminate(proc, backend_id)
        b = self.cfg.backend(backend_id)
        if b is not None and b.kind == "salad":
            # id+port prefix: also finds an orphan spawned with an OLD nonce
            # (e.g. before a key rotation), which the current nonce misses.
            self._kill_by_nonce(f"dispatcherd-{b.id}-{b.salad_proxy_port}-")

    def stop_all(self) -> None:
        ids = {b.id for b in self.cfg.backends if b.kind == "salad"}
        ids.update(self._procs)
        for bid in ids:
            self.stop(bid)

    # -- keepalive ------------------------------------------------------------

    async def heartbeat_tick(self, db) -> None:
        """1-token completion per 30s to each salad gateway that has a worker
        actively running on it, so IDLE_SHUTDOWN=heartbeat groups survive the
        gaps between a worker's LLM calls (long Bash runs etc.). Real chat
        traffic already counts as activity; this is just the floor. Mirrors
        cl_salad's keepalive. Silently no-ops when the group is dead — the
        probe marks the backend offline either way."""
        import httpx
        now = time.monotonic()
        for b in self.cfg.backends:
            if b.kind != "salad" or not b.salad_heartbeat:
                continue
            row = db.query_one(
                "SELECT COUNT(*) c FROM reservations WHERE resource_id=? "
                "AND kind='backend' AND status='active'", (b.id,))
            if not row or row["c"] == 0:
                continue
            last = self._last_beat.get(b.id)
            # None = never beaten: fire immediately, even on a host whose
            # monotonic clock is younger than the interval.
            if last is not None and now - last < HEARTBEAT_INTERVAL_S:
                continue
            keyfile = b.salad_keyfile or SALAD_KEYFILE_DEFAULT
            key = read_salad_key(keyfile)
            if not key:
                continue
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        normalize_gateway(b.endpoint) + "/v1/completions",
                        headers={"Salad-Api-Key": key,
                                 "Content-Type": "application/json"},
                        json={"model": b.model_label or b.model_id,
                              "prompt": ".", "max_tokens": 1})
            except Exception as e:  # noqa: BLE001 — keepalive must not raise
                log.debug("salad heartbeat for %s failed: %s", b.id,
                          type(e).__name__)
            finally:
                # Stamp AFTER the attempt: the next beat is one interval
                # after this one finished, not after it started (a timeout
                # would otherwise stretch the gap past the idle window).
                self._last_beat[b.id] = time.monotonic()


# --------------------------------------------------------------------------- #
# config.toml editing (daemon-side: the daemon is the single writer)
# --------------------------------------------------------------------------- #

def _toml_str(s: str) -> str:
    return json.dumps(s)  # JSON basic strings are valid TOML basic strings


def salad_block_text(b: BackendConfig) -> str:
    """The canonical [[backends]] block `ladd backend add-salad` writes."""
    lines = [
        "[[backends]]",
        f"# salad backend {b.id} — managed by `ladd backend add-salad {b.id}`;",
        f"# remove with `ladd backend remove-salad {b.id}`. The daemon proxies",
        f"# it on 127.0.0.1:{b.salad_proxy_port} (key injection + Anthropic<->OpenAI).",
        f"id = {_toml_str(b.id)}",
        'kind = "salad"',
        f"endpoint = {_toml_str(normalize_gateway(b.endpoint))}",
        f"model_id = {_toml_str(b.model_id)}",
        f"model_label = {_toml_str(b.model_label or b.model_id)}",
        f"salad_proxy_port = {b.salad_proxy_port}",
    ]
    if b.salad_keyfile and b.salad_keyfile != SALAD_KEYFILE_DEFAULT:
        lines.append(f"salad_keyfile = {_toml_str(b.salad_keyfile)}")
    if b.concurrency != 1:
        lines.append(f"concurrency = {b.concurrency}")
    if b.priority != 1:
        lines.append(f"priority = {b.priority}")
    if b.resource_group:
        lines.append(f"resource_group = {_toml_str(b.resource_group)}")
    if b.salad_thinking:
        lines.append("salad_thinking = true")
    if not b.salad_heartbeat:
        lines.append("salad_heartbeat = false")
    if not b.enabled:
        lines.append("enabled = false")
    if b.notes:
        lines.append(f"notes = {_toml_str(b.notes)}")
    return "\n".join(lines)


def append_salad_backend(text: str, b: BackendConfig) -> str:
    """Append the backend's canonical block; the file's comments survive."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + salad_block_text(b) + "\n"


_TABLE_HDR = re.compile(r"^\s*\[\s*\[?\s*[A-Za-z0-9_\"'-]")  # TOML allows indentation


def _tables(lines: list[str]) -> list[list]:
    """Spans of [start, end_exclusive, header] for every top-level table."""
    starts = [(i, ln.strip()) for i, ln in enumerate(lines) if _TABLE_HDR.match(ln)]
    out = []
    for k, (i, hdr) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        out.append([i, end, hdr])
    return out


def remove_salad_backend(text: str, backend_id: str) -> str:
    """Remove the [[backends]] block with this id (managed or hand-written),
    plus the id from every [[profiles]] backend_ids list. Raises ValueError
    when the id is not found (the caller reports it actionably)."""
    lines = text.splitlines()
    id_re = re.compile(r'^\s*id\s*=\s*["\']' + re.escape(backend_id) + r'["\']')
    tables = _tables(lines)
    removed = False
    for start, end, header in list(tables):
        if not header.startswith("[[backends]]"):
            continue
        for i in range(start, end):
            if id_re.match(lines[i]):
                # drop the block plus one trailing blank line, if present
                stop = end
                if stop > start + 1 and lines[stop - 1].strip() == "":
                    stop -= 1
                del lines[start:stop]
                removed = True
                break
        if removed:
            break
    if not removed:
        raise ValueError(f"no [[backends]] entry with id {backend_id!r}")
    # strip the id from every profile's backend_ids list
    lines = _strip_from_profile_lists(lines, backend_id)
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def _array_span(lines: list[str], start: int, end: int, key: str):
    """Locate `key = [ ... ]` within [start, end).

    Returns (first_line, last_line, indent, items, comment) or None. Handles
    multi-line arrays and a trailing comment after the closing bracket —
    both valid TOML that a single-line regex silently missed.
    """
    pat = re.compile(r"^(\s*)" + re.escape(key) + r"\s*=\s*\[")
    for i in range(start, end):
        m = pat.match(lines[i])
        if not m:
            continue
        indent = m.group(1)
        # Build a comment-free copy to find the closing bracket and parse
        # items; TOML allows `# ...` comments inside a multi-line array.
        code = lines[i][m.end():].split("#", 1)[0]
        last = i
        while "]" not in code and last + 1 < end:
            last += 1
            code += "\n" + lines[last].split("#", 1)[0]
        if "]" not in code:
            return None                     # malformed: leave the file alone
        inner, _, _tail = code.partition("]")
        comment = ""
        cm = re.search(r"#.*$", lines[last])
        if cm:
            comment = "  " + cm.group(0).strip()
        items = []
        for piece in inner.replace("\n", " ").split(","):
            piece = piece.strip()
            if len(piece) >= 2 and piece[0] == piece[-1] and piece[0] in "\"'":
                piece = piece[1:-1]
            if piece:
                items.append(piece)
        return i, last, indent, items, comment
    return None


def _format_array(indent: str, items: list[str], comment: str) -> str:
    return (f"{indent}backend_ids = "
            f"[{', '.join(json.dumps(x) for x in items)}]{comment}")


def append_to_profile_backend_lists(text: str, backend_id: str,
                                    adapters: set[str] | None = None) -> str:
    """Add backend_id to the backend_ids list of every [[profiles]] entry.

    adapters: when given, only profiles whose adapter line matches (a missing
    adapter line counts as "claude"). Idempotent: ids already listed are
    skipped. A profile without a backend_ids line gets one inserted. Reverse
    table order keeps the computed spans valid across inserts.
    """
    lines = text.splitlines()
    for start, end, header in reversed(_tables(lines)):
        if not header.startswith("[[profiles]]"):
            continue
        adapter = "claude"
        for i in range(start, end):
            ma = re.match(r'^\s*adapter\s*=\s*["\']?([A-Za-z0-9_-]+)', lines[i])
            if ma:
                adapter = ma.group(1)
        if adapters is not None and adapter not in adapters:
            continue
        span = _array_span(lines, start, end, "backend_ids")
        if span is None:
            lines.insert(start + 1, f"backend_ids = [{json.dumps(backend_id)}]")
            continue
        first, last, indent, items, comment = span
        if backend_id in items:
            continue
        items.append(backend_id)
        lines[first:last + 1] = [_format_array(indent, items, comment)]
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def _strip_from_profile_lists(lines: list[str], backend_id: str) -> list[str]:
    # Reverse order: replacing a multi-line array with one line shifts all
    # later line numbers, so edit bottom-up.
    for start, end, header in reversed(_tables(lines)):
        if not header.startswith("[[profiles]]"):
            continue
        span = _array_span(lines, start, end, "backend_ids")
        if span is None:
            continue
        first, last, indent, items, comment = span
        if backend_id not in items:
            continue
        items = [x for x in items if x != backend_id]
        lines[first:last + 1] = [_format_array(indent, items, comment)]
    return lines


def allocate_salad_port(cfg) -> int | None:
    """First free port in the dispatcher-reserved salad range: not used by
    another salad backend in config, and (best effort) not bound locally."""
    used = {b.salad_proxy_port for b in cfg.backends if b.kind == "salad"}
    for port in range(SALAD_PROXY_PORT_BASE,
                      SALAD_PROXY_PORT_BASE + SALAD_PROXY_PORT_SPAN):
        if port in used:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Tolerate TIME_WAIT left by a just-removed proxy (its server sets
        # allow_reuse_address), otherwise a usable port looks occupied.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    return None
