"""Unit tests — salad URL normalization, port allocation, proxy nonce,
TOML backend/profile-list editing, and the -d URL→row resolution."""

from __future__ import annotations

import pytest

from local_dispatcher.config import (BackendConfig, Config, DaemonConfig,
                                     MyceliumConfig, WorkerConfig,
                                     load_config, worker_endpoint)
from local_dispatcher.salad import (SALAD_PROXY_PORT_BASE,
                                    allocate_salad_port, append_salad_backend,
                                    append_to_profile_backend_lists,
                                    find_salad_backend, normalize_gateway,
                                    proxy_nonce, remove_salad_backend)

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


def test_port_allocation():
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
