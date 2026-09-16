import pytest
from kiterouter.router import ProviderRouter


@pytest.mark.asyncio
async def test_provider_router_model_parsing():
    router = ProviderRouter()
    prov, model = router.parse_model_and_provider("cursor/claude-3-5-sonnet")
    assert prov == "cursor"
    assert model == "claude-3-5-sonnet"

    prov2, model2 = router.parse_model_and_provider("claude-3-5-sonnet")
    assert prov2 is None
    assert model2 == "claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_provider_router_fallback_mock():
    config = {
        "opencode_free": {"mock": True},
        "opencode_go": {"mock": True},
        "cursor": {"mock": True},
        "antigravity": {"mock": True},
        "cline": {"mock": True},
    }
    router = ProviderRouter(config=config)

    chunks = []
    async for chunk in router.stream_with_fallback(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "hello"}],
    ):
        chunks.append(chunk)

    assert len(chunks) > 0
    full = "".join(chunks)
    assert "data:" in full
    assert "[DONE]" in full
