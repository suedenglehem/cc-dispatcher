"""Unit tests — migrations, event pruning, procfs identity/liveness, and the
API response guarantees (credential redaction + 8k cap)."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from local_dispatcher.api import ApiServer
from local_dispatcher.config import BackendConfig, Config, ProfileConfig
from local_dispatcher.db import Database
from local_dispatcher.jobs import Jobs
from local_dispatcher.models import ApiError
from local_dispatcher import procfs


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


# -- migrations --------------------------------------------------------------

def test_migrations_idempotent(tmp_path):
    d = Database(tmp_path / "t.db")
    v1 = d.migrate()
    v2 = d.migrate()
    assert v1 == v2 == 1
    tables = {r["name"] for r in d.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("projects", "backends", "resource_groups", "worker_profiles",
              "jobs", "attempts", "reservations", "events", "schema_version"):
        assert t in tables
    d.close()


# -- event pruning -------------------------------------------------------------

def test_prune_events_keeps_newest(db):
    for i in range(100):
        db.event("job", f"j{i:03d}", "tick", {"i": i})
    assert db.query("SELECT COUNT(*) c FROM events")[0]["c"] == 100
    assert db.prune_events(max_rows=50) == 50
    rows = db.query("SELECT subject_id FROM events ORDER BY id DESC")
    assert len(rows) == 50
    # newest (j099…) survived, oldest (j000…) were dropped
    assert rows[0]["subject_id"] == "j099"
    assert all(r["subject_id"] >= "j050" for r in rows)


def test_prune_events_noop_under_limit(db):
    for i in range(3):
        db.event("job", f"j{i}", "tick")
    assert db.prune_events(max_rows=50) == 0


# -- procfs identity ------------------------------------------------------------

def test_identity_roundtrip():
    pid = os.getpid()
    ident = procfs.make_identity(pid)
    p, g, st = procfs.parse_identity(ident)
    assert (p, g) == (pid, pid)
    assert st is not None
    assert procfs.identity_matches(pid, pid, ident)
    assert not procfs.identity_matches(pid + 1, pid, ident)
    assert not procfs.identity_matches(pid, pid + 1, ident)


def test_identity_unknown_starttime_still_matches_pid():
    # '?' starttime means "recorded but unreadable" → trust pid+pgid only
    assert procfs.parse_identity("42:42:?") == (42, 42, None)
    assert procfs.parse_identity("garbage") == (0, 0, None)
    assert procfs.identity_matches(42, 42, "42:42:?")


def test_pid_alive():
    assert procfs.pid_alive(os.getpid())
    assert not procfs.pid_alive(2 ** 22)


def test_group_alive():
    assert procfs.group_alive(os.getpgrp())
    assert not procfs.group_alive(0)
    # a dead session's group: spawn our own, reap it, then check
    p = subprocess.Popen(["sleep", "5"], start_new_session=True)
    pgid = p.pid
    p.terminate()
    p.wait()
    assert not procfs.group_alive(pgid)
    assert procfs.pgid_of(os.getpid()) == os.getpgrp()
    assert procfs.pgid_of(2 ** 22) is None


# -- API response guarantees -----------------------------------------------------

def _api_server(db: Database, cap: int = 8000) -> ApiServer:
    srv = object.__new__(ApiServer)
    srv.db = db
    srv.cfg = SimpleNamespace(daemon=SimpleNamespace(
        response_cap_chars=cap))
    return srv


def _insert_backend_secret(db: Database, secret: str):
    db.execute(
        "INSERT INTO backends (id, endpoint, model_id, credential_env) "
        "VALUES ('b1', 'http://x', 'm', ?)",
        (json.dumps({"ANTHROPIC_API_KEY": secret}),))


def test_shape_redacts_credential_values(db):
    _insert_backend_secret(db, "sk-secret-value-12345678")
    srv = _api_server(db)
    out = srv._shape({"note": "the key is sk-secret-value-12345678"})
    assert out["note"] == "the key is [REDACTED]"
    # short values are never treated as secrets (false positives)
    db.execute("DELETE FROM backends WHERE id='b1'")
    _insert_backend_secret(db, "ab")
    out2 = _api_server(db)._shape({"note": "ab"})
    assert out2["note"] == "ab"


def test_shape_enforces_response_cap(db):
    srv = _api_server(db, cap=8000)
    assert srv._shape({"ok": "x" * 100}) == {"ok": "x" * 100}
    with pytest.raises(ApiError) as ei:
        srv._shape({"big": "x" * 9000})
    assert "response too large" in ei.value.args[0]


def test_shape_keeps_json_valid_after_redaction(db):
    # a secret appearing as a JSON key's value survives as valid JSON
    _insert_backend_secret(db, "tok-0123456789")
    srv = _api_server(db)
    out = srv._shape({"items": [{"k": "tok-0123456789"}]})
    assert out["items"][0]["k"] == "[REDACTED]"


def _submit_api_server(db: Database) -> ApiServer:
    """An ApiServer wired to a real Jobs + Config whose `profiles`/`backends`
    are live pydantic/dataclass objects — the exact shape that used to leak a
    non-serializable object into an ApiError `known=` list and crash _send."""
    cfg = Config(
        paths=SimpleNamespace(), daemon=SimpleNamespace(response_cap_chars=8000),
        worker=SimpleNamespace(), mycelium=SimpleNamespace(),
        backends=[BackendConfig(id="b1", endpoint="http://x")], groups=[],
        profiles=[ProfileConfig(id="good-profile")], projects=[])
    jobs = Jobs(db, cfg, SimpleNamespace(), "worker policy")
    srv = object.__new__(ApiServer)
    srv.db = db
    srv.cfg = cfg
    srv.jobs = jobs
    return srv


def _submit_params(profile_id, backend_id=None):
    p = {"project_id": "demo", "objective": "x",
         "base_commit": "abcdef0123456789abcdef0123456789abcdef01",
         "allowed_scope": ["src/"], "profile_id": profile_id}
    if backend_id is not None:
        p["backend_id"] = backend_id
    return p


async def test_submit_unknown_profile_error_is_json_serializable(db):
    # Regression: PROFILE_UNKNOWN used to pass live ProfileConfig objects in
    # `known=`, so the ApiError response crashed json.dumps in _send and the
    # connection was dropped with no reply (daemon survived, no job row).
    db.execute("INSERT INTO projects (id, repo_path, integration_path, mode) "
               "VALUES ('demo', '/tmp/x', '', 'team')")
    srv = _submit_api_server(db)
    resp = await srv._dispatch({"method": "submit",
                                "params": _submit_params("nope")})
    wire = json.dumps(resp)  # this is the line that used to raise TypeError
    assert resp["ok"] is False
    assert resp["error"]["code"] == "profile_unknown"
    known = resp["error"]["detail"]["known"]
    assert known == ["good-profile"]
    assert "good-profile" in wire


async def test_submit_unknown_backend_error_is_json_serializable(db):
    # Same class of bug on the BACKEND_UNKNOWN branch (live BackendConfig objs).
    db.execute("INSERT INTO projects (id, repo_path, integration_path, mode) "
               "VALUES ('demo', '/tmp/x', '', 'team')")
    srv = _submit_api_server(db)
    resp = await srv._dispatch({"method": "submit",
                                "params": _submit_params("good-profile",
                                                         "nope-backend")})
    json.dumps(resp)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "backend_unknown"
    assert resp["error"]["detail"]["known"] == ["b1"]
