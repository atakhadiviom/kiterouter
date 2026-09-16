"""Endpoint tests for the health prober and interactive Cline re-auth."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from kiterouter import cline_auth, server
from kiterouter.prober import HealthProber
from kiterouter.server import app

BASE = "http://test"


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


# ------------------------------------------------------------------- prober


@pytest.mark.asyncio
async def test_get_prober_reports_state():
    async with client() as c:
        resp = await c.get("/api/prober")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert "interval_seconds" in body
    assert "providers" in body


@pytest.mark.asyncio
async def test_post_prober_enables_and_disables(monkeypatch):
    monkeypatch.setattr(HealthProber, "start", lambda self: None)
    monkeypatch.setattr(HealthProber, "stop", AsyncMock())
    monkeypatch.setattr(server.config, "save", lambda: None)
    monkeypatch.setattr(server.config, "enable_prober", False)

    async with client() as c:
        enabled = await c.post("/api/prober", json={"enabled": True, "interval_seconds": 120})
        assert enabled.status_code == 200
        assert server.config.enable_prober is True
        assert server.config.prober_interval_seconds == 120
        assert server.prober.interval_seconds == 120

        disabled = await c.post("/api/prober", json={"enabled": False})
        assert disabled.status_code == 200
        assert server.config.enable_prober is False


@pytest.mark.asyncio
async def test_post_prober_clamps_interval_to_a_sane_minimum(monkeypatch):
    monkeypatch.setattr(HealthProber, "stop", AsyncMock())
    monkeypatch.setattr(server.config, "save", lambda: None)
    monkeypatch.setattr(server.config, "enable_prober", False)
    monkeypatch.setattr(server.config, "prober_interval_seconds", 900)
    async with client() as c:
        await c.post("/api/prober", json={"enabled": False, "interval_seconds": 1})
    assert server.config.prober_interval_seconds == 60


@pytest.mark.asyncio
async def test_post_prober_run_executes_a_sweep(monkeypatch):
    monkeypatch.setattr(
        HealthProber, "probe_once", AsyncMock(return_value={"summary": {"ok": 2}})
    )
    async with client() as c:
        resp = await c.post("/api/prober/run")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_health_includes_prober_state():
    async with client() as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert "prober" in resp.json()


@pytest.mark.asyncio
async def test_lifespan_starts_prober_only_when_enabled(monkeypatch):
    """The prober stays dormant unless the config opts in."""
    started = []
    monkeypatch.setattr(HealthProber, "start", lambda self: started.append(True))
    monkeypatch.setattr(HealthProber, "stop", AsyncMock())

    monkeypatch.setattr(server.config, "enable_prober", False)
    async with server.lifespan(app):
        pass
    assert started == []

    monkeypatch.setattr(server.config, "enable_prober", True)
    async with server.lifespan(app):
        pass
    assert started == [True]


# ------------------------------------------------------- cline re-auth API


@pytest.mark.asyncio
async def test_cline_auth_status_reports_honest_state(monkeypatch):
    fake = AsyncMock(
        return_value={
            "has_access_token": True,
            "reauth_required": True,
            "last_auth_error": "re-authenticate",
        }
    )
    monkeypatch.setattr(type(server.router.providers["cline"]), "auth_status", fake)
    monkeypatch.setattr(
        server.TokenFetcher, "fetch_cline_credentials", staticmethod(lambda: {"source": "cline-cli"})
    )

    async with client() as c:
        resp = await c.get("/api/cline/auth/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["reauth_required"] is True
    assert body["local_session_available"] is True
    assert body["local_session_source"] == "cline-cli"


@pytest.mark.asyncio
async def test_cline_auth_start_returns_device_code_without_leaking_it():
    device = {
        "device_code": "secret-device-code",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://authkit.cline.bot/device",
        "verification_uri_complete": "https://authkit.cline.bot/device?user_code=ABCD-EFGH",
        "expires_in": 300,
        "interval": 5,
    }
    with patch.object(cline_auth, "start_device_flow", AsyncMock(return_value=device)):
        async with client() as c:
            resp = await c.post("/api/cline/auth/start")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["user_code"] == "ABCD-EFGH"
    assert "device_code" not in body
    assert body["flow_id"] in server._pending_cline_flows


@pytest.mark.asyncio
async def test_cline_auth_start_surfaces_upstream_failure():
    with patch.object(
        cline_auth, "start_device_flow", AsyncMock(side_effect=cline_auth.DeviceFlowError("boom"))
    ):
        async with client() as c:
            resp = await c.post("/api/cline/auth/start")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_cline_auth_poll_reports_pending():
    device = {
        "device_code": "dev",
        "user_code": "CODE",
        "verification_uri": "https://authkit.cline.bot/device",
        "verification_uri_complete": None,
        "expires_in": 300,
        "interval": 5,
    }
    with patch.object(cline_auth, "start_device_flow", AsyncMock(return_value=device)):
        async with client() as c:
            flow_id = (await c.post("/api/cline/auth/start")).json()["flow_id"]

    with patch.object(
        cline_auth, "complete_device_flow", AsyncMock(side_effect=cline_auth.AuthorizationPending())
    ):
        async with client() as c:
            resp = await c.post(f"/api/cline/auth/poll?flow_id={flow_id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_cline_auth_poll_rejects_unknown_flow():
    async with client() as c:
        resp = await c.post("/api/cline/auth/poll?flow_id=does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cline_auth_poll_persists_session_and_rebuilds_router(monkeypatch):
    device = {
        "device_code": "dev",
        "user_code": "CODE",
        "verification_uri": "https://authkit.cline.bot/device",
        "verification_uri_complete": None,
        "expires_in": 300,
        "interval": 5,
    }
    session = {
        "access_token": "session-token",
        "refresh_token": "session-refresh",
        "expires_at": "2026-09-16T18:11:42Z",
        "email": "dev@example.com",
        "accounts": ["acc"],
    }
    saved = {}
    monkeypatch.setattr(
        server.config, "update_provider_tokens", lambda p, u: saved.update({p: u})
    )

    with patch.object(cline_auth, "start_device_flow", AsyncMock(return_value=device)):
        async with client() as c:
            flow_id = (await c.post("/api/cline/auth/start")).json()["flow_id"]

    with patch.object(cline_auth, "complete_device_flow", AsyncMock(return_value=session)):
        async with client() as c:
            resp = await c.post(f"/api/cline/auth/poll?flow_id={flow_id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert saved["cline"]["access_token"] == "session-token"
    assert saved["cline"]["expires_at"] == 1789582302
    assert flow_id not in server._pending_cline_flows


@pytest.mark.asyncio
async def test_cline_auth_poll_warns_when_account_has_no_workspace(monkeypatch):
    device = {
        "device_code": "dev",
        "user_code": "CODE",
        "verification_uri": "https://authkit.cline.bot/device",
        "verification_uri_complete": None,
        "expires_in": 300,
        "interval": 5,
    }
    session = {
        "access_token": "t",
        "refresh_token": "r",
        "expires_at": None,
        "email": "dev@example.com",
        "accounts": None,
    }
    monkeypatch.setattr(server.config, "update_provider_tokens", lambda p, u: None)

    with patch.object(cline_auth, "start_device_flow", AsyncMock(return_value=device)):
        async with client() as c:
            flow_id = (await c.post("/api/cline/auth/start")).json()["flow_id"]

    with patch.object(cline_auth, "complete_device_flow", AsyncMock(return_value=session)):
        async with client() as c:
            resp = await c.post(f"/api/cline/auth/poll?flow_id={flow_id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "warning"


@pytest.mark.asyncio
async def test_cline_auth_poll_abandons_denied_flow():
    device = {
        "device_code": "dev",
        "user_code": "CODE",
        "verification_uri": "https://authkit.cline.bot/device",
        "verification_uri_complete": None,
        "expires_in": 300,
        "interval": 5,
    }
    with patch.object(cline_auth, "start_device_flow", AsyncMock(return_value=device)):
        async with client() as c:
            flow_id = (await c.post("/api/cline/auth/start")).json()["flow_id"]

    with patch.object(
        cline_auth, "complete_device_flow", AsyncMock(side_effect=cline_auth.DeviceFlowError("denied"))
    ):
        async with client() as c:
            resp = await c.post(f"/api/cline/auth/poll?flow_id={flow_id}")

    assert resp.status_code == 400
    assert flow_id not in server._pending_cline_flows


@pytest.mark.asyncio
async def test_expired_flow_is_rejected():
    flow_id = "flow_expired"
    server._pending_cline_flows[flow_id] = {"device_code": "dev", "expires_at": 1}
    async with client() as c:
        resp = await c.post(f"/api/cline/auth/poll?flow_id={flow_id}")
    assert resp.status_code == 410
    assert flow_id not in server._pending_cline_flows
