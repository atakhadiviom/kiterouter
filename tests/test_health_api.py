"""Tests for the health-history and store endpoints."""
from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from kiterouter import server
from kiterouter.server import app


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def seeded_store():
    """Rows in the (isolated) store, via the server's own instance."""
    now = int(time.time())

    def seed():
        server.store.record_health(
            "antigravity", "acct1", ok=True, model="gemini-3.8",
            latency_ms=12274, ttft_ms=9132, at=now - 60,
        )
        server.store.record_health(
            "antigravity", "acct1", ok=True, model="gemini-3.8",
            latency_ms=9000, ttft_ms=8000, at=now - 30,
        )
        server.store.record_health(
            "cline", "acct2", ok=False, model="cline-free/x",
            latency_ms=90, ttft_ms=None, error="Cline HTTP 401 unauthorized", at=now - 10,
        )

    return seed


@pytest.mark.asyncio
async def test_health_history_returns_newest_first(seeded_store):
    seeded_store()
    async with client() as c:
        resp = await c.get("/api/health/history?limit=10")

    assert resp.status_code == 200
    checks = resp.json()["checks"]
    assert len(checks) == 3
    assert checks[0]["at"] >= checks[-1]["at"], "newest first"


@pytest.mark.asyncio
async def test_health_connections_aggregates_per_connection(seeded_store):
    seeded_store()
    async with client() as c:
        resp = await c.get("/api/health/connections?days=7")

    assert resp.status_code == 200
    rows = {r["provider"]: r for r in resp.json()["connections"]}
    assert set(rows) == {"antigravity", "cline"}

    a = rows["antigravity"]
    assert a["checks"] == 2
    assert a["ok_count"] == 2
    assert a["ok_rate_pct"] == 100.0
    assert a["min_latency_ms"] == 9000
    assert a["max_latency_ms"] == 12274
    assert a["avg_ttft_ms"] == 8566
    assert a["latest"]["ok"] == 1

    c_row = rows["cline"]
    assert c_row["ok_rate_pct"] == 0.0
    assert c_row["latest"]["error"].startswith("Cline")


@pytest.mark.asyncio
async def test_health_connections_can_be_widened_to_all_history(seeded_store):
    """A wide window must still return the rows, not silently nothing."""
    seeded_store()
    async with client() as c:
        narrow = await c.get("/api/health/connections?days=1")
        wide = await c.get("/api/health/connections?days=365")

    assert len(narrow.json()["connections"]) == 2
    assert len(wide.json()["connections"]) == 2


@pytest.mark.asyncio
async def test_store_endpoint_reports_size_wal_and_retention(seeded_store):
    seeded_store()
    async with client() as c:
        resp = await c.get("/api/store")

    assert resp.status_code == 200
    body = resp.json()
    assert body["health_checks"] == 3
    assert body["size_bytes"] > 0
    assert "wal_bytes" in body
    assert body["retention_days"] == {
        "health_checks": 30,
        "requests": 30,
        "quota_snapshots": 90,
        "events": 90,
    }
    assert body["path"].endswith("kiterouter.db")


@pytest.mark.asyncio
async def test_store_maintain_endpoint_prunes_and_checkpoints(seeded_store):
    seeded_store()
    async with client() as c:
        resp = await c.post("/api/store/maintain")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert "pruned" in body and "checkpointed" in body


@pytest.mark.asyncio
async def test_prober_status_exposes_per_connection_and_backoff():
    async with client() as c:
        resp = await c.get("/api/prober")

    assert resp.status_code == 200
    body = resp.json()
    assert "connections" in body
    assert "backoff" in body
    assert "needs_action" in body
    assert "timeout_seconds" in body


@pytest.mark.asyncio
async def test_prober_run_records_into_the_store(monkeypatch):
    """A sweep must leave rows the Health page can read."""
    async def fake_probe_once():
        server.store.record_health("cursor", "abc", ok=True, latency_ms=2068, ttft_ms=1500)
        return {"summary": {"ok": 1}}

    monkeypatch.setattr(server.prober, "probe_once", fake_probe_once)

    async with client() as c:
        assert (await c.post("/api/prober/run")).status_code == 200
        connections = (await c.get("/api/health/connections")).json()["connections"]

    assert [r["provider"] for r in connections] == ["cursor"]
    assert connections[0]["avg_ttft_ms"] == 1500
