import pytest
from httpx import ASGITransport, AsyncClient
from kiterouter.server import app


@pytest.mark.asyncio
async def test_server_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "available_providers" in data


@pytest.mark.asyncio
async def test_server_models():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) > 0


@pytest.mark.asyncio
async def test_server_chat_completions_non_stream():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert "choices" in data
