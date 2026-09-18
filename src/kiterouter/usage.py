"""Usage and cost extraction from upstream responses.

Cost is recorded only when an upstream reports one, and stays NULL otherwise —
there is no per-model price table, so nothing here can present a derived figure
as billed. Token counts fall back to a labelled estimate only when the upstream
sent no usage block at all.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(payload: Any) -> Optional[Dict[str, int]]:
    """Map an upstream usage block onto the store's token columns.

    Handles OpenAI (``prompt_tokens`` / ``completion_tokens`` with the nested
    ``prompt_tokens_details`` / ``completion_tokens_details`` for cache and
    reasoning) and Anthropic (``input_tokens`` / ``output_tokens`` with
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``) shapes.
    Returns None when the payload carries no usable usage data.
    """
    if not isinstance(payload, dict):
        return None

    def pick(*keys: str) -> int:
        for key in keys:
            if key in payload:
                return _as_int(payload.get(key))
        return 0

    has_tokens = any(k in payload for k in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"))
    if not has_tokens:
        return None

    tokens_in = pick("prompt_tokens", "input_tokens")
    tokens_out = pick("completion_tokens", "output_tokens")

    cached_read = 0
    reasoning = 0
    details = payload.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached_read = _as_int(details.get("cached_tokens"))
    details = payload.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = _as_int(details.get("reasoning_tokens"))
    cached_read += pick("cache_read_input_tokens")
    cached_write = pick("cache_creation_input_tokens")

    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_cache_read": cached_read,
        "tokens_cache_write": cached_write,
        "tokens_reasoning": reasoning,
    }


def extract_cost(payload: Any) -> Optional[float]:
    """Cost as reported by the upstream, or None when it reported none."""
    if not isinstance(payload, dict):
        return None
    for key in ("cost_usd", "cost", "price_usd"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    cost = payload.get("cost_details")
    if isinstance(cost, dict):
        try:
            return float(cost.get("total_cost_usd"))
        except (TypeError, ValueError):
            pass
    return None


class UsageCollector:
    """Keep the last non-null usage/cost object seen in an SSE stream.

    Most providers that report usage do so once, in a trailing chunk — so the
    latest block wins and anything earlier is superseded, not summed.
    """

    def __init__(self) -> None:
        self._usage: Optional[Dict[str, int]] = None
        self._cost: Optional[float] = None

    def feed(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        usage = payload.get("usage")
        if usage is None and isinstance(payload.get("message"), dict):
            usage = payload["message"].get("usage")
        tokens = normalize_usage(usage)
        if tokens is not None:
            self._usage = tokens
        cost = extract_cost(usage)
        if cost is None:
            cost = extract_cost(payload)
        if cost is not None:
            self._cost = cost

    @property
    def usage(self) -> Optional[Dict[str, int]]:
        return self._usage

    @property
    def cost_usd(self) -> Optional[float]:
        return self._cost
