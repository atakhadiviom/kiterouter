"""Quota awareness: record what providers report, surface the rest as unknown.

Discovery note (live, 2026-09-18): antigravity signals exhaustion as a bare 429
body (``RESOURCE_EXHAUSTED`` / "check quota") with no reset time and no
rate-limit headers — and the request funnel only ever sees chunk text, not
response headers. So exhaustion is detected centrally from error text, and
``resets_at`` stays NULL until a provider actually reports one. Unknown stays
unknown: nothing here fabricates a remaining percentage or a reset time.
"""
from __future__ import annotations

import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional

EXHAUSTION_MARKERS = (
    "resource_exhausted",
    "check quota",
    "quota exceeded",
    "exceeded your current quota",
    "out of usage",
    "usage limit",
    "rate limit exceeded",
    "too many requests",
    "http 429",
    "429",
)


def is_exhaustion_text(text: Optional[str]) -> bool:
    """True when an error body reads as quota/rate-limit exhaustion."""
    if not text:
        return False
    lowered = str(text).lower()
    return any(marker in lowered for marker in EXHAUSTION_MARKERS)


def parse_retry_after(value: Any, now: Optional[int] = None) -> Optional[int]:
    """Retry-After (delta seconds or HTTP-date) to an epoch reset time."""
    if value is None:
        return None
    moment = int(now if now is not None else time.time())
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return moment + int(text)
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _first(headers: Mapping[str, Any], *names: str) -> Optional[Any]:
    lowered = {str(k).lower(): v for k, v in dict(headers or {}).items()}
    for name in names:
        if name in lowered and lowered[name] not in (None, ""):
            return lowered[name]
    return None


def parse_rate_limit_headers(
    headers: Optional[Mapping[str, Any]], now: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Rate-limit headers to a quota snapshot body. None when absent.

    Returns remaining_pct / is_exhausted / resets_at; anything the headers do
    not say stays None rather than defaulted.
    """
    if not headers:
        return None
    remaining = _first(headers, "x-ratelimit-remaining", "x-ratelimit-remaining-requests", "ratelimit-remaining")
    limit = _first(headers, "x-ratelimit-limit", "x-ratelimit-limit-requests", "ratelimit-limit")
    reset = _first(headers, "x-ratelimit-reset", "x-ratelimit-reset-requests", "ratelimit-reset")
    retry_after = _first(headers, "retry-after")

    if remaining is None and limit is None and reset is None and retry_after is None:
        return None

    snapshot: Dict[str, Any] = {"remaining_pct": None, "is_exhausted": False, "resets_at": None}
    try:
        if remaining is not None and limit is not None and float(limit) > 0:
            snapshot["remaining_pct"] = max(
                0.0, min(100.0, float(remaining) * 100.0 / float(limit))
            )
        elif remaining is not None and float(remaining) <= 0:
            snapshot["is_exhausted"] = True
    except (TypeError, ValueError):
        pass

    resets_at: Optional[int] = None
    if reset is not None:
        try:
            reset_value = float(str(reset).strip())
            resets_at = int(reset_value) if reset_value > 1e12 / 1000 else int(reset_value)
            if resets_at < 1_000_000_000:
                moment = int(now if now is not None else time.time())
                resets_at = moment + resets_at
        except (TypeError, ValueError):
            resets_at = None
    if resets_at is None and retry_after is not None:
        resets_at = parse_retry_after(retry_after, now=now)
    snapshot["resets_at"] = resets_at
    if resets_at is not None:
        moment = int(now if now is not None else time.time())
        snapshot["is_exhausted"] = bool(snapshot["is_exhausted"] or resets_at > moment)
    return snapshot


EXTRACTORS: Dict[str, Any] = {}


def register_extractor(provider: str):
    """Per-provider quota extractors; default is "reports nothing"."""

    def wrap(fn):
        EXTRACTORS[provider] = fn
        return fn

    return wrap


def quota_from_response(
    provider: str,
    headers: Optional[Mapping[str, Any]] = None,
    body_text: Optional[str] = None,
    now: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort quota snapshot from a response. None means "reports nothing".

    Header-derived quota applies uniformly; provider-specific body parsing only
    where an extractor is registered (none are yet — discovery has not proven a
    body surface on any provider).
    """
    extractor = EXTRACTORS.get(provider or "")
    if extractor is not None:
        try:
            result = extractor(headers=headers, body_text=body_text, now=now)
            if result is not None:
                return result
        except Exception:
            pass
    snapshot = parse_rate_limit_headers(headers, now=now)
    if snapshot is not None:
        snapshot["source"] = "headers"
        return snapshot
    if is_exhaustion_text(body_text):
        return {
            "remaining_pct": 0.0,
            "is_exhausted": True,
            "resets_at": None,
            "source": "error-text",
        }
    return None


def exhausted_until(snapshot: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Known reset epoch, or None when the reset time was never reported."""
    if not snapshot:
        return None
    resets_at = snapshot.get("resets_at")
    try:
        return int(resets_at) if resets_at is not None else None
    except (TypeError, ValueError):
        return None


def is_exhausted_now(snapshot: Optional[Mapping[str, Any]], now: Optional[int] = None) -> bool:
    """An exhaustion marker counts only while its reset lies in the future.

    A bare exhaustion with no reset time is reported (so the operator sees it)
    but never used to skip — without a reset there is nothing to skip until.
    """
    if not snapshot or not snapshot.get("is_exhausted"):
        return False
    resets_at = exhausted_until(snapshot)
    if resets_at is None:
        return False
    return resets_at > int(now if now is not None else time.time())
