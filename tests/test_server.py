import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from starlette.testclient import TestClient
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
async def test_list_models_includes_imported_providers():
    from kiterouter.server import router, app
    router.config["imported_custom"] = {
        "enabled": True,
        "models": ["custom-model-1", "custom-model-2"]
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.get("/v1/models")
        assert resp1.status_code == 200
        models1 = [m["id"] for m in resp1.json()["data"]]
        assert "imported_custom/custom-model-1" in models1 or "custom-model-1" in models1

        resp2 = await client.get("/api/v1/models")
        assert resp2.status_code == 200
        models2 = [m["id"] for m in resp2.json()["data"]]
        assert "imported_custom/custom-model-1" in models2 or "custom-model-1" in models2


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


@pytest.mark.asyncio
async def test_get_config_recursively_redacts_all_secrets(monkeypatch, tmp_path):
    from kiterouter import server

    # Set up config with various secret-like fields and nested structures
    test_providers = {
        "cursor": {
            "enabled": True,
            "token": "secret_cursor_token_12345",
            "access_token": "secret_cursor_access_token_67890",
            "refresh_token": "secret_cursor_refresh_token_abcde",
            "machine_id": "non_secret_machine_id_val",
        },
        "custom": {
            "enabled": True,
            "endpoint": "https://api.example.com",
            "api_key": "sk-secret-key-9999",
            "client_secret": "sensitive_client_secret_xyz",
            "nested_auth": {
                "bearer_token": "nested_secret_token_1111",
                "normal_field": "safe_value",
            },
        },
    }
    monkeypatch.setattr(server.config, "providers", test_providers)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        providers = data["providers"]

        cursor_p = providers["cursor"]
        # token must be redacted
        assert cursor_p["token"] == "...2345"
        # access_token and refresh_token MUST NOT be leaked in plaintext
        assert cursor_p["access_token"] == "...7890"
        assert cursor_p["refresh_token"] == "...bcde"
        assert cursor_p["machine_id"] == "non_secret_machine_id_val"

        custom_p = providers["custom"]
        assert custom_p["api_key"] == "...9999"
        assert custom_p["client_secret"] == "..._xyz"
        assert custom_p["nested_auth"]["bearer_token"] == "...1111"
        assert custom_p["nested_auth"]["normal_field"] == "safe_value"


@pytest.mark.asyncio
async def test_post_config_preserves_secrets_when_placeholders_sent(monkeypatch, tmp_path):
    from kiterouter import server

    test_providers = {
        "cursor": {
            "enabled": True,
            "token": "original_cursor_token_12345",
            "access_token": "original_cursor_access_token_67890",
            "refresh_token": "original_cursor_refresh_token_abcde",
        }
    }
    monkeypatch.setattr(server.config, "providers", test_providers)
    monkeypatch.setattr(server.config, "save", lambda: None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "providers": {
                "cursor": {
                    "token": "...2345",
                    "access_token": "******",
                    "refresh_token": "••••••••",
                }
            }
        }
        resp = await client.post("/api/config", json=payload)
        assert resp.status_code == 200

        # Verify underlying secrets were NOT replaced by placeholders
        assert server.config.providers["cursor"]["token"] == "original_cursor_token_12345"
        assert server.config.providers["cursor"]["access_token"] == "original_cursor_access_token_67890"
        assert server.config.providers["cursor"]["refresh_token"] == "original_cursor_refresh_token_abcde"


@pytest.mark.asyncio
async def test_post_fetch_all_models():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/fetch-all-models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["total_models"] > 0
        assert "providers" in data
        assert "opencode_free" in data["providers"]
        assert len(data["providers"]["opencode_free"]["models"]) > 0


@pytest.mark.asyncio
async def test_test_model_endpoint(monkeypatch):
    from kiterouter import server

    monkeypatch.setattr(server.config, "save", lambda: None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"provider": "opencode_free", "model": "claude-3-5-sonnet"}
        resp = await client.post("/api/test-model", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert data["status"] in ("ok", "error")
        assert "latency_ms" in data
        # Check that it recorded in config
        prov_conf = server.config.providers.get("opencode_free", {})
        assert "test_results" in prov_conf
        assert "claude-3-5-sonnet" in prov_conf["test_results"]
        assert "last_test_status" in prov_conf


@pytest.mark.asyncio
async def test_test_all_models_endpoint(monkeypatch):
    from kiterouter import server

    monkeypatch.setattr(server.config, "save", lambda: None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Request with a limit or test all
        resp = await client.post("/api/test-all-models", json={"max_per_provider": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "tested_count" in data
        assert "results" in data


@pytest.mark.asyncio
async def test_claude_header_list_sanitization():
    from kiterouter.providers.claude import ClaudeProvider

    p = ClaudeProvider({
        "api_key": "test_key",
        "headers": {"anthropic-beta": ["beta-1", "beta-2"], "anthropic-version": ["2023-06-01"]},
    })
    headers = p._build_headers()
    # Header values MUST be str, not list
    assert isinstance(headers["anthropic-beta"], str)
    assert headers["anthropic-beta"] == "beta-1,beta-2"
    assert isinstance(headers["anthropic-version"], str)
    assert headers["anthropic-version"] == "2023-06-01"


def test_cursor_envelope_construction():
    from kiterouter.providers.cursor import build_connect_envelope

    payload = {"model": "claude-3-5-sonnet", "messages": []}
    envelope = build_connect_envelope(payload)
    assert len(envelope) >= 5
    assert envelope[0] == 0  # flag 0
    msg_len = int.from_bytes(envelope[1:5], "big")
    assert len(envelope) == 5 + msg_len


def test_kiro_endpoint_url():
    from kiterouter.providers.kiro import KiroProvider

    p1 = KiroProvider({})
    assert "api.kiro.ai" not in p1.endpoint
    assert p1.endpoint.startswith("http")

    p2 = KiroProvider({"base_url": "https://api.kilocode.ai"})
    assert p2.endpoint == "https://api.kilocode.ai/v1/chat/completions"


@pytest.mark.asyncio
async def test_recent_requests_logging(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from kiterouter.server import app, router

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        # Mock opencode_free stream_chat
        target = router.providers["opencode_free"]

        async def mock_stream(model, messages, **kwargs):
            yield 'data: {"choices": [{"delta": {"content": "Hello!"}}]}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(target, "stream_chat", mock_stream)

        # Send completion request
        res = await ac.post(
            "/v1/chat/completions",
            json={
                "model": "opencode_free/gpt-4o",
                "messages": [{"role": "user", "content": "Ping"}],
                "stream": False,
            },
        )
        assert res.status_code == 200

        # Query recent-requests
        rec_res = await ac.get("/api/recent-requests?limit=5")
        assert rec_res.status_code == 200
        data = rec_res.json()
        assert data["status"] == "success"
        assert len(data["requests"]) > 0
        latest = data["requests"][0]
        assert latest["model"] == "opencode_free/gpt-4o"
        assert latest["provider"] == "opencode_free"
        assert latest["status"] == "ok"
        assert latest["tokens_in"] >= 1
        assert latest["tokens_out"] >= 1
        assert "id" in latest
        assert "timestamp" in latest


def test_sync_source_does_not_alter_port():
    from kiterouter.server import app
    from kiterouter import server

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_cfg = Path(tmpdir) / "config.json"
        with patch("kiterouter.config.CONFIG_DIR", Path(tmpdir)), patch(
            "kiterouter.config.CONFIG_FILE", fake_cfg
        ):
            server.config.port = 3001
            server.config.save()

            # Mock fetch_from_omniroute returning many providers plus an attempt to tamper port
            fake_omni = {
                "groq": {"api_key": "gsk_test", "source": "omniroute"},
                "command_code": {"api_key": "cmd_test", "source": "omniroute"},
                "port": {"port": 20128},
            }
            with patch("kiterouter.token_fetcher.TokenFetcher.fetch_from_omniroute", return_value=fake_omni):
                client = TestClient(app)
                res = client.post("/api/sync-source", json={"source": "omniroute"})
                assert res.status_code == 200

                # Verify in-memory config port is strictly 3001
                assert server.config.port == 3001

                # Verify persisted file port is strictly 3001 and NEVER 20128
                with open(fake_cfg, "r") as f:
                    saved_data = json.load(f)
                assert saved_data.get("port") == 3001
                assert saved_data.get("port") != 20128


def test_config_save_guards_port():
    from kiterouter.config import KiteConfig

    cfg = KiteConfig(port=20128)
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_cfg = Path(tmpdir) / "config.json"
        with patch("kiterouter.config.CONFIG_DIR", Path(tmpdir)), patch(
            "kiterouter.config.CONFIG_FILE", fake_cfg
        ):
            cfg.save()
            with open(fake_cfg, "r") as f:
                saved = json.load(f)
            assert saved["port"] == 3001
            assert cfg.port == 3001




