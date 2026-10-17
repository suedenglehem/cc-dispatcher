"""P4: the cline adapter drives a worker through the real daemon.

E2E: build (argv packet, --auto-approve true, --data-dir isolation,
-t budget, no plan-mode -p) → spawn → stream → RESULT_PATH result file →
succeeded. The "cline" under test is fake_worker.py with
FAKE_PROMPT_FROM=argv, which speaks cline's positional-prompt contract;
the fake reads RESULT_PATH out of the argv packet, so a valid result
file is the proof the packet actually arrived via argv. The REAL
model-level smoke is gated on the user freeing a backend + matching
Cline's stored model entry — docs/cline-status.md, per PRD §9 never faked.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import subprocess
import types

from local_dispatcher.adapters.base import StreamInfo
from local_dispatcher.adapters.cline import ClineAdapter
from local_dispatcher.models import FailureClass
from tests.conftest import FAKE_WORKER
from tests.helpers import wait_job


def test_cline_adapter_end_to_end(env):
    env.backend()  # b1
    profiles = [dict(id="cline-w", adapter="cline",
                     executable=FAKE_WORKER,
                     backend_ids=["b1"], deadline_s=300, max_attempts=1,
                     env_overrides={
                         "FAKE_PROMPT_FROM": "argv",
                         "FAKE_WORKER_MODE": "normal",
                         "FAKE_LAUNCH_LOG": str(env.root / "launch-c.log"),
                     })]
    h = env.daemon(profiles=profiles)
    c = h.client()
    c.set_mode("team", project_id="p1")
    jid = c.submit("p1", objective="cline adapter end-to-end",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="cline-w")["job_id"]
    v = wait_job(c, jid, "succeeded", timeout=60)
    assert v["attempt_count"] == 1
    assert v["failure_class"] is None
    att = h.query("SELECT * FROM attempts WHERE job_id=?", (jid,))[0]
    assert att["status"] == "succeeded"
    assert att["backend_id"] == "b1"
    assert (env.root / "launch-c.log").exists()


# -- build() contract --------------------------------------------------------

def _cfg_ns(root="/state/x", deadline_s=1800) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        worker=types.SimpleNamespace(deadline_s=deadline_s,
                                     anthropic_api_key="local"),
        paths=types.SimpleNamespace(root=root))


def _profile_row(**kw) -> dict:
    base = {"executable": "/usr/local/bin/ccline",
            "args": "[]", "env_overrides": "{}", "deadline_s": 1800}
    base.update(kw)
    return base


def _backend_ns(**kw) -> types.SimpleNamespace:
    base = {"endpoint": "http://127.0.0.1:8080", "openai_base_url": "",
            "credential_env": {}, "model_id": "swift-1.5-iq3_xxs"}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _build(job, **kw):
    return ClineAdapter().build(
        cfg=_cfg_ns(), profile_row=_profile_row(**kw),
        backend_cfg=_backend_ns(), backend_row={},
        job=job, attempt_no=1, worktree="/wt/jc1",
        artifact_dir="/art/jc1/1",
        packet=job.get("packet", "TASK PACKET\nRESULT_PATH: /a/result.json"),
        mycelium_mcp_json="{}")


def test_cline_build_contract():
    packet = "TASK PACKET\nRESULT_PATH: /a/result.json"
    launch = _build({"id": "jc1", "deadline_expires_at": None,
                     "packet": packet})
    cmd = launch.cmd
    assert launch.packet_via == "argv"
    assert cmd[0] == "/usr/local/bin/ccline"   # the baseUrl-seeding launcher
    assert cmd[-1] == packet                      # positional prompt
    assert "-p" not in cmd and "--plan" not in cmd  # -p is PLAN mode
    assert cmd[cmd.index("--auto-approve") + 1] == "true"
    assert cmd[cmd.index("--data-dir") + 1] == "/art/jc1/1/cline-data"
    assert cmd[cmd.index("--cwd") + 1] == "/wt/jc1"
    assert launch.cwd == "/wt/jc1"
    assert cmd[cmd.index("--model") + 1] == "swift-1.5-iq3_xxs"
    # no known budget → effective profile deadline
    assert int(cmd[cmd.index("-t") + 1]) == 1800
    env = launch.env
    assert env["OPENAI_API_BASE"] == "http://127.0.0.1:8080/v1"
    assert env["OPENAI_API_KEY"] == "local"
    assert env["DISPATCHER_STATE_DIR"] == "/state/x"
    assert env["DISPATCHER_ATTEMPT_ID"] == "jc1-a1"


def test_cline_build_timeout_is_remaining_budget():
    exp = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(seconds=3600)
           ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    launch = _build({"id": "jc1", "deadline_expires_at": exp})
    t = int(launch.cmd[launch.cmd.index("-t") + 1])
    assert 3500 <= t <= 3600


# -- classify() ---------------------------------------------------------------

def _info(text: str = "", result=None) -> StreamInfo:
    return StreamInfo(raw_text=[text], result=result)


def test_cline_classify():
    ad = ClineAdapter()
    assert ad.classify(exit_code=0, info=_info(), ctx_limit=None) is None
    assert ad.classify(exit_code=0, info=_info(result={"is_error": True}),
                       ctx_limit=None) is not None
    assert ad.classify(exit_code=1,
                       info=_info("ran into a token limit"),
                       ctx_limit=None) == FailureClass.CONTEXT_OVERFLOW.value
    assert ad.classify(exit_code=1,
                       info=_info("fetch failed: ECONNREFUSED 127.0.0.1:8080"),
                       ctx_limit=None) == FailureClass.BACKEND_DISCONNECT.value
    assert ad.classify(exit_code=1, info=_info("some other crash"),
                       ctx_limit=None) == FailureClass.CRASH.value


# -- the ccline launcher (bin/ccline) ----------------------------------------
# Runs the REPO copy of the wrapper against a fake `cline` shim on PATH:
# the shim records every invocation, so the test proves the wrapper seeds
# the provider config (baseUrl/model/key/data-dir) BEFORE firing cline with
# the unchanged original argv.

REPO_CCLINE = pathlib.Path(__file__).resolve().parent.parent / "bin" / "ccline"

_FAKE_CLINE = r"""#!/usr/bin/env bash
{ printf '%s\0' "$@"; printf '\x01'; } >> "$CLINE_FAKE_LOG"
if [ "${1:-}" = "auth" ]; then
    d=""
    for ((i=1; i<$#; i++)); do
        [ "${!i}" = "--data-dir" ] && d="${!((i+1))}"
    done
    mkdir -p "$d/settings"
    echo '{"version":1}' > "$d/settings/providers.json"
    echo "Provider configured: openai-compatible (fake)"
    exit 0
fi
echo '{"type":"result","ok":true}'
exit 0
"""


def _run_ccline(tmp_path: pathlib.Path, argv: list[str], extra_env: dict,
                drop_env: tuple[str, ...] = ()):
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    fake = shim / "cline"
    fake.write_text(_FAKE_CLINE)
    fake.chmod(0o755)
    env = dict(os.environ)
    for k in drop_env:
        env.pop(k, None)
    env.update(extra_env)
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    return subprocess.run([str(REPO_CCLINE), *argv], env=env,
                          capture_output=True, text=True, timeout=30)


def _calls(tmp_path: pathlib.Path) -> list[list[str]]:
    raw = (tmp_path / "calls.log").read_bytes()
    calls = []
    for c in raw.split(b"\x01"):
        if not c:
            continue
        if c.endswith(b"\0"):       # every record ends with a trailing NUL
            c = c[:-1]
        calls.append(c.split(b"\0"))
    return calls


def _argv_of(call: list[str]) -> list[str]:
    return [x.decode() for x in call]


def test_ccline_seeds_base_url_then_execs(tmp_path):
    env = {"OPENAI_API_BASE": "http://127.0.0.1:8080/v1",
           "OPENAI_API_KEY": "local",
           "CLINE_FAKE_LOG": str(tmp_path / "calls.log")}
    data_dir = str(tmp_path / "data")
    argv = ["--json", "--cwd", "/tmp", "--auto-approve", "true",
            "--data-dir", data_dir,
            "--provider", "openai-compatible",
            "--model", "swift-1.5-iq3_xxs", "say hi"]
    r = _run_ccline(tmp_path, argv, env)
    assert r.returncode == 0, r.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 2, "expected exactly auth + run"
    auth = _argv_of(calls[0])
    assert auth[0] == "auth" and auth[1] == "openai-compatible"
    assert auth[auth.index("-b") + 1] == "http://127.0.0.1:8080/v1"
    assert auth[auth.index("-m") + 1] == "swift-1.5-iq3_xxs"
    assert auth[auth.index("-k") + 1] == "local"
    assert auth[auth.index("--data-dir") + 1] == data_dir
    run = _argv_of(calls[1])
    assert run == argv, "original argv must reach cline unchanged"
    # the auth banner must not pollute the worker's JSON stdout stream
    assert "Provider configured" not in r.stdout
    assert (tmp_path / "data" / "ccline-auth.log").exists()


def test_ccline_requires_base_url_and_datadir(tmp_path):
    base = {"OPENAI_API_KEY": "local",
            "CLINE_FAKE_LOG": str(tmp_path / "calls.log")}
    argv = ["--data-dir", str(tmp_path / "d"), "--model", "m", "hi"]
    r = _run_ccline(tmp_path, argv, base, drop_env=("OPENAI_API_BASE",))
    assert r.returncode == 2 and "OPENAI_API_BASE" in r.stderr
    r = _run_ccline(tmp_path, ["--model", "m", "hi"], base)
    assert r.returncode == 2 and "data-dir" in r.stderr
    # no key set -> auth runs WITHOUT -k, still succeeds
    argv2 = ["--data-dir", str(tmp_path / "d2"), "--model", "m", "hi"]
    env2 = {"OPENAI_API_BASE": "http://127.0.0.1:8080/v1",
            "CLINE_FAKE_LOG": str(tmp_path / "calls.log")}
    r = _run_ccline(tmp_path, argv2, env2, drop_env=("OPENAI_API_KEY",))
    assert r.returncode == 0, r.stderr
    auth = _argv_of(_calls(tmp_path)[0])
    assert "-k" not in auth
