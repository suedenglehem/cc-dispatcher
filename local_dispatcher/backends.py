"""Backend registry: probes, health, resource groups, capacity.

Probes are cheap GET /v1/models calls (the same endpoint the user's own
runner scripts probe at start-up) cached for probe_ttl_s. No
generation-heavy probes. ctx_limit follows the claude-ctx.sh convention:
90% of the probed maximum (vLLM max_model_len, llama.cpp meta.n_ctx),
overridable per backend via ctx_override.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import BackendConfig, Config, derive_max_context
from .db import Database, now_iso
from .salad import SALAD_KEYFILE_DEFAULT, read_salad_key


def extract_ctx_max(body: dict) -> int | None:
    """Pull the model context maximum out of /v1/models shapes.

    vLLM: data[0].max_model_len ; llama.cpp: data[0].meta.n_ctx
    (meta may nest under data[0].meta or data[0] directly).
    """
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return None
    entry = data[0]
    for key in ("max_model_len", "max_context_length", "context_length"):
        v = entry.get(key)
        if isinstance(v, int) and v > 0:
            return v
    meta = entry.get("meta") or {}
    for key in ("n_ctx", "max_model_len", "max_context_length"):
        v = meta.get(key)
        if isinstance(v, int) and v > 0:
            return v
    return None


@dataclass
class ProbeResult:
    health: str          # online | offline
    ctx_max: int | None
    model_ids: list[str]
    error: str = ""


class BackendRegistry:
    def __init__(self, cfg: Config, db: Database, salad=None):
        self.cfg = cfg
        self.db = db
        self.salad = salad            # SaladProxyManager | None
        self._last_probe: dict[str, float] = {}
        self._last_result: dict[str, ProbeResult] = {}

    # -- sync --------------------------------------------------------------
    def sync(self) -> None:
        """Push config backends/groups into the DB (config is the source of truth
        for topology; the DB tracks live health/probe state)."""
        with self.db.tx() as conn:
            for g in self.cfg.groups:
                conn.execute(
                    "INSERT INTO resource_groups(id, max_concurrency) VALUES (?,?) "
                    "ON CONFLICT(id) DO UPDATE SET max_concurrency=excluded.max_concurrency",
                    (g.id, g.max_concurrency),
                )
            for b in self.cfg.backends:
                conn.execute(
                    """INSERT INTO backends(id, endpoint, openai_base_url, model_id, model_label,
                       credential_env, enabled, is_primary, manually_reserved, reserved_note,
                       concurrency, resource_group, notes, kind, salad_keyfile,
                       salad_proxy_port, salad_thinking, salad_heartbeat)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint,
                       openai_base_url=excluded.openai_base_url,
                       model_id=excluded.model_id, model_label=excluded.model_label,
                       enabled=excluded.enabled, is_primary=excluded.is_primary,
                       manually_reserved=excluded.manually_reserved,
                       reserved_note=excluded.reserved_note,
                       concurrency=excluded.concurrency, resource_group=excluded.resource_group,
                       notes=excluded.notes, kind=excluded.kind,
                       salad_keyfile=excluded.salad_keyfile,
                       salad_proxy_port=excluded.salad_proxy_port,
                       salad_thinking=excluded.salad_thinking,
                       salad_heartbeat=excluded.salad_heartbeat""",
                    (b.id, b.endpoint, b.openai_base_url, b.model_id, b.model_label,
                     _json(b.credential_env), b.enabled, b.is_primary, b.manually_reserved,
                     b.reserved_note, b.concurrency, b.resource_group or None, b.notes,
                     b.kind, b.salad_keyfile or None, b.salad_proxy_port or None,
                     b.salad_thinking, b.salad_heartbeat),
                )
            # Remove backends no longer in config. A config rename is a
            # remove + add, and historical rows (jobs/attempts) may still
            # reference the old id — those keep it as an inert tombstone
            # (disabled + reserved, never scheduled) so the foreign keys
            # stay resolvable instead of the delete crashing the daemon.
            ids = [b.id for b in self.cfg.backends]
            if ids:
                placeholders = ",".join("?" * len(ids))
                removed = conn.execute(
                    f"SELECT id FROM backends WHERE id NOT IN ({placeholders})",
                    tuple(ids)).fetchall()
            else:
                # config with no backends: every DB row is stale
                # (NOT IN () is invalid SQL, hence the branch)
                removed = conn.execute("SELECT id FROM backends").fetchall()
            for (rid,) in removed:
                referenced = (
                    conn.execute(
                        "SELECT 1 FROM jobs WHERE backend_id = ? LIMIT 1",
                        (rid,)).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM attempts WHERE backend_id = ? LIMIT 1",
                        (rid,)).fetchone())
                if referenced:
                    conn.execute(
                        "UPDATE backends SET enabled = 0, manually_reserved = 1, "
                        "reserved_note = 'removed from config; tombstone for history' "
                        "WHERE id = ?", (rid,))
                else:
                    conn.execute("DELETE FROM backends WHERE id = ?", (rid,))
            for p in self.cfg.profiles:
                conn.execute(
                    """INSERT INTO worker_profiles(id, adapter, executable, args, env_overrides,
                       backend_ids, allowed_tools, deadline_s, max_attempts, output_limit_chars,
                       allow_test_repair)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET adapter=excluded.adapter,
                       executable=excluded.executable, args=excluded.args,
                       env_overrides=excluded.env_overrides, backend_ids=excluded.backend_ids,
                       allowed_tools=excluded.allowed_tools, deadline_s=excluded.deadline_s,
                       max_attempts=excluded.max_attempts,
                       output_limit_chars=excluded.output_limit_chars,
                       allow_test_repair=excluded.allow_test_repair""",
                    (p.id, p.adapter, self.cfg.effective_executable(p), _json(p.args),
                     _json(p.env_overrides), _json(p.backend_ids),
                     _json(self.cfg.effective_allowed_tools(p)),
                     self.cfg.effective_deadline(p), self.cfg.effective_max_attempts(p),
                     self.cfg.effective_output_limit(p), p.allow_test_repair),
                )
            for pr in self.cfg.projects:
                conn.execute(
                    """INSERT INTO projects(id, repo_path, integration_path, integration_ref,
                       mycelium_project)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET repo_path=excluded.repo_path,
                       integration_path=excluded.integration_path,
                       integration_ref=excluded.integration_ref,
                       mycelium_project=excluded.mycelium_project""",
                    (pr.id, pr.repo_path, pr.integration_path, pr.integration_ref,
                     pr.mycelium_project),
                )

    # -- probes ------------------------------------------------------------
    async def probe(self, backend_id: str, force: bool = False) -> ProbeResult:
        now = time.monotonic()
        cached = self._last_probe.get(backend_id)
        if not force and cached is not None and now - cached < self.cfg.daemon.probe_ttl_s \
                and backend_id in self._last_result:
            return self._last_result[backend_id]
        b = self.cfg.backend(backend_id)
        if b is None:
            return ProbeResult("offline", None, [], "unknown backend")
        if b.kind == "salad":
            if self.salad is None:
                self._record(backend_id, now, ProbeResult(
                    "offline", None, [], "salad proxy manager unavailable"))
                return self._last_result[backend_id]
            # The local proxy is the liveness precondition: without it the
            # gateway is unreachable for workers no matter what it serves.
            up, detail = await self.salad.ensure(b)
            if not up:
                self._record(backend_id, now,
                             ProbeResult("offline", None, [], detail))
                return self._last_result[backend_id]
            keyfile = b.salad_keyfile or SALAD_KEYFILE_DEFAULT
            key = read_salad_key(keyfile)
            headers = {"Salad-Api-Key": key} if key else None
            res = await _probe_endpoint(b.endpoint,
                                        self.cfg.daemon.probe_timeout_s,
                                        headers=headers)
            if res.ctx_max is None:
                res.ctx_max = await self._salad_ctx_via_proxy(b)
        else:
            res = await _probe_endpoint(b.endpoint,
                                        self.cfg.daemon.probe_timeout_s)
        if b.ctx_override:
            res.ctx_max = b.ctx_override
        self._record(backend_id, now, res)
        return res

    def _record(self, backend_id: str, now: float, res: ProbeResult) -> None:
        self._last_probe[backend_id] = now
        self._last_result[backend_id] = res
        ctx = derive_max_context(res.ctx_max, self.cfg.worker.max_context_pct) if res.ctx_max else None
        self.db.execute(
            "UPDATE backends SET health=?, probed_at=?, ctx_max=? WHERE id=?",
            (res.health, now_iso(), ctx, backend_id),
        )

    async def _salad_ctx_via_proxy(self, b: BackendConfig) -> int | None:
        """llama.cpp /props through the proxy's keyed passthrough: the
        /v1/models shape is OpenAI-shaped and does not always carry n_ctx."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(
                    f"http://127.0.0.1:{b.salad_proxy_port}/__upstream/props")
            body = r.json()
            n = (body.get("default_generation_settings") or {}).get("n_ctx")
            if isinstance(n, int) and n > 0:
                return n
        except Exception:  # noqa: BLE001 — ctx is best-effort
            pass
        return None

    def health_row(self, backend_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM backends WHERE id=?", (backend_id,))
        return dict(row) if row else None

    # -- capacity ----------------------------------------------------------
    def active_reservations(self, backend_id: str) -> int:
        r = self.db.query_one(
            "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='backend' AND status='active'",
            (backend_id,))
        return r["c"] if r else 0

    def quarantined_reservations(self, backend_id: str) -> int:
        r = self.db.query_one(
            "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='backend' AND status='quarantined'",
            (backend_id,))
        return r["c"] if r else 0

    def group_active(self, group_id: str) -> int:
        r = self.db.query_one(
            "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='group' AND status IN ('active','quarantined')",
            (group_id,))
        return r["c"] if r else 0

    def backend_free(self, conn, b: BackendConfig) -> bool:
        """Capacity check inside a launch transaction (counts include quarantine)."""
        row = conn.execute(
            "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='backend' "
            "AND status IN ('active','quarantined')", (b.id,)).fetchone()
        if row["c"] >= b.concurrency:
            return False
        if b.resource_group:
            g = conn.execute(
                "SELECT max_concurrency m FROM resource_groups WHERE id=?",
                (b.resource_group,)).fetchone()
            if g:
                used = conn.execute(
                    "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='group' "
                    "AND status IN ('active','quarantined')", (b.resource_group,)).fetchone()
                if used["c"] >= g["m"]:
                    return False
        return True

    def pick_backend(self, profile_backend_ids: list[str], preferred: str | None = None) -> str | None:
        """First eligible backend: enabled, not primary, not manually reserved,
        online with a fresh probe, with free capacity. Preferred id first, then
        config order."""
        order = list(profile_backend_ids)
        if preferred and preferred in order:
            order.remove(preferred)
            order.insert(0, preferred)
        for bid in order:
            b = self.cfg.backend(bid)
            if b is None or b.is_primary or not b.enabled or b.manually_reserved:
                continue
            row = self.db.query_one("SELECT * FROM backends WHERE id=?", (bid,))
            if row is None:
                continue
            if row["health"] != "online":
                continue
            probed = row["probed_at"]
            if probed:
                age = _age_s(probed)
                if age is not None and age > self.cfg.daemon.probe_ttl_s:
                    continue
            if not self._free_outside_tx(b):
                continue
            return bid
        return None

    def _free_outside_tx(self, b: BackendConfig) -> bool:
        row = self.db.query_one(
            "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='backend' "
            "AND status IN ('active','quarantined')", (b.id,))
        if row["c"] >= b.concurrency:
            return False
        if b.resource_group:
            g = self.db.query_one(
                "SELECT max_concurrency m FROM resource_groups WHERE id=?", (b.resource_group,))
            if g:
                used = self.db.query_one(
                    "SELECT COUNT(*) c FROM reservations WHERE resource_id=? AND kind='group' "
                    "AND status IN ('active','quarantined')", (b.resource_group,))
                if used["c"] >= g["m"]:
                    return False
        return True

    def worker_availability(self) -> list[dict]:
        """Compact per-backend state for dispatch_workers."""
        out = []
        for b in self.cfg.backends:
            row = self.db.query_one("SELECT * FROM backends WHERE id=?", (b.id,))
            if b.is_primary:
                state = "primary-reserved"
            elif not b.enabled:
                state = "disabled"
            elif b.manually_reserved:
                state = f"reserved{':' + b.reserved_note if b.reserved_note else ''}"
            elif row and row["health"] == "online":
                state = "busy" if self._free_outside_tx(b) is False else "available"
            elif row:
                state = "offline"
            else:
                state = "unknown"
            entry = {
                "id": b.id, "endpoint": b.endpoint, "state": state,
                "model": b.model_id, "concurrency": b.concurrency,
                "group": b.resource_group or None,
                "ctx_limit": row["ctx_max"] if row else None,
                "health": row["health"] if row else "unknown",
                "is_primary": b.is_primary,
            }
            if b.kind == "salad":
                entry["kind"] = "salad"
                entry["proxy"] = f"127.0.0.1:{b.salad_proxy_port}"
            out.append(entry)
        return out


async def _probe_endpoint(endpoint: str, timeout_s: float,
                          headers: dict | None = None) -> ProbeResult:
    url = endpoint.rstrip("/") + "/v1/models"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(url, headers=headers or {})
        if r.status_code != 200:
            return ProbeResult("offline", None, [], f"HTTP {r.status_code}")
        body = r.json()
        ctx = extract_ctx_max(body)
        ids = [m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)]
        return ProbeResult("online", ctx, ids)
    except Exception as e:  # noqa: BLE001 — probe must never raise into the tick
        return ProbeResult("offline", None, [], type(e).__name__)


def _age_s(iso: str) -> float | None:
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return None


def _json(obj) -> str:
    import json
    return json.dumps(obj)
