"""Unit: marker ownership — the /proc marker scan is global, but the
orphan-kill must be scoped to the instance's own state dir (P3 step 5,
the "known footgun"). marker_owned() is the pure decision; the scan must
report the DISPATCHER_STATE_DIR tag it decides on."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from local_dispatcher.procfs import scan_marker_processes
from local_dispatcher.scheduler import marker_owned


def test_marker_owned_truth_table():
    own, other = "/state/a", "/state/b"
    # tagged worker: the tag decides, the db row is irrelevant
    assert marker_owned(own, own, True) is True
    assert marker_owned(own, own, False) is True
    assert marker_owned(other, own, True) is False
    assert marker_owned(other, own, False) is False
    # legacy worker (no tag, pre-upgrade): only an attempt row in OUR db
    # proves ownership (rows are never deleted)
    assert marker_owned("", own, True) is True
    assert marker_owned("", own, False) is False


def _spawn_marker(attempt_id: str, state_dir: str | None) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "DISPATCHER": "1",
        "DISPATCHER_JOB_ID": "jscope",
        "DISPATCHER_ATTEMPT_ID": attempt_id,
    })
    if state_dir is not None:
        env["DISPATCHER_STATE_DIR"] = state_dir
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_scan_reports_state_dir(tmp_path):
    tagged = _spawn_marker("jscope-a1", str(tmp_path))
    legacy = _spawn_marker("jscope-a2", None)
    procs = [tagged, legacy]
    try:
        time.sleep(0.2)  # let the children reach exec
        found = scan_marker_processes()
        assert found["jscope-a1"]["state_dir"] == str(tmp_path)
        assert found["jscope-a1"]["pid"] == tagged.pid
        assert found["jscope-a2"]["state_dir"] == ""  # legacy: empty tag
        assert found["jscope-a2"]["pid"] == legacy.pid
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=10)
