"""Unit tests — migrations, event pruning, procfs identity/liveness, and the
API response guarantees (credential redaction + 8k cap)."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from local_dispatcher.api import ApiServer
from local_dispatcher.db import Database
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
