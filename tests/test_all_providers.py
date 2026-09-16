"""Tests for all 13 providers and configuration endpoints."""
import pytest
from unittest.mock import AsyncMock, patch

from kiterouter.config import KiteConfig
from kiterouter.providers.opencode_free import OpenCodeFreeProvider
from kiterouter.router import ProviderRouter as KiteRouter, ProviderRouter
from kiterouter.providers.base import BaseProvider

# Stubbed upstream completion: these endpoint tests are about gateway behaviour,
# not upstream latency. Left live, the suite ranged from 3s to 5 minutes.
FAKE_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def test_all_13_providers_registered():
    config = KiteConfig()
    router = KiteRouter(config)
    
    expected_providers = [
        "cursor",
        "antigravity",
        "cline",
        "opencode_free",
        "opencode_go",
        "kiro",
        "glm",
        "minimax",
        "codex",
        "command_code",
        "claude",
        "copilot",
        "vertex",
        "custom",
    ]
    
    for prov_id in expected_providers:
        assert prov_id in router.providers, f"Provider {prov_id} is not registered in KiteRouter"
        provider = router.providers[prov_id]
        assert isinstance(provider, BaseProvider)
        assert len(provider.get_models()) > 0


def test_prefix_routing_for_providers():
    config = KiteConfig()
    router = KiteRouter(config)
    
    provider, model = router.select_provider("kiro/claude-3-5-sonnet")
    assert provider.name == "kiro"
    assert model == "claude-3-5-sonnet"

    provider, model = router.select_provider("glm/glm-4-flash")
    assert provider.name == "glm"
    assert model == "glm-4-flash"

    provider, model = router.select_provider("minimax/abab6.5s-chat")
    assert provider.name == "minimax"
    assert model == "abab6.5s-chat"

    provider, model = router.select_provider("codex/o3-mini")
    assert provider.name == "codex"
    assert model == "o3-mini"

    provider, model = router.select_provider("claude/claude-3-7-sonnet")
    assert provider.name == "claude"
    assert model == "claude-3-7-sonnet"

    provider, model = router.select_provider("copilot/gpt-4o")
    assert provider.name == "copilot"
    assert model == "gpt-4o"

    provider, model = router.select_provider("vertex/gemini-2.5-flash")
    assert provider.name == "vertex"
    assert model == "gemini-2.5-flash"

    provider, model = router.select_provider("custom/local-llama")
    assert provider.name == "custom"
    assert model == "local-llama"


@pytest.mark.asyncio
@patch.object(
    OpenCodeFreeProvider, "fetch_models", AsyncMock(return_value=["model-a", "model-b"])
)
@patch.object(OpenCodeFreeProvider, "chat_complete", AsyncMock(return_value=FAKE_COMPLETION))
async def test_config_api_endpoints():
    from httpx import AsyncClient, ASGITransport
    from kiterouter.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET config
        res = await client.get("/api/config")
        assert res.status_code == 200
        data = res.json()
        assert "providers" in data
        assert "cursor" in data["providers"]
        assert "kiro" in data["providers"]

        # POST config update
        update_res = await client.post(
            "/api/config",
            json={"providers": {"glm": {"enabled": True, "api_key": "test_glm_key"}}}
        )
        assert update_res.status_code == 200
        assert update_res.json()["status"] == "saved"

        # Verify updated config (keys are masked by get_config)
        res_after = await client.get("/api/config")
        assert res_after.json()["providers"]["glm"]["api_key"].endswith("_key")

        # Test single provider test endpoint
        test_prov_res = await client.post("/api/test-provider", json={"provider": "opencode_free"})
        assert test_prov_res.status_code == 200
        assert "status" in test_prov_res.json()

        # Test fetch-models endpoint
        fetch_res = await client.post("/api/fetch-models", json={"provider": "opencode_free"})
        assert fetch_res.status_code == 200
        fetch_data = fetch_res.json()
        assert fetch_data["status"] == "success"
        assert fetch_data["count"] > 0
        assert isinstance(fetch_data["models"], list)

        # Test fetch-token endpoint
        tok_res = await client.post("/api/fetch-token", json={"provider": "cursor"})
        assert tok_res.status_code == 200
        tok_data = tok_res.json()
        assert tok_data["status"] == "success"
        assert tok_data["has_token"] is True

        # Test sync-source from 9router
        sync_res = await client.post("/api/sync-source", json={"source": "9router"})
        assert sync_res.status_code == 200
        sync_data = sync_res.json()
        assert sync_data["status"] == "success"
        assert sync_data["imported_count"] > 0
        assert "antigravity" in sync_data["imported_providers"]
