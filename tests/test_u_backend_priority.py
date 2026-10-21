"""Backend priority (default 1): config parse, persistence, pick order."""

from __future__ import annotations

import pytest

from local_dispatcher.backends import BackendRegistry
from local_dispatcher.config import (BackendConfig, Config, DaemonConfig,
                                     MyceliumConfig, WorkerConfig, load_config)
from local_dispatcher.db import Database, now_iso


def _cfg(priorities: list[int]) -> Config:
    bs = [BackendConfig(id=f"b{i}", endpoint=f"http://127.0.0.1:{9100 + i}",
                        model_id="m", model_label="m", priority=p)
          for i, p in enumerate(priorities)]
    return Config(paths=None, daemon=DaemonConfig(), worker=WorkerConfig(),
                  mycelium=MyceliumConfig(), backends=bs, groups=[],
                  profiles=[], projects=[])


def _online(tmp_path, priorities: list[int]) -> BackendRegistry:
    db = Database(tmp_path / "t.db")
    db.migrate()
    reg = BackendRegistry(_cfg(priorities), db)
    reg.sync()
    for i in range(len(priorities)):
        db.execute("UPDATE backends SET health='online', probed_at=? WHERE id=?",
                   (now_iso(), f"b{i}"))
    return reg


def test_pick_backend_higher_priority_first(tmp_path):
    reg = _online(tmp_path, [1, 5, 2])
    try:
        assert reg.pick_backend(["b0", "b1", "b2"]) == "b1"
        reg.db.execute("UPDATE backends SET health='offline' WHERE id='b1'")
        assert reg.pick_backend(["b0", "b1", "b2"]) == "b2"
        reg.db.execute("UPDATE backends SET health='offline' WHERE id='b2'")
        assert reg.pick_backend(["b0", "b1", "b2"]) == "b0"
    finally:
        reg.db.close()


def test_pick_backend_default_priority_keeps_declared_order(tmp_path):
    reg = _online(tmp_path, [1, 1, 1])      # all default
    try:
        assert reg.pick_backend(["b2", "b0", "b1"]) == "b2"   # declared order
        reg.db.execute("UPDATE backends SET health='offline' WHERE id='b2'")
        assert reg.pick_backend(["b2", "b0", "b1"]) == "b0"
    finally:
        reg.db.close()


def test_pick_backend_preferred_beats_priority(tmp_path):
    reg = _online(tmp_path, [1, 9])
    try:
        assert reg.pick_backend(["b0", "b1"], preferred="b0") == "b0"
        assert reg.pick_backend(["b0", "b1"], preferred="b1") == "b1"
    finally:
        reg.db.close()


def test_sync_persists_priority(tmp_path):
    reg = _online(tmp_path, [1, 7])
    try:
        rows = {r["id"]: r["priority"] for r in
                reg.db.query("SELECT id, priority FROM backends")}
        assert rows == {"b0": 1, "b1": 7}
        states = {w["id"]: w["priority"] for w in reg.worker_availability()}
        assert states == {"b0": 1, "b1": 7}
    finally:
        reg.db.close()


def test_config_priority_parse(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[backends]]\nid = "lo"\nendpoint = "http://127.0.0.1:9200"\n'
        '[[backends]]\nid = "hi"\nendpoint = "http://127.0.0.1:9201"\n'
        'priority = 4\n',
        encoding="utf-8")
    cfg = load_config(p)
    assert cfg.backend("lo").priority == 1        # default
    assert cfg.backend("hi").priority == 4


def test_config_rejects_low_priority(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[[backends]]\nid = "x"\nendpoint = "http://127.0.0.1:9202"\n'
                 'priority = 0\n', encoding="utf-8")
    with pytest.raises(ValueError, match="priority must be >= 1"):
        load_config(p)
