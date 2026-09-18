"""Tests for usage/token aggregation, per-provider metrics and their endpoints."""
from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from kiterouter import server
from kiterouter.server import app
from kiterouter.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "kiterouter.db")
    yield s
    s.close()


def seed(store, provider="alpha", model="m", latencies=(), ttfts=(), status="ok", **kwargs):
    """Insert one request per latency value, pairing ttfts positionally."""
    now = int(time.time())
    for index, latency in enumerate(latencies):
        store.record_request(
            provider=provider,
            model=model,
            status=status,
            latency_ms=latency,
            ttft_ms=ttfts[index] if index < len(ttfts) else None,
            at=now - index,
            **kwargs,
        )


# ── percentile maths ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "values,pct,expected",
    [
        ([], 50, None),
        ([7], 95, 7),
        ([100, 200, 300, 400, 500], 50, 300),
        ([100, 200, 300, 400, 500], 95, 480),
        ([10, 20], 50, 15),
    ],
)
def test_percentile(values, pct, expected):
    assert Store._percentile(values, pct) == expected


# ── usage ────────────────────────────────────────────────────────────────────

def test_usage_summary_groups_by_provider(store):
    seed(store, provider="alpha", latencies=[100, 110], tokens_in=10, tokens_out=5)
    seed(store, provider="beta", latencies=[200], tokens_in=20, tokens_out=7)

    rows = {r["key"]: r for r in store.usage_summary()}

    assert set(rows) == {"alpha", "beta"}
    assert rows["alpha"]["requests"] == 2
    assert rows["alpha"]["tokens_in"] == 20
    assert rows["alpha"]["tokens_out"] == 10
    assert rows["beta"]["requests"] == 1


def test_usage_summary_reports_success_rate(store):
    store.record_request(provider="p", model="m", status="ok")
    store.record_request(provider="p", model="m", status="error", error="boom")

    row = store.usage_summary()[0]
    assert row["requests"] == 2
    assert row["ok"] == 1
    assert row["failed"] == 1
    assert row["ok_rate_pct"] == 50.0


def test_usage_summary_can_group_by_model_and_combo(store):
    store.record_request(provider="p", model="model-a", status="ok", combo="coding")
    store.record_request(provider="p", model="model-b", status="ok", combo="coding")

    by_model = {r["key"] for r in store.usage_summary(group_by="model")}
    by_combo = {r["key"] for r in store.usage_summary(group_by="combo")}

    assert by_model == {"model-a", "model-b"}
    assert by_combo == {"coding"}


def test_usage_summary_rejects_unknown_grouping(store):
    with pytest.raises(ValueError):
        store.usage_summary(group_by="; DROP TABLE requests")


def test_usage_summary_respects_the_window(store):
    now = int(time.time())
    store.record_request(provider="old", model="m", status="ok", at=now - 10 * 86400)
    store.record_request(provider="new", model="m", status="ok", at=now - 60)

    assert {r["key"] for r in store.usage_summary(since=now - 86400)} == {"new"}
    assert {r["key"] for r in store.usage_summary()} == {"old", "new"}


def test_usage_daily_buckets_by_day(store):
    now = int(time.time())
    store.record_request(provider="p", model="m", status="ok", at=now - 3600)
    store.record_request(provider="p", model="m", status="ok", at=now - 3600 - 86400)

    days = store.usage_daily()
    assert len(days) == 2
    assert all(day["requests"] == 1 for day in days)


# ── tokens ───────────────────────────────────────────────────────────────────

def test_token_breakdown_sums_every_recorded_column(store):
    store.record_request(
        provider="p", model="m", status="ok", tokens_in=100, tokens_out=40,
        tokens_cache_read=30, tokens_cache_write=20, tokens_reasoning=10, rtk_saved=7,
    )
    store.record_request(provider="p", model="m", status="ok", tokens_in=1, tokens_out=2)

    totals = store.token_breakdown()

    assert totals["tokens_in"] == 101
    assert totals["tokens_out"] == 42
    assert totals["tokens_cache_read"] == 30
    assert totals["tokens_cache_write"] == 20
    assert totals["tokens_reasoning"] == 10
    assert totals["rtk_saved"] == 7
    assert totals["tokens_total"] == 143
    assert totals["requests"] == 2


def test_token_breakdown_is_zeroed_when_empty(store):
    totals = store.token_breakdown()
    assert totals["requests"] == 0
    assert totals["tokens_total"] == 0


# ── provider metrics ─────────────────────────────────────────────────────────

def test_provider_metrics_computes_latency_percentiles(store):
    seed(store, provider="alpha", latencies=[100, 200, 300, 400, 500])

    row = store.provider_metrics()[0]

    assert row["provider"] == "alpha"
    assert row["p50_latency_ms"] == 300
    assert row["p95_latency_ms"] == 480


def test_ttft_percentiles_only_use_rows_that_reported_one(store):
    seed(store, provider="alpha", latencies=[100, 200, 300], ttfts=[10, 20, 30])
    seed(store, provider="alpha", latencies=[400])  # no ttft

    row = store.provider_metrics()[0]

    assert row["ttft_samples"] == 3, "rows without a ttft must not count as samples"
    assert row["p50_ttft_ms"] == 20


def test_provider_metrics_tolerates_missing_latency(store):
    store.record_request(provider="p", model="m", status="error", error="nope")

    row = store.provider_metrics()[0]
    assert row["p50_latency_ms"] is None
    assert row["p95_ttft_ms"] is None
    assert row["last_error"] == "nope"


def test_provider_metrics_splits_by_provider_and_rates(store):
    store.record_request(provider="a", model="m", status="ok", latency_ms=100)
    store.record_request(provider="b", model="m", status="error", latency_ms=50, error="x")

    rows = {r["provider"]: r for r in store.provider_metrics()}

    assert rows["a"]["ok_rate_pct"] == 100.0
    assert rows["b"]["ok_rate_pct"] == 0.0


# ── endpoints ────────────────────────────────────────────────────────────────

def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_usage_endpoint_shape():
    server.store.record_request(
        provider="alpha", model="m", status="ok", latency_ms=120, tokens_in=9, tokens_out=4
    )

    async with client() as c:
        resp = await c.get("/api/usage?days=1&group_by=provider")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["group_by"] == "provider"
    assert any(g["key"] == "alpha" for g in body["groups"])
    assert "totals" in body and "daily" in body


@pytest.mark.asyncio
async def test_tokens_endpoint_shape():
    server.store.record_request(
        provider="p", model="m", status="ok", tokens_in=11, tokens_out=5, tokens_cache_read=3
    )

    async with client() as c:
        resp = await c.get("/api/tokens?days=1")

    body = resp.json()
    assert body["status"] == "success"
    assert body["tokens_in"] >= 11
    assert body["tokens_cache_read"] >= 3
    assert body["tokens_total"] == body["tokens_in"] + body["tokens_out"]


@pytest.mark.asyncio
async def test_provider_stats_endpoint_reports_percentiles():
    for latency in (100, 300, 500):
        server.store.record_request(
            provider="beta", model="m", status="ok", latency_ms=latency, ttft_ms=latency // 10
        )

    async with client() as c:
        resp = await c.get("/api/provider-stats?days=1")

    body = resp.json()
    row = next(p for p in body["providers"] if p["provider"] == "beta")
    assert row["requests"] >= 3
    assert row["p50_latency_ms"] == 300
    assert row["p95_ttft_ms"] is not None


@pytest.mark.asyncio
async def test_endpoints_exclude_rows_outside_the_window():
    now = int(time.time())
    server.store.record_request(
        provider="ancient", model="m", status="ok", at=now - 40 * 86400
    )

    async with client() as c:
        narrow = await c.get("/api/usage?days=1")
        wide = await c.get("/api/usage?days=365")

    assert not any(g["key"] == "ancient" for g in narrow.json()["groups"])
    assert any(g["key"] == "ancient" for g in wide.json()["groups"])


def test_cost_summary_splits_billed_estimated_and_unknown():
    now = int(time.time())
    server.store.record_request(
        provider="bill", model="m", status="ok", cost_usd=0.01, at=now
    )
    server.store.record_request(
        provider="bill", model="m", status="ok", cost_usd=0.02, cost_is_estimate=1, at=now
    )
    server.store.record_request(provider="bill", model="m", status="ok", at=now)

    rows = server.store.cost_summary(since=now - 86400)
    row = next(g for g in rows if g["key"] == "bill")
    assert row["cost_usd"] == pytest.approx(0.03)
    assert row["cost_billed_usd"] == pytest.approx(0.01)
    assert row["cost_estimated_usd"] == pytest.approx(0.02)
    assert row["unknown_cost_requests"] == 1
    assert row["requests"] == 3


def test_cost_groups_by_key_and_counts_unattributed():
    now = int(time.time())
    server.store.record_request(
        provider="p", model="m", status="ok", cost_usd=0.05, api_key_id="abc123", at=now
    )
    server.store.record_request(provider="p", model="m", status="ok", at=now)

    rows = server.store.cost_summary(since=now - 86400, group_by="key")
    assert next(g for g in rows if g["key"] == "abc123")["cost_usd"] == pytest.approx(0.05)
    assert next(g for g in rows if g["key"] == "(unattributed)")["unknown_cost_requests"] == 1


def test_cost_summary_rejects_unknown_grouping():
    with pytest.raises(ValueError):
        server.store.cost_summary(group_by="'; DROP TABLE requests;--")


@pytest.mark.asyncio
async def test_costs_endpoint_reports_unknowns_not_zeroes():
    now = int(time.time())
    server.store.record_request(provider="gamma", model="m", status="ok", at=now)

    async with client() as c:
        resp = await c.get("/api/costs?days=1")

    body = resp.json()
    assert body["status"] == "success"
    assert "as_of" in body
    row = next(g for g in body["groups"] if g["key"] == "gamma")
    assert row["cost_usd"] is None
    assert row["unknown_cost_requests"] >= 1
    assert body["totals"]["unknown_cost_requests"] >= 1


@pytest.mark.asyncio
async def test_usage_endpoints_carry_freshness_and_estimate_flags():
    server.store.record_request(
        provider="est", model="m", status="ok", tokens_in=4, tokens_out=2,
        tokens_is_estimate=1,
    )
    row = server.store.request_by_id(server.store.count_requests() and server.store.recent_requests(limit=1)[0]["id"])
    assert row is not None
    assert row["tokens_is_estimate"] == 1

    async with client() as c:
        usage = await c.get("/api/usage?days=1")
        stats = await c.get("/api/stats")

    assert "as_of" in usage.json()
    assert "store" in stats.json() and "as_of" in stats.json()["store"]
