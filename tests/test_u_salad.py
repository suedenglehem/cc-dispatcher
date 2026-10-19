"""Unit tests — salad URL normalization, port allocation, proxy nonce,
TOML backend/profile-list editing, and the -d URL→row resolution."""

from __future__ import annotations

import json
import tomllib

import pytest

from local_dispatcher.config import (BackendConfig, Config, DaemonConfig,
                                     MyceliumConfig, WorkerConfig,
                                     load_config, worker_endpoint)
from local_dispatcher.salad import (SALAD_PROXY_PORT_BASE,
                                    allocate_salad_port, append_salad_backend,
                                    append_to_profile_backend_lists,
                                    find_salad_backend, normalize_gateway,
                                    proxy_nonce, remove_salad_backend)
from local_dispatcher.salad_proxy import EmptyThinkStripper, StreamBuilder

# -- URL normalization ----------------------------------------------------------

def test_normalize_gateway_forms():
    assert normalize_gateway("tamarind-x.salad.cloud") == \
        "https://tamarind-x.salad.cloud"
    assert normalize_gateway("https://tamarind-x.salad.cloud") == \
        "https://tamarind-x.salad.cloud"
    # explicit http to a non-loopback host is still forced to https
    # (the salad.cloud front only proxies https)
    assert normalize_gateway("http://tamarind-x.salad.cloud/v1") == \
        "https://tamarind-x.salad.cloud"
    # explicit http to loopback is preserved (local test gateways)
    assert normalize_gateway("http://127.0.0.1:54321/v1") == \
        "http://127.0.0.1:54321"
    for bad in ("", "   ", "a b.c"):
        with pytest.raises(ValueError):
            normalize_gateway(bad)


# -- worker endpoint -------------------------------------------------------------

def test_worker_endpoint():
    b = BackendConfig(id="x", endpoint="http://127.0.0.1:8080")
    assert worker_endpoint(b) == "http://127.0.0.1:8080"
    s = BackendConfig(id="y", endpoint="https://g.salad.cloud", kind="salad",
                      salad_proxy_port=18101)
    assert worker_endpoint(s) == "http://127.0.0.1:18101"


# -- proxy nonce -----------------------------------------------------------------

def test_proxy_nonce_stable_and_url_sensitive():
    mk = lambda ep: BackendConfig(id="g1", endpoint=ep, kind="salad",
                                  salad_proxy_port=18100)
    assert proxy_nonce(mk("https://g1.salad.cloud")) == \
        proxy_nonce(mk("https://g1.salad.cloud"))
    assert proxy_nonce(mk("https://g1.salad.cloud")) != \
        proxy_nonce(mk("https://g2.salad.cloud"))


# -- port allocation ---------------------------------------------------------------

def _cfg(*ports) -> Config:
    bs = [BackendConfig(id=f"s{i}", endpoint=f"https://g{i}.salad.cloud",
                        kind="salad", salad_proxy_port=p)
          for i, p in enumerate(ports)]
    return Config(paths=None, daemon=DaemonConfig(), worker=WorkerConfig(),
                  mycelium=MyceliumConfig(), backends=bs, groups=[],
                  profiles=[], projects=[])


def test_port_allocation(monkeypatch):
    # Hermetic: the allocator does a best-effort local bind probe, which
    # would otherwise depend on whether the live daemon holds 18100. Stub
    # the socket so every port reads as free and only the config `used`
    # set drives the result.
    import local_dispatcher.salad as salad

    class _FakeSocket:
        def setsockopt(self, _level, _opt, _val):
            pass

        def bind(self, _addr):
            pass

        def close(self):
            pass

    class _FakeSocketModule:
        AF_INET = 2
        SOCK_STREAM = 1
        SOL_SOCKET = 1
        SO_REUSEADDR = 2

        @staticmethod
        def socket(_af, _type):
            return _FakeSocket()

    monkeypatch.setattr(salad, "socket", _FakeSocketModule)
    port = allocate_salad_port(_cfg())
    assert port == SALAD_PROXY_PORT_BASE
    port = allocate_salad_port(_cfg(SALAD_PROXY_PORT_BASE))
    assert port == SALAD_PROXY_PORT_BASE + 1
    full = tuple(range(SALAD_PROXY_PORT_BASE,
                       SALAD_PROXY_PORT_BASE + 100))
    assert allocate_salad_port(_cfg(*full)) is None


# -- TOML editing -------------------------------------------------------------------

TOML = """# top comment
[daemon]
tick_s = 1.0

[[backends]]
id = "local1"
endpoint = "http://127.0.0.1:8080"

[[profiles]]
id = "w"
adapter = "claude"
backend_ids = ["local1"]

[[profiles]]
id = "c"
adapter = "cline"
backend_ids = ["local1"]
"""


def test_toml_add_remove_roundtrip(tmp_path):
    b = BackendConfig(id="salad2", endpoint="https://g2.salad.cloud",
                      model_id="qwen", model_label="qwen", kind="salad",
                      salad_keyfile="/tmp/k", salad_proxy_port=18105)
    text = append_to_profile_backend_lists(
        append_salad_backend(TOML, b), "salad2", adapters={"claude"})
    # file comments survive; the claude profile gains the id, the cline
    # profile does not
    assert "# top comment" in text
    claude_block = text.split('id = "w"')[1].split("[[profiles]]")[0]
    assert '"salad2"' in claude_block and '"local1"' in claude_block
    # the cline profile's block (up to the next table header) is untouched
    cline_block = text.split('id = "c"')[1].split("[[")[0]
    assert '"salad2"' not in cline_block and '"local1"' in cline_block

    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(text, encoding="utf-8")
    cfg = load_config(cfgfile)
    s = cfg.backend("salad2")
    assert s is not None and s.kind == "salad" and s.salad_proxy_port == 18105
    assert s.model_id == "qwen" and s.model_label == "qwen"
    assert "salad2" in cfg.profile("w").backend_ids
    assert "salad2" not in cfg.profile("c").backend_ids

    # list-append is idempotent
    assert append_to_profile_backend_lists(text, "salad2",
                                           adapters={"claude"}) == text

    out = remove_salad_backend(text, "salad2")
    assert "salad2" not in out
    cfgfile.write_text(out, encoding="utf-8")
    cfg2 = load_config(cfgfile)
    assert cfg2.backend("salad2") is None
    assert "salad2" not in cfg2.profile("w").backend_ids
    assert "local1" in cfg2.profile("w").backend_ids
    with pytest.raises(ValueError):
        remove_salad_backend(out, "salad2")


# -- -d URL → row resolution ----------------------------------------------------------

def test_find_salad_backend():
    rows = [
        {"id": "b1", "kind": "local", "endpoint": "http://127.0.0.1:8080"},
        {"id": "g1", "kind": "salad",
         "endpoint": "https://tamarind-x.salad.cloud"},
        {"id": "g2", "kind": "salad", "endpoint": "http://127.0.0.1:54321"},
    ]
    assert find_salad_backend(rows, "tamarind-x.salad.cloud")["id"] == "g1"
    # scheme/path variations normalize to the same row
    assert find_salad_backend(rows,
                              "https://tamarind-x.salad.cloud/v1")["id"] == "g1"
    assert find_salad_backend(rows, "http://127.0.0.1:54321")["id"] == "g2"
    assert find_salad_backend(rows, "https://unknown.salad.cloud") is None
    with pytest.raises(ValueError):
        find_salad_backend(rows, "bad url with spaces")


# -- regression: scheme handling, proxy identity, multi-line TOML --------------------

def test_normalize_gateway_scheme_handling():
    assert normalize_gateway("HTTPS://G.salad.cloud") == "https://G.salad.cloud"
    # non-http(s) schemes are rejected, not mangled into https://ftp:
    for bad in ("ftp://g.salad.cloud", "file:///etc/passwd", "ssh://host"):
        with pytest.raises(ValueError):
            normalize_gateway(bad)


def test_proxy_nonce_covers_spawn_identity(tmp_path):
    key = tmp_path / "salad_key"
    key.write_text("key-one", encoding="utf-8")

    def mk(**kw):
        args = dict(id="g1", endpoint="https://g1.salad.cloud",
                    model_id="qwen", model_label="qwen",
                    kind="salad", salad_proxy_port=18100,
                    salad_keyfile=str(key))
        args.update(kw)
        return BackendConfig(**args)

    base = proxy_nonce(mk())
    assert base == proxy_nonce(mk())               # stable
    assert base != proxy_nonce(mk(model_label="other"))
    assert base != proxy_nonce(mk(salad_thinking=True))
    key.write_text("key-two", encoding="utf-8")    # rotation = new identity
    assert base != proxy_nonce(mk())


def test_toml_multiline_and_indented_profile_lists():
    text = (
        '[[profiles]]\n'
        'id = "w"\n'
        '  adapter = "claude"          # indented on purpose\n'
        'backend_ids = [\n'
        '  "local1",   # a comment inside the array\n'
        '  "local2",\n'
        ']  # trailing comment\n'
    )
    out = append_to_profile_backend_lists(text, "s1", adapters={"claude"})
    assert 'backend_ids = ["local1", "local2", "s1"]  # trailing comment' in out
    assert "a comment inside the array" not in out
    # idempotent, and the result still parses as TOML
    assert append_to_profile_backend_lists(out, "s1",
                                           adapters={"claude"}) == out
    import tomllib
    parsed = tomllib.loads(out)
    assert parsed["profiles"][0]["backend_ids"] == ["local1", "local2", "s1"]


def test_toml_remove_from_multiline_list():
    text = (
        '[[backends]]\n'
        'id = "s1"\n'
        'endpoint = "https://g.salad.cloud"\n'
        'kind = "salad"\n'
        'model_id = "qwen"\n'
        'salad_proxy_port = 18100\n'
        '\n'
        '  [[profiles]]\n'
        '  id = "w"\n'
        '  adapter = "claude"\n'
        '  backend_ids = [\n'
        '    "local1",\n'
        '    "s1",\n'
        '  ]  # tail\n'
    )
    out = remove_salad_backend(text, "s1")
    assert 'backend_ids = ["local1"]  # tail' in out
    assert "s1" not in out


def test_think_stripper_chunk_boundaries():
    text = "<think>\n\n</think>\n\nHello"
    for chunks in ([text],
                   ["<think>\n\n</think>", "\n", "\nHello"],
                   ["<thi", "nk>\n\n</think>\n", "\nHel", "lo"],
                   ["<think>", "\n\n", "</think>", "\n\n", "Hello"]):
        s = EmptyThinkStripper()
        out = "".join(s.feed(c) for c in chunks) + s.flush()
        assert out == "Hello", chunks


def test_stream_builder_reports_input_tokens():
    b = StreamBuilder("qwen", "u1")
    frames = [b.message_start()]
    frames += b.handle({"usage": {"prompt_tokens": 123, "completion_tokens": 4},
                        "choices": [{"delta": {"content": "hi"},
                                     "finish_reason": "stop"}]})
    frames += b.finish()
    delta = next(f for f in frames if "message_delta" in f)
    payload = json.loads(delta.split("data: ", 1)[1])
    assert payload["usage"] == {"input_tokens": 123, "output_tokens": 4}


def test_pick_backend_skips_salad_for_cline(tmp_path):
    from local_dispatcher.backends import BackendRegistry
    from local_dispatcher.db import Database, now_iso
    cfg = _cfg(18100)
    db = Database(tmp_path / "t.db")
    db.migrate()
    reg = BackendRegistry(cfg, db)
    reg.sync()
    db.execute("UPDATE backends SET health='online', probed_at=? WHERE id='s0'",
               (now_iso(),))
    assert reg.pick_backend(["s0"]) == "s0"           # claude can use it
    assert reg.pick_backend(["s0"], adapter="cline") is None
    db.close()


def test_config_rejects_bad_salad_ports(tmp_path):
    p = tmp_path / "config.toml"

    def write(*blocks):
        p.write_text("\n".join(blocks), encoding="utf-8")

    write('[[backends]]\nid = "a"\nendpoint = "https://a.salad.cloud"\n'
          'kind = "salad"\nsalad_proxy_port = 18100\n',
          '[[backends]]\nid = "b"\nendpoint = "https://b.salad.cloud"\n'
          'kind = "salad"\nsalad_proxy_port = 18100\n')
    with pytest.raises(ValueError, match="used by both"):
        load_config(p)

    write('[[backends]]\nid = "a"\nendpoint = "https://a.salad.cloud"\n'
          'kind = "salad"\n')
    with pytest.raises(ValueError, match="salad_proxy_port is required"):
        load_config(p)


def test_config_rejects_salad_backend_for_cline_profile(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[backends]]\nid = "s"\nendpoint = "https://s.salad.cloud"\n'
        'kind = "salad"\nsalad_proxy_port = 18100\n'
        '[[profiles]]\nid = "c"\nadapter = "cline"\nbackend_ids = ["s"]\n',
        encoding="utf-8")
    with pytest.raises(ValueError, match="cannot use salad"):
        load_config(p)


# -- upstream stream anomaly detection (salad_proxy) -------------------------------
#
# The live j_8bf0ff2baf failure: upstream returned 200, then the stream
# ended with zero content (edge/origin kill, in-band error payload, or a
# zero-token completion) and the proxy rendered it as a clean empty
# "success" turn → bogus invalid_result. These pin the fix: anomalies and
# contentless ends must be surfaced, never silently finished.

from local_dispatcher.salad_proxy import StreamBuilder, _iter_sse


class _LineSource:
    """Minimal http.client response stand-in: readline() over bytes."""
    def __init__(self, lines):
        # b"" is EOF for readline(); an empty SSE separator line is b"\n".
        self._lines = [b"\n" if l == "" else
                       (l if isinstance(l, bytes) else l.encode())
                       for l in lines]

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


def _chunks(objs):
    return ["data: " + __import__("json").dumps(o) + "\n\n" for o in objs]


def _content_delta(c):
    return {"choices": [{"index": 0, "delta": {"content": c}}]}


def test_iter_sse_normal_stream():
    out = list(_iter_sse(_LineSource([
        *(_chunks([_content_delta("a"), _content_delta("b")])),
        "",              # event separator
        ": keepalive",   # SSE comment — fine, no anomaly
        "event: x",
        "id: 42",        # standard SSE fields — fine, no anomaly
        "retry: 3000",
        "data: [DONE]",
        _chunks([_content_delta("never")])[0],  # after DONE: not read
    ])))
    chunks = [c for c in out if "__proxy_done__" not in c]
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["a", "b"]
    assert any(o.get("__proxy_done__") for o in out)
    assert not any("__proxy_anomaly__" in o for o in out)


def test_iter_sse_error_null_is_not_terminal():
    out = list(_iter_sse(_LineSource([
        "data: " + json.dumps({"error": None, "choices": [
            {"index": 0, "delta": {"content": "ok"}}]}) + "\n\n",
    ])))
    assert not any("__proxy_anomaly__" in o for o in out)
    assert out and out[0]["choices"][0]["delta"]["content"] == "ok"


def test_iter_sse_flags_anomalies():
    out = list(_iter_sse(_LineSource([
        _chunks([_content_delta("a")])[0],
        "<html>524 a timeout occurred</html>\n\n",
        "data: {not json\n\n",
        "data: " + __import__("json").dumps(
            {"error": {"message": "boom"}}) + "\n\n",
        _chunks([_content_delta("never")])[0],  # terminal: after error
    ])))
    chunks = [o for o in out if "__proxy_anomaly__" not in o]
    anomalies = [o["__proxy_anomaly__"] for o in out
                 if "__proxy_anomaly__" in o]
    assert len(chunks) == 1
    assert len(anomalies) == 3
    assert "524" in anomalies[0]
    assert "unparseable" in anomalies[1]
    assert "boom" in anomalies[2]


def test_streambuilder_empty_vs_content():
    b = StreamBuilder("qwen", "u1")
    assert b.is_empty()
    for _ in b.handle(_content_delta("hi")):
        pass
    assert not b.is_empty()          # text counts as content

    b2 = StreamBuilder("qwen", "u2")
    # tool-only turns are valid (the live large-prefill run ended with
    # finish_reason=tool_calls, zero text) — must NOT be flagged empty
    for _ in b2.handle({"choices": [{"index": 0,
                                     "delta": {"tool_calls": [
                                         {"index": 0, "id": "c1",
                                          "type": "function",
                                          "function": {
                                              "name": "f", "arguments": "x"}}]}}]}):
        pass
    assert not b2.is_empty()

    b3 = StreamBuilder("qwen", "u3")
    b3.handle({"__proxy_anomaly__": "upstream error payload: boom"})
    assert b3.is_empty()
    assert b3.anomalies == ["upstream error payload: boom"]
    # finish() still closes cleanly for a contentless stream
    frames = b3.finish()
    assert any("message_stop" in f for f in frames)
