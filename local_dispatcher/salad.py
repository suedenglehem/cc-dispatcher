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

from .config import BackendConfig

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


def normalize_gateway(url: str) -> str:
    """Normalise a gateway URL to scheme://host[:port] (path dropped).

    Mirrors cl_salad: bare host or full URL both work. For public
    salad.cloud hosts the scheme is always https (the Cloudflare front
    only proxies standard ports). An explicit http:// to a loopback host
    is preserved as-is — that is the local test-gateway path (the vendored
    proxy speaks TLS upstream, which real salad.cloud groups require).
    """
    u = url.strip()
    m = re.match(r"^(https?)://", u)
    scheme = m.group(1) if m else None
    u = re.sub(r"^https?://", "", u)
    u = u.split("/", 1)[0]
    u = u.strip()
    if not u or " " in u:
        raise ValueError(f"invalid salad gateway URL: {url!r}")
    if scheme == "http" and u.split(":", 1)[0] in ("127.0.0.1", "localhost"):
        return f"http://{u}"
    return f"https://{u}"


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
    generation is adopted, not duplicated) but changes when the gateway URL
    changes (so a stale proxy for an old group is never adopted).
    """
    digest = hashlib.md5(b.endpoint.encode("utf-8")).hexdigest()[:8]
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
        """
        if b.kind != "salad":
            return True, ""
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
            # same port + same upstream hash in the nonce => still valid,
            # adopt instead of duplicating. We cannot kill it at clean stop
            # (pid unknown), which is fine: it holds no state.
            log.info("salad backend %s: adopting orphaned proxy on 127.0.0.1:%d",
                     b.id, port)
            return True, ""

        # Not ours. If we had a tracked proc it died — reap the record.
        dead = self._procs.pop(b.id, None)
        if dead is not None and dead.poll() is not None:
            log.warning("salad backend %s: proxy exited (rc=%s); restarting",
                        b.id, dead.returncode)
        elif current is not None:
            return False, (f"127.0.0.1:{port} is held by another process; "
                           f"free the port or set a different salad_proxy_port")

        self._spawn(b, keyfile)
        for _ in range(25):
            if await self._healthz_nonce(port) == nonce:
                return True, ""
            await asyncio.sleep(0.4)
        proc = self._procs.pop(b.id, None)
        if proc is not None and proc.poll() is not None:
            return False, (f"proxy exited on startup (rc={proc.returncode}); "
                           f"log: {self._log_path(b)}")
        if proc is not None:
            self.stop(b.id)   # spawned but never identified as ours
            self._procs.pop(b.id, None)
        return False, f"proxy did not come up on 127.0.0.1:{port} " \
                      f"(log: {self._log_path(b)})"

    def stop(self, backend_id: str) -> None:
        """Terminate the proxy for this backend (TERM group, then KILL)."""
        self._last_beat.pop(backend_id, None)
        proc = self._procs.pop(backend_id, None)
        if proc is None:
            return
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
            log.warning("salad proxy for %s did not exit after SIGKILL", backend_id)

    def stop_all(self) -> None:
        for bid in list(self._procs):
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
            if now - self._last_beat.get(b.id, 0.0) < HEARTBEAT_INTERVAL_S:
                continue
            keyfile = b.salad_keyfile or SALAD_KEYFILE_DEFAULT
            key = read_salad_key(keyfile)
            if not key:
                continue
            self._last_beat[b.id] = now
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


# --------------------------------------------------------------------------- #
# config.toml editing (daemon-side: the daemon is the single writer)
# --------------------------------------------------------------------------- #

def _toml_str(s: str) -> str:
    return json.dumps(s)  # JSON basic strings are valid TOML basic strings


def _toml_bool(v: bool) -> str:
    return "true" if v else "false"


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


_TABLE_HDR = re.compile(r"^\[\S")              # [table] / [[array-of-tables]]


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


def append_to_profile_backend_lists(text: str, backend_id: str,
                                    adapters: set[str] | None = None) -> str:
    """Add backend_id to the backend_ids list of every [[profiles]] entry.

    adapters: when given, only profiles whose adapter line matches (a missing
    adapter line counts as "claude"). Idempotent: ids already listed are
    skipped. A profile without a backend_ids line gets one inserted. Reverse
    table order keeps the computed spans valid across inserts.
    """
    lines = text.splitlines()
    tables = _tables(lines)
    q = f'"{backend_id}"'
    q1 = f"'{backend_id}'"
    for start, end, header in reversed(tables):
        if not header.startswith("[[profiles]]"):
            continue
        adapter = "claude"
        bi = None
        for i in range(start, end):
            ma = re.match(r'^\s*adapter\s*=\s*["\']?([A-Za-z0-9_-]+)', lines[i])
            if ma:
                adapter = ma.group(1)
            if re.match(r"^\s*backend_ids\s*=\s*\[", lines[i]):
                bi = i
        if adapters is not None and adapter not in adapters:
            continue
        if bi is not None:
            m = re.match(r"^(\s*backend_ids\s*=\s*\[)(.*)(\])\s*$", lines[bi])
            inner = m.group(2)
            if q in inner or q1 in inner:
                continue
            items = [x.strip() for x in inner.split(",") if x.strip()]
            items.append(q)
            lines[bi] = m.group(1) + ", ".join(items) + m.group(3)
        else:
            lines.insert(start + 1, f"backend_ids = [{q}]")
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def _strip_from_profile_lists(lines: list[str], backend_id: str) -> list[str]:
    tables = _tables(lines)
    q = f'"{backend_id}"'
    q1 = f"'{backend_id}'"
    for start, end, header in tables:
        if not header.startswith("[[profiles]]"):
            continue
        for i in range(start, end):
            m = re.match(r"^(\s*backend_ids\s*=\s*\[)(.*)(\])\s*$", lines[i])
            if not m:
                continue
            inner = m.group(2)
            if q in inner:
                inner = inner.replace(q, "")
            elif q1 in inner:
                inner = inner.replace(q1, "")
            items = [x.strip() for x in inner.split(",") if x.strip()]
            lines[i] = m.group(1) + ", ".join(items) + m.group(3)
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
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    return None
