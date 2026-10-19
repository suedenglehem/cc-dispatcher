"""Config backend removal must not crash sync (FK integrity).

Regression: renaming a backend id in config (remove + add) used to hit
`DELETE FROM backends WHERE id NOT IN (...)` while jobs/attempts rows
still referenced the old id -> sqlite3.IntegrityError at daemon start.
A removed backend that history references stays as an inert tombstone
(disabled + reserved, never scheduled); an unreferenced one is deleted.
"""
from local_dispatcher.backends import BackendRegistry
from local_dispatcher.config import load_config
from local_dispatcher.db import Database

from tests.helpers import wait_job


def _resync(env, handle, keep_specs):
    """Rebuild a config holding only keep_specs and run sync() against the
    running instance's db — the exact shape the daemon takes at next start.
    Own Database object: sqlite connections are thread-bound, and the test
    thread is not the daemon's."""
    profiles = [dict(id="w",
                     backend_ids=[b["id"] for b in keep_specs],
                     deadline_s=600, max_attempts=1)]
    path = env._write_toml(keep_specs, [], profiles, [env.project()])
    cfg2 = load_config(path)
    db2 = Database(cfg2.paths.db_path)
    db2.migrate()
    BackendRegistry(cfg2, db2).sync()
    db2.close()


def test_removed_referenced_backend_becomes_tombstone(env):
    old = env.backend("old")
    keep = env.backend("keep")
    h = env.daemon(backends=[old, keep],
                   profiles=[dict(id="w", backend_ids=["old"])])
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="reference the old backend",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]
    wait_job(c, jid, "succeeded", timeout=60)
    att = h.query("SELECT backend_id FROM attempts WHERE job_id=?", (jid,))[0]
    assert att["backend_id"] == "old"

    # The config "rename": old disappears, only keep remains. This call
    # raised sqlite3.IntegrityError before the tombstone fix.
    _resync(env, h, [keep])

    rows = {r["id"]: r for r in h.query("SELECT * FROM backends")}
    assert "old" in rows, "referenced backend must stay as a tombstone"
    t = rows["old"]
    assert t["enabled"] == 0 and t["manually_reserved"] == 1
    assert "tombstone" in (t["reserved_note"] or "")
    assert rows["keep"]["enabled"] == 1


def test_removed_unreferenced_backend_is_deleted(env):
    a = env.backend("a")
    b = env.backend("b")
    h = env.daemon(backends=[a, b])
    _resync(env, h, [a])
    ids = {r["id"] for r in h.query("SELECT id FROM backends")}
    assert ids == {"a"}


def _bare_cfg(*backends):
    from local_dispatcher.config import (Config, DaemonConfig,
                                         MyceliumConfig, WorkerConfig)
    return Config(paths=None, daemon=DaemonConfig(), worker=WorkerConfig(),
                  mycelium=MyceliumConfig(), backends=list(backends),
                  groups=[], profiles=[], projects=[])


def test_sync_preserve_runtime_keeps_backend_set(tmp_path):
    """A runtime salad add/remove must not undo `ladd backend set`: that
    path calls sync(preserve_runtime=True). A daemon start keeps the old
    behavior (config reasserts itself)."""
    from local_dispatcher.config import BackendConfig
    b = BackendConfig(id="b1", endpoint="http://127.0.0.1:1234")
    cfg = _bare_cfg(b)
    db = Database(tmp_path / "t.db")
    db.migrate()
    BackendRegistry(cfg, db).sync()
    db.execute("UPDATE backends SET enabled=0, manually_reserved=1, "
               "reserved_note='manual' WHERE id='b1'")

    BackendRegistry(cfg, db).sync(preserve_runtime=True)
    row = db.query_one("SELECT * FROM backends WHERE id='b1'")
    assert (row["enabled"], row["manually_reserved"],
            row["reserved_note"]) == (0, 1, "manual")

    BackendRegistry(cfg, db).sync()      # startup: config wins again
    row = db.query_one("SELECT * FROM backends WHERE id='b1'")
    assert (row["enabled"], row["manually_reserved"]) == (1, 0)
    db.close()


def test_sync_empty_config_does_not_wipe_backends(tmp_path):
    """A mistyped --config that loads zero backends must not tombstone or
    delete the live state's rows."""
    from local_dispatcher.config import BackendConfig
    a = BackendConfig(id="a", endpoint="http://127.0.0.1:1")
    db = Database(tmp_path / "t.db")
    db.migrate()
    BackendRegistry(_bare_cfg(a), db).sync()
    BackendRegistry(_bare_cfg(), db).sync()
    assert {r["id"] for r in db.query("SELECT id FROM backends")} == {"a"}
    db.close()
