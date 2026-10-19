"""Integration — runtime salad backend add/remove against a fake
salad.cloud gateway (in-process daemon; the REAL salad_proxy.py subprocess
is started by SaladProxyManager, so its lifecycle, nonce and orphan
adoption are exercised end-to-end. The proxy's TLS upstream path against a
real group is covered by the live smoke, see docs/commands.md)."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from local_dispatcher.models import ApiError, ErrorCodes
from local_dispatcher.salad import SaladProxyManager, proxy_nonce
from tests.fakes.fake_salad_gateway import FakeSaladGateway
from tests.helpers import wait_for, wait_job


def _make_env(env, key: str = "test-salad-key",
              ctx_max: int = 32768) -> FakeSaladGateway:
    gw = FakeSaladGateway(key=key, ctx_max=ctx_max).start()
    env.root.mkdir(parents=True, exist_ok=True)
    kf = env.root / "salad_key.txt"
    kf.write_text(key + "\n", encoding="utf-8")
    os.chmod(kf, 0o600)
    env._salad_keyfile = kf          # type: ignore[attr-defined]
    env._fakes.append(gw)            # env teardown stops the gateway
    return gw


# -- add / probe lifecycle ------------------------------------------------------

def test_add_while_group_stopped(env):
    gw = _make_env(env)
    gw.stop_group()                     # user has the group stopped
    h = env.daemon(backends=[])
    r = h.client().backend_add_salad(gw.gateway_url, id="sg1",
                                     keyfile=str(env._salad_keyfile))
    # registration succeeds even with a dead group; the proxy is up
    assert r["health"] == "offline"
    assert r["offline_detail"] == "HTTP 503"
    assert r["proxy_up"] is True
    assert r["key_rejected"] is False
    row = h.query("SELECT * FROM backends WHERE id=?", ("sg1",))[0]
    assert row["kind"] == "salad" and row["enabled"] == 1
    # config.toml was rewritten and the live config reloaded in place
    text = (env.root / "config.toml").read_text(encoding="utf-8")
    assert 'id = "sg1"' in text
    b = h.cfg.backend("sg1")
    assert b is not None and b.kind == "salad"
    assert "sg1" in h.cfg.profile("w").backend_ids   # claude profile
    # the daemon's proxy answers with this backend's nonce
    assert asyncio.run(h.salad._healthz_nonce(row["salad_proxy_port"])) \
        == proxy_nonce(b)   # noqa: SLF001 (test seam)


def test_group_lifecycle_flips_health(env):
    gw = _make_env(env)                  # group running
    h = env.daemon(backends=[])
    r = h.client().backend_add_salad(gw.gateway_url, id="sg2",
                                     keyfile=str(env._salad_keyfile))
    assert r["health"] == "online"
    row = h.query("SELECT * FROM backends WHERE id=?", ("sg2",))[0]
    assert row["ctx_max"] == int(32768 * 0.90)      # 90% convention
    # user stops the group → next refresh marks it offline
    gw.stop_group()
    h.client().workers(refresh=True)
    row = h.query("SELECT * FROM backends WHERE id=?", ("sg2",))[0]
    assert row["health"] == "offline"
    # user starts it again → online with ctx
    gw.start_group()
    h.client().workers(refresh=True)
    row = h.query("SELECT * FROM backends WHERE id=?", ("sg2",))[0]
    assert row["health"] == "online"
    assert row["ctx_max"] == int(32768 * 0.90)


def test_key_rejected_is_reported_not_fatal(env):
    gw = _make_env(env, key="the-right-key")
    kf = env._salad_keyfile
    kf.write_text("wrong-key\n", encoding="utf-8")   # stale/wrong keyfile
    h = env.daemon(backends=[])
    r = h.client().backend_add_salad(gw.gateway_url, id="sg3",
                                     keyfile=str(kf))
    assert r["health"] == "offline"
    assert r["key_rejected"] is True
    assert r["offline_detail"] == "HTTP 403"


def test_add_error_paths(env):
    gw = _make_env(env)
    h = env.daemon(backends=[])
    c = h.client()
    c.backend_add_salad(gw.gateway_url, id="sg4",
                        keyfile=str(env._salad_keyfile))
    with pytest.raises(ApiError) as e:
        c.backend_add_salad(gw.gateway_url, id="sg4",
                            keyfile=str(env._salad_keyfile))
    assert e.value.code == ErrorCodes.SALAD_ID_EXISTS
    missing = str(env.root / "no_key.txt")
    with pytest.raises(ApiError) as e:
        c.backend_add_salad(gw.gateway_url, id="sg5", keyfile=missing)
    assert e.value.code == ErrorCodes.SALAD_KEYFILE_MISSING
    with pytest.raises(ApiError) as e:
        c.backend_add_salad("not a url at all", id="sg6",
                            keyfile=str(env._salad_keyfile))
    assert e.value.code == ErrorCodes.SALAD_URL_INVALID


# -- jobs on salad backends -------------------------------------------------------

def test_job_runs_on_salad_backend(env):
    gw = _make_env(env)
    h = env.daemon(backends=[])
    c = h.client()
    c.set_mode("team", project_id="p1")
    r = c.backend_add_salad(gw.gateway_url, id="sg7",
                            keyfile=str(env._salad_keyfile))
    assert r["health"] == "online"
    job = c.submit("p1", objective="tiny task", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    view = wait_job(c, job, "succeeded")
    assert view["failure_class"] is None
    att = h.query("SELECT * FROM attempts WHERE job_id=?", (job,))[0]
    assert att["backend_id"] == "sg7"
    assert att["status"] == "succeeded"


def test_heartbeat_beats_only_with_workers(env):
    gw = _make_env(env)
    h = env.daemon(backends=[], worker_mode="hold",
                   profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "30"})])
    c = h.client()
    c.set_mode("team", project_id="p1")
    c.backend_add_salad(gw.gateway_url, id="sg8",
                        keyfile=str(env._salad_keyfile))
    time.sleep(1.0)      # several scheduler ticks, no reservation
    assert gw.completions == 0
    # a worker running on the salad backend → the tick-path keepalive beats
    job = c.submit("p1", objective="hold", base_commit=env.repo_head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    wait_for(lambda: gw.completions >= 1, 15, 0.1,
             "salad heartbeat while worker running")
    c.cancel(job)
    wait_job(c, job, "cancelled")


# -- remove --------------------------------------------------------------------

def test_remove_salad_backend(env):
    gw = _make_env(env)
    h = env.daemon(backends=[])
    c = h.client()
    c.backend_add_salad(gw.gateway_url, id="sg9",
                        keyfile=str(env._salad_keyfile))
    port = h.query("SELECT salad_proxy_port FROM backends WHERE id='sg9'")[0] \
        ["salad_proxy_port"]
    r = c.backend_remove_salad("sg9")
    assert r["removed"] is True
    # config no longer mentions it (block + profile list entry)
    text = (env.root / "config.toml").read_text(encoding="utf-8")
    assert "sg9" not in text
    assert h.cfg.backend("sg9") is None
    # history rows are either gone (unreferenced) or tombstoned
    rows = h.query("SELECT * FROM backends WHERE id=?", ("sg9",))
    if rows:
        assert rows[0]["enabled"] == 0 and rows[0]["manually_reserved"] == 1
    # the proxy process was reaped
    assert asyncio.run(h.salad._healthz_nonce(port)) is None  # noqa: SLF001
    with pytest.raises(ApiError) as e:
        c.backend_remove_salad("nope")
    assert e.value.code == ErrorCodes.SALAD_NOT_FOUND


# -- orphan adoption ------------------------------------------------------------

def test_orphan_proxy_adopted_not_duplicated(env):
    gw = _make_env(env)
    h = env.daemon(backends=[])
    h.client().backend_add_salad(gw.gateway_url, id="sga",
                                 keyfile=str(env._salad_keyfile))
    b = h.cfg.backend("sga")
    # a fresh daemon generation does not know the process — it must adopt
    # the orphan by nonce, not spawn a second proxy
    mgr2 = SaladProxyManager(h.cfg, h.cfg.paths)
    up, detail = asyncio.run(mgr2.ensure(b))
    assert up is True, detail
    assert mgr2._procs == {}                       # noqa: SLF001
    # and the original process is still the one answering healthz
    assert asyncio.run(mgr2._healthz_nonce(b.salad_proxy_port)) \
        == proxy_nonce(b)   # noqa: SLF001


# -- CLI shorthand -----------------------------------------------------------------

def test_cli_shorthand_add_delete(env, capsys, monkeypatch):
    import local_dispatcher.api as api_mod
    gw = _make_env(env)
    monkeypatch.setattr(api_mod, "SALAD_KEYFILE_DEFAULT",
                        str(env._salad_keyfile))
    h = env.daemon(backends=[])
    from local_dispatcher.cli import main
    cfg_path = str(env.root / "config.toml")

    rc = main(["--config", cfg_path, "-a", gw.gateway_url])
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "added salad backend" in out
    # id derived from the loopback host's first dot-label
    sid = gw.gateway_url.split("://", 1)[1].split(":", 1)[0].split(".", 1)[0]
    assert h.cfg.backend(sid) is not None

    rc = main(["--config", cfg_path, "-d", gw.gateway_url])
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert f"removed salad backend {sid}" in out
    assert h.cfg.backend(sid) is None

    # -d on an unknown URL is an actionable error
    rc = main(["--config", cfg_path, "-d", "https://unknown.salad.cloud"])
    assert rc == 1
    assert "salad_not_found" in capsys.readouterr().err


# -- streaming failure surfacing (regression: job j_8bf0ff2baf) -------------------
#
# Live failure mode: the group answered 200, but the stream ended with zero
# content (edge/origin kill mid-flight, an in-band error payload, or a
# zero-token completion). The proxy used to render that as a clean empty
# Anthropic "success" — the CLI recorded an empty result and the dispatcher
# logged invalid_result without retrying. Such streams must now produce one
# `error` event so the CLI fails loudly and the adapter classifies it as a
# retryable backend disconnect. The REAL salad_proxy.py subprocess is driven
# here against the fake gateway's SSE modes.

import http.client
import json as _json
import signal
import socket as _socket
import subprocess as _subprocess
import sys as _sys

from local_dispatcher.salad import PROXY_SCRIPT

_REGRESS_NONCE = "regress-streaming"


def _free_port() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_proxy(gw, keyfile, port):
    args = [_sys.executable, str(PROXY_SCRIPT),
            "--port", str(port),
            "--host", "127.0.0.1",
            "--upstream", gw.gateway_url + "/v1/chat/completions",
            "--model", "qwen",
            "--keyfile", str(keyfile),
            "--thinking", "0",
            "--nonce", _REGRESS_NONCE,
            "--allow-http-upstream"]
    return _subprocess.Popen(args, stdout=_subprocess.DEVNULL,
                             stderr=_subprocess.DEVNULL,
                             start_new_session=True)


def _wait_proxy(port, nonce, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/healthz")
            body = _json.loads(conn.getresponse().read())
            conn.close()
            if body.get("nonce") == nonce:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError("proxy did not come up on %d" % port)


def _stream_from_proxy(port):
    """One streaming POST /v1/messages; returns (status, event names, body)."""
    body = _json.dumps({
        "model": "qwen", "max_tokens": 64, "stream": True,
        "messages": [{"role": "user", "content": "hi"}]}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/v1/messages", body=body, headers={
        "content-type": "application/json",
        "anthropic-version": "2023-06-01"})
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    events = [ln.split(":", 1)[1].strip()
              for ln in raw.splitlines() if ln.startswith("event: ")]
    return resp.status, events, raw


def _text_deltas(raw):
    """Reassembled text_delta payloads of one SSE stream.
    (The fake answers with two content chunks, so "fake-ok" arrives as two
    separate text_delta frames and never appears contiguously in raw.)"""
    out = []
    for ln in raw.splitlines():
        if ln.startswith("data: ") and '"text_delta"' in ln:
            out.append(_json.loads(ln[len("data: "):])
                       .get("delta", {}).get("text", ""))
    return "".join(out)


@pytest.mark.parametrize("mode,expect", [
    ("stream", "ok"),
    ("stream_empty", "empty"),
    ("stream_error_payload", "error"),
])
def test_stream_failure_surfaced(env, mode, expect):
    gw = _make_env(env)
    gw.chat_mode = mode
    port = _free_port()
    proc = _spawn_proxy(gw, env._salad_keyfile, port)
    try:
        _wait_proxy(port, _REGRESS_NONCE)
        status, events, raw = _stream_from_proxy(port)
        assert status == 200
        if expect == "ok":
            # normal stream: unchanged, clean finish with content
            assert "message_stop" in events
            assert "error" not in events
            assert _text_deltas(raw) == "fake-ok"
        else:
            # contentless/anomalous stream: one error event, no clean finish
            assert "error" in events
            assert "message_stop" not in events
            assert "upstream stream produced no content" in raw
            if expect == "error":
                # the in-band upstream error text is surfaced in the detail
                assert "upstream exploded" in raw
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
        proc.wait(timeout=10)
