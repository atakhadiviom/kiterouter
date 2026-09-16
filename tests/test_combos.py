"""Tests for KiteRouter Combos feature."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient

from kiterouter.config import KiteConfig
from kiterouter.router import ProviderRouter
from kiterouter.server import app, router as global_router
from kiterouter.token_fetcher import TokenFetcher


@pytest.fixture
def mock_router():
    config = {
        "providers": {
            "opencode_go": {"enabled": True, "api_key": "test"},
            "opencode_free": {"enabled": True},
        },
        "combos": {
            "test_combo": {
                "name": "test_combo",
                "strategy": "fallback",
                "models": ["opencode_go/deepseek-flash", "opencode_free/nemotron-3-ultra-free"],
                "description": "Test combo description",
            }
        }
    }
    return ProviderRouter(config=config)


def test_combo_in_model_catalog(mock_router):
    models = mock_router.get_all_models()
    model_ids = [m["id"] for m in models]
    assert "combo/test_combo" in model_ids
    assert "test_combo" in model_ids

    combo_entry = next(m for m in models if m["id"] == "combo/test_combo")
    assert combo_entry["owned_by"] == "kiterouter-combo"
    assert combo_entry["is_combo"] is True
    assert combo_entry["strategy"] == "fallback"
    assert len(combo_entry["models"]) == 2


def test_get_combo(mock_router):
    assert mock_router.get_combo("test_combo") is not None
    assert mock_router.get_combo("combo/test_combo") is not None
    assert mock_router.get_combo("nonexistent") is None


@pytest.mark.asyncio
async def test_stream_combo_fallback():
    r = ProviderRouter()
    r.combos = {
        "smart_chain": {
            "name": "smart_chain",
            "strategy": "fallback",
            "models": ["mock_fail/fail-model", "mock_succ/succ-model"],
        }
    }

    # Setup 2 mock providers
    p_fail = MagicMock()
    p_fail.name = "mock_fail"
    p_fail.is_available = AsyncMock(return_value=True)

    async def fail_stream(*args, **kwargs):
        raise RuntimeError("Provider connection refused")
        yield "never"

    p_fail.stream_chat = fail_stream

    p_succ = MagicMock()
    p_succ.name = "mock_succ"
    p_succ.is_available = AsyncMock(return_value=True)

    async def succ_stream(*args, **kwargs):
        yield 'data: {"choices":[{"delta":{"content":"Hello from fallback!"}}]}\n\n'
        yield 'data: [DONE]\n\n'

    p_succ.stream_chat = succ_stream

    r.providers["mock_fail"] = p_fail
    r.providers["mock_succ"] = p_succ

    chunks = []
    combo = r.get_combo("smart_chain")
    assert combo is not None
    async for chunk in r.stream_combo(combo, [{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    full_text = "".join(chunks)
    assert "Hello from fallback!" in full_text
    assert r.metrics["fallback_events"] >= 1
    assert r.metrics["successful_requests"] == 1


@pytest.mark.asyncio
async def test_stream_combo_round_robin():
    r = ProviderRouter()
    r.combos = {
        "rr_chain": {
            "name": "rr_chain",
            "strategy": "round-robin",
            "models": ["p1/m1", "p2/m2"],
        }
    }

    called = []

    def make_provider(name, tag):
        p = MagicMock()
        p.name = name
        p.is_available = AsyncMock(return_value=True)

        async def stream(*args, **kwargs):
            called.append(tag)
            yield f'data: {{"choices":[{{"delta":{{"content":"{tag}"}}}}]}}\n\n'
            yield 'data: [DONE]\n\n'

        p.stream_chat = stream
        return p

    r.providers["p1"] = make_provider("p1", "TAG1")
    r.providers["p2"] = make_provider("p2", "TAG2")

    combo = r.get_combo("rr_chain")
    assert combo is not None

    # Call 1 -> should call TAG1
    async for _ in r.stream_combo(combo, []):
        pass
    assert called == ["TAG1"]

    # Call 2 -> should rotate to TAG2
    async for _ in r.stream_combo(combo, []):
        pass
    assert called == ["TAG1", "TAG2"]


def test_combo_api_endpoints():
    client = TestClient(app)

    # 1. Create a combo
    res = client.post("/api/combos", json={
        "name": "api_test_combo",
        "strategy": "fallback",
        "models": ["opencode_go/deepseek-flash", "opencode_free/nemotron-3-ultra-free"],
        "description": "API created combo",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["combo"]["name"] == "api_test_combo"

    # 2. List combos
    res = client.get("/api/combos")
    assert res.status_code == 200
    combos = res.json()["combos"]
    assert any(c["name"] == "api_test_combo" for c in combos)

    # 3. Update combo
    res = client.put("/api/combos/api_test_combo", json={
        "strategy": "round-robin",
        "description": "Updated description",
    })
    assert res.status_code == 200
    assert res.json()["combo"]["strategy"] == "round-robin"

    # 4. Test combo endpoint
    with patch.object(global_router, "stream_combo") as mock_stream:
        async def fake_stream(*args, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"pong"}}]}\n\n'
            yield 'data: [DONE]\n\n'

        mock_stream.side_effect = fake_stream
        res = client.post("/api/test-combo", json={"name": "api_test_combo", "prompt": "ping"})
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    # 5. Delete combo
    res = client.delete("/api/combos/api_test_combo")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify deleted
    res = client.get("/api/combos")
    assert not any(c["name"] == "api_test_combo" for c in res.json()["combos"])


def test_import_9router_combos_endpoint():
    client = TestClient(app)
    with patch("kiterouter.token_fetcher.TokenFetcher.fetch_combos_from_9router") as mock_fetch:
        mock_fetch.return_value = {
            "Own": {
                "id": "mock-id-123",
                "name": "Own",
                "strategy": "fallback",
                "models": ["cursor/composer-2.5", "opencode_free/nemotron-3-ultra-free"],
                "source": "9router",
            }
        }
        res = client.post("/api/combos/import-9router")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "Own" in data["imported_combos"]

        # Clean up
        client.delete("/api/combos/Own")
