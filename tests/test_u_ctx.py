"""Unit tests — context-max extraction and the 90% derivation."""

from local_dispatcher.backends import extract_ctx_max
from local_dispatcher.config import derive_max_context


def test_extract_vllm():
    body = {"data": [{"id": "qwen3.8-27b", "max_model_len": 262144}]}
    assert extract_ctx_max(body) == 262144


def test_extract_llamacpp_nested_meta():
    body = {"data": [{"id": "swift-1.5-iq3_xxs",
                      "meta": {"n_ctx": 92768}}]}
    assert extract_ctx_max(body) == 92768


def test_extract_llamacpp_direct_meta():
    body = {"data": [{"id": "m", "meta": {"max_model_len": 142848}}]}
    assert extract_ctx_max(body) == 142848


def test_extract_entry_level_fallback_keys():
    assert extract_ctx_max({"data": [{"max_context_length": 4096}]}) == 4096
    assert extract_ctx_max({"data": [{"context_length": 2048}]}) == 2048


def test_extract_missing_or_garbage():
    assert extract_ctx_max({}) is None
    assert extract_ctx_max({"data": []}) is None
    assert extract_ctx_max({"data": [{"meta": {}}]}) is None
    # non-int / non-positive values are ignored
    assert extract_ctx_max({"data": [{"max_model_len": "big"}]}) is None
    assert extract_ctx_max({"data": [{"max_model_len": 0}]}) is None
