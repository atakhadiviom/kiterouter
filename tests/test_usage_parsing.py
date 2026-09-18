"""Upstream usage/cost parsing: real figures recorded, the rest flagged."""

import pytest

from kiterouter.usage import UsageCollector, extract_cost, normalize_usage


def test_openai_usage_maps_all_columns():
    assert normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 30},
            "completion_tokens_details": {"reasoning_tokens": 12},
        }
    ) == {
        "tokens_in": 100,
        "tokens_out": 40,
        "tokens_cache_read": 30,
        "tokens_cache_write": 0,
        "tokens_reasoning": 12,
    }


def test_anthropic_usage_maps_cache_write():
    assert normalize_usage(
        {
            "input_tokens": 200,
            "output_tokens": 50,
            "cache_read_input_tokens": 120,
            "cache_creation_input_tokens": 80,
        }
    ) == {
        "tokens_in": 200,
        "tokens_out": 50,
        "tokens_cache_read": 120,
        "tokens_cache_write": 80,
        "tokens_reasoning": 0,
    }


@pytest.mark.parametrize("payload", [None, {}, {"choices": []}, [], "usage"])
def test_no_usage_block_is_none(payload):
    assert normalize_usage(payload) is None


def test_tolerates_string_numbers_and_missing_keys():
    assert normalize_usage({"prompt_tokens": "5"}) == {
        "tokens_in": 5,
        "tokens_out": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "tokens_reasoning": 0,
    }


def test_collector_keeps_the_last_usage_block():
    collector = UsageCollector()
    collector.feed({"choices": [{"delta": {"content": "hi"}}]})
    assert collector.usage is None
    collector.feed({"usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    collector.feed({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert collector.usage is not None
    assert collector.usage["tokens_out"] == 5


def test_upstream_reported_cost_is_billed_not_estimated():
    assert extract_cost({"cost_usd": 0.004}) == pytest.approx(0.004)
    assert extract_cost({"usage": {"prompt_tokens": 1}}) is None
    assert extract_cost({}) is None
    assert extract_cost(None) is None


def test_collector_cost_tracks_usage():
    collector = UsageCollector()
    collector.feed({"choices": [{"delta": {"content": "x"}}]})
    assert collector.cost_usd is None
    collector.feed({"usage": {"prompt_tokens": 2, "cost_usd": 0.001}})
    assert collector.cost_usd == pytest.approx(0.001)
