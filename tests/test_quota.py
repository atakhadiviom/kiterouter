"""Quota: parse what providers report, surface the rest as unknown."""

import time

import pytest

from kiterouter import server
from kiterouter.quota import (
    exhausted_until,
    is_exhausted_now,
    is_exhaustion_text,
    parse_rate_limit_headers,
    parse_retry_after,
    quota_from_response,
)


def test_exhaustion_text_matches_live_antigravity_429():
    assert is_exhaustion_text(
        'Antigravity HTTP 429: {"error": {"code": 429, '
        '"message": "Resource has been exhausted (e.g. check quota).", '
        '"status": "RESOURCE_EXHAUSTED"}}'
    )
    assert is_exhaustion_text("HTTP 429 too many requests")
    assert not is_exhaustion_text("HTTP 401 unauthorized")
    assert not is_exhaustion_text("")
    assert not is_exhaustion_text(None)


def test_retry_after_seconds_and_http_date():
    now = 1_700_000_000
    assert parse_retry_after("120", now=now) == now + 120
    assert parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT") == 784111777
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


def test_full_headers_compute_pct_and_reset():
    now = 1_700_000_000
    snap = parse_rate_limit_headers(
        {"x-ratelimit-remaining": "25", "x-ratelimit-limit": "100", "x-ratelimit-reset": str(now + 60)},
        now=now,
    )
    assert snap is not None
    assert snap["remaining_pct"] == pytest.approx(25.0)
    assert snap["resets_at"] == now + 60
    assert snap["is_exhausted"] is True


def test_partial_and_absent_headers():
    now = 1_700_000_000
    assert parse_rate_limit_headers({}, now=now) is None
    assert parse_rate_limit_headers(None, now=now) is None
    snap = parse_rate_limit_headers({"retry-after": "30"}, now=now)
    assert snap is not None
    assert snap["remaining_pct"] is None
    assert snap["resets_at"] == now + 30


def test_quota_from_response_reports_nothing_by_default():
    assert quota_from_response("copilot", headers=None, body_text="ok") is None
    snap = quota_from_response("antigravity", headers=None, body_text="HTTP 429 Resource exhausted, check quota")
    assert snap is not None
    assert snap["is_exhausted"] is True
    assert snap["resets_at"] is None
    assert snap["source"] == "error-text"


def test_skip_needs_a_future_reset():
    now = int(time.time())
    assert is_exhausted_now({"is_exhausted": True, "resets_at": now + 300}, now=now) is True
    assert is_exhausted_now({"is_exhausted": True, "resets_at": now - 300}, now=now) is False
    assert is_exhausted_now({"is_exhausted": True, "resets_at": None}, now=now) is False
    assert is_exhausted_now(None, now=now) is False
    assert exhausted_until({"resets_at": now + 10}) == now + 10
    assert exhausted_until({}) is None


def test_store_round_trip_and_latest():
    now = int(time.time())
    server.store.record_quota(provider="q", remaining_pct=12.5, connection="c1", source="headers", at=now)
    server.store.record_quota(provider="q", is_exhausted=True, connection="c1", source="error-text", at=now + 5)
    latest = server.store.quota_for("q", "c1")
    assert latest is not None
    assert latest["is_exhausted"] == 1
    assert server.store.quota_for("nobody") is None


def test_quota_pruned_by_retention():
    now = int(time.time())
    server.store.record_quota(provider="old", at=now - 100 * 86400)
    server.store.record_quota(provider="new", at=now)
    removed = server.store.prune({"quota_snapshots": 90}, now=now)
    assert removed == 1
    assert server.store.quota_for("old") is None
    assert server.store.quota_for("new") is not None


@pytest.mark.asyncio
async def test_quota_endpoint_marks_unknown_providers():
    from httpx import ASGITransport, AsyncClient

    from kiterouter.server import app

    server.store.record_quota(provider="seen", remaining_pct=50.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/quota")
    body = resp.json()
    assert body["status"] == "success"
    assert "as_of" in body
    assert body["providers"]["seen"][0]["remaining_pct"] == pytest.approx(50.0)
    assert "copilot" in body["unknown_providers"]


def test_router_skips_only_with_future_reset():
    from kiterouter.router import ProviderRouter

    router = ProviderRouter(config={"providers": {}, "combos": {}})
    assert router._quota_exhausted("x") is False

    now = int(time.time())
    router.quota_lookup = lambda name: {"is_exhausted": True, "resets_at": now + 600}
    assert router._quota_exhausted("x") is True
    router.quota_lookup = lambda name: {"is_exhausted": True, "resets_at": None}
    assert router._quota_exhausted("x") is False
    router.quota_lookup = lambda name: {"is_exhausted": True, "resets_at": now - 600}
    assert router._quota_exhausted("x") is False
    router.quota_lookup = lambda name: None
    assert router._quota_exhausted("x") is False
