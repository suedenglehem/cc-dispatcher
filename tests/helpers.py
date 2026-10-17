"""Small sync helpers shared by the acceptance tests."""

from __future__ import annotations

import json
import os
import time

MYCELIUM_BIN = os.path.abspath(
    os.path.expanduser("~/.venvs/mycelium/bin/mycelium"))


def make_mycelium_store(root) -> dict:
    """Temp mycelium config whose [storage]/[foundry] paths all live under
    `root` — the user's ~/.mycelium is never touched (a14-a16 isolation).
    Returns {"cfg", "env", "command"}: a TOML path, the env dict for a
    daemon [mycelium.env] or worker env_overrides, and the argv to spawn."""
    root = os.fspath(root)
    store = os.path.join(root, "mycelium-store")
    os.makedirs(store, exist_ok=True)
    cfg_path = os.path.join(store, "mycelium.toml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "[storage]\n"
            f'db_path = "{os.path.join(store, "memory.db")}"\n'
            f'backup_dir = "{os.path.join(store, "backups")}"\n'
            "[foundry]\n"
            f'log_dir = "{os.path.join(store, "foundry-logs")}"\n'
            f'db_path = "{os.path.join(store, "foundry.db")}"\n')
    return {"cfg": cfg_path,
            "env": {"MYCELIUM_CONFIG": cfg_path},
            "command": [MYCELIUM_BIN, "serve"]}


def wait_for(cond, timeout: float = 25.0, interval: float = 0.1,
             message: str = "condition"):
    """Poll cond() until it returns a truthy value; return that value.

    Raises AssertionError with `message` after `timeout` seconds."""
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = cond()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message!r} (last: {last!r})")


def wait_job(client, job_id: str, statuses, timeout: float = 25.0):
    """Wait until job `job_id` reaches one of `statuses`; return its view."""
    target = {statuses} if isinstance(statuses, (list, tuple, set, frozenset)) else {statuses}

    def _check():
        v = client.job(job_id)
        return v if v["status"] in target else None

    return wait_for(_check, timeout, 0.1, f"job {job_id} status in {sorted(target)}")


def wait_attempts(handle, job_id: str, count: int, timeout: float = 25.0) -> list[dict]:
    """Wait until `count` attempt rows exist for the job; return them."""
    return wait_for(
        lambda: (rows := handle.query(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_no",
            (job_id,))) and (rows if len(rows) >= count else None),
        timeout, 0.1, f"{count} attempts for {job_id}")


def read_launch_log(path: str | os.PathLike) -> list[dict]:
    """Parse the fake worker's FAKE_LAUNCH_LOG JSONL (the no-overlap /
    no-duplication oracle)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def active_reservations(handle) -> list[dict]:
    return handle.query("SELECT * FROM reservations WHERE status='active'")


def launch_intervals(path: str | os.PathLike) -> dict[str, tuple]:
    """Map attempt_id -> (start_ts, end_ts) from the launch log.
    end_ts is None if the process never logged its end phase (killed)."""
    entries = read_launch_log(path)
    starts = {e["attempt"]: e["ts"] for e in entries if e["phase"] == "start"}
    ends = {e["attempt"]: e["ts"] for e in entries if e["phase"] == "end"}
    return {a: (s, ends.get(a)) for a, s in starts.items()}


def intervals_overlap(a: tuple, b: tuple) -> bool:
    (s1, e1), (s2, e2) = a, b
    return e1 is not None and e2 is not None and s1 < e2 and s2 < e1
