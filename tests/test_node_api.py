"""Tests for the provider-node endpoints and their dashboard rendering."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from kiterouter import server
from kiterouter.server import app


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def node_provider(monkeypatch):
    """A node provider in an isolated providers dict."""
    providers = {"n1": {"kind": "node", "enabled": True, "base_url": "https://api.x.com/v1", "api_key": "k"}}
    monkeypatch.setattr(server.config, "providers", providers)
    saved = {}
    monkeypatch.setattr(
        server.config, "update_provider_tokens", lambda p, u: saved.update({p: u})
    )
    return providers, saved


@pytest.mark.asyncio
async def test_validate_rejects_a_non_node_provider(monkeypatch):
    monkeypatch.setattr(server.config, "providers", {"plain": {"api_key": "k"}})
    async with client() as c:
        resp = await c.post("/api/providers/node/validate", json={"provider": "plain"})
    assert resp.status_code == 400
    assert "not a provider node" in resp.json()["message"]


@pytest.mark.asyncio
async def test_validate_records_verification_only_after_a_real_completion(node_provider, monkeypatch):
    _, saved = node_provider
    monkeypatch.setattr(server.NodeProvider, "fetch_models", AsyncMock(return_value=["m1", "m2"]))
    monkeypatch.setattr(
        server,
        "probe_stream",
        AsyncMock(return_value={"text": "hello", "latency_ms": 120, "ttft_ms": 40, "error": None}),
    )

    async with client() as c:
        resp = await c.post("/api/providers/node/validate", json={"provider": "n1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["model_count"] == 2
    assert body["completion"]["latency_ms"] == 120
    assert body["completion"]["ttft_ms"] == 40
    assert saved["n1"]["verified_at"] is not None, "a passing probe should verify the node"
    assert saved["n1"]["last_error"] is None


@pytest.mark.asyncio
async def test_validate_does_not_verify_on_a_failing_completion(node_provider, monkeypatch):
    _, saved = node_provider
    monkeypatch.setattr(server.NodeProvider, "fetch_models", AsyncMock(return_value=["m1"]))
    monkeypatch.setattr(
        server,
        "probe_stream",
        AsyncMock(
            return_value={"text": "HTTP 401 unauthorized", "latency_ms": 90, "ttft_ms": None, "error": None}
        ),
    )

    async with client() as c:
        resp = await c.post("/api/providers/node/validate", json={"provider": "n1"})

    body = resp.json()
    assert body["verified"] is False, "an error body is not a working provider"
    assert saved["n1"]["verified_at"] is None
    assert "401" in saved["n1"]["last_error"]


@pytest.mark.asyncio
async def test_validate_does_not_verify_when_the_probe_raises(node_provider, monkeypatch):
    _, saved = node_provider
    monkeypatch.setattr(server.NodeProvider, "fetch_models", AsyncMock(return_value=["m1"]))
    monkeypatch.setattr(
        server,
        "probe_stream",
        AsyncMock(return_value={"text": "", "latency_ms": 50, "ttft_ms": None, "error": "ConnectError: refused"}),
    )

    async with client() as c:
        resp = await c.post("/api/providers/node/validate", json={"provider": "n1"})

    body = resp.json()
    assert body["verified"] is False
    assert "refused" in body["completion"]["error"]
    assert saved["n1"]["verified_at"] is None


@pytest.mark.asyncio
async def test_validate_explains_itself_when_there_is_no_model_to_probe(node_provider, monkeypatch):
    monkeypatch.setattr(server.NodeProvider, "fetch_models", AsyncMock(return_value=[]))
    async with client() as c:
        resp = await c.post("/api/providers/node/validate", json={"provider": "n1"})

    body = resp.json()
    assert body["verified"] is False
    assert "no model to probe" in body["message"].lower()


@pytest.mark.asyncio
async def test_validate_uses_an_explicit_model_when_given(node_provider, monkeypatch):
    monkeypatch.setattr(server.NodeProvider, "fetch_models", AsyncMock(return_value=["m1", "m2"]))
    probe = AsyncMock(return_value={"text": "ok", "latency_ms": 10, "ttft_ms": 5, "error": None})
    monkeypatch.setattr(server, "probe_stream", probe)

    async with client() as c:
        await c.post("/api/providers/node/validate", json={"provider": "n1", "model": "m2"})

    assert probe.await_args.args[1] == "m2"


@pytest.mark.asyncio
async def test_validate_never_echoes_the_credential(node_provider, monkeypatch):
    monkeypatch.setattr(server.NodeProvider, "fetch_models", AsyncMock(return_value=["m1"]))
    monkeypatch.setattr(
        server,
        "probe_stream",
        AsyncMock(return_value={"text": "ok", "latency_ms": 10, "ttft_ms": 5, "error": None}),
    )

    async with client() as c:
        resp = await c.post("/api/providers/node/validate", json={"provider": "n1"})

    assert '"k"' not in resp.text
    assert resp.json()["describe"]["has_credential"] is True


@pytest.mark.asyncio
async def test_list_nodes_reports_only_nodes(monkeypatch):
    monkeypatch.setattr(
        server.config,
        "providers",
        {
            "n1": {"kind": "node", "base_url": "https://a/v1", "prefix": "ds"},
            "plain": {"api_key": "x"},
        },
    )
    async with client() as c:
        resp = await c.get("/api/providers/nodes")

    body = resp.json()
    assert [n["id"] for n in body["nodes"]] == ["n1"]
    assert body["nodes"][0]["api_type"] == "openai-compatible"
    assert body["nodes"][0]["models_url"] == "https://a/v1/models"
