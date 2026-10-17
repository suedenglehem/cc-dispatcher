"""Unit tests — context-max extraction, the 90% derivation, the live
context-warning math, and the configurable overflow threshold in
ClaudeAdapter.classify."""

import pytest

from local_dispatcher.adapters.base import StreamInfo
from local_dispatcher.adapters.claude import ClaudeAdapter
from local_dispatcher.backends import extract_ctx_max
from local_dispatcher.config import derive_max_context
from local_dispatcher.results import context_pct


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


# -- context_pct: live 65% warning math --------------------------------------

def test_context_pct_measurable_above_and_below_warn():
    pct, warn = context_pct(20000, 28800)
    assert pct == pytest.approx(20000 / 28800)
    assert warn is True  # 0.694 >= 0.65

    pct2, warn2 = context_pct(10000, 28800)
    assert pct2 == pytest.approx(10000 / 28800)
    assert warn2 is False


def test_context_pct_warn_boundary_is_inclusive():
    pct, warn = context_pct(18720, 28800)
    assert pct == pytest.approx(0.65)
    assert warn is True
    _, warn2 = context_pct(18719, 28800)
    assert warn2 is False


def test_context_pct_not_measurable_stays_unknown():
    # PRD §9: show unknown rather than fabricate a percentage
    assert context_pct(None, 28800) == (None, False)
    assert context_pct(0, 28800) == (None, False)
    assert context_pct(20000, None) == (None, False)
    assert context_pct(20000, 0) == (None, False)
    assert context_pct(20000, -5) == (None, False)


def test_context_pct_custom_warn_pct():
    assert context_pct(5000, 10000, warn_pct=0.5) == (0.5, True)
    assert context_pct(4999, 10000, warn_pct=0.5)[1] is False


# -- claude.classify: configurable overflow threshold --------------------------

def test_classify_overflow_from_usage_uses_configured_pct():
    ad = ClaudeAdapter()
    info = StreamInfo(last_context_tokens=9100)
    # default 0.95: 91% of the budget is not yet overflow -> plain crash
    assert ad.classify(exit_code=1, info=info, ctx_limit=10000) == "crash"
    # configured 0.9: the same evidence is context_overflow
    assert ad.classify(exit_code=1, info=info, ctx_limit=10000,
                      overflow_pct=0.9) == "context_overflow"


def test_classify_clean_exit_is_not_overflow_despite_full_context():
    # a worker that finishes near the limit with a clean exit is not a failure
    ad = ClaudeAdapter()
    info = StreamInfo(last_context_tokens=10000)
    assert ad.classify(exit_code=0, info=info, ctx_limit=10000,
                      overflow_pct=0.9) is None


def test_classify_overflow_regex_wins_without_usage():
    ad = ClaudeAdapter()
    info = StreamInfo(result={"type": "result", "is_error": True,
                              "result": "API Error: prompt is too long"})
    assert ad.classify(exit_code=1, info=info, ctx_limit=None) \
        == "context_overflow"
