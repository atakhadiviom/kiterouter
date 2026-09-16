"""Tests for Command Code provider integration."""
import pytest
from unittest.mock import AsyncMock, patch
import httpx

from kiterouter.config import KiteConfig
from kiterouter.router import ProviderRouter
from kiterouter.providers.command_code import CommandCodeProvider


def test_command_code_config_default():
    config = KiteConfig()
    assert "command_code" in config.providers
    assert config.providers["command_code"] == {"enabled": True, "api_key": ""}


def test_command_code_router_registration_and_prefixes():
    router = ProviderRouter()
    assert "command_code" in router.providers
    assert isinstance(router.providers["command_code"], CommandCodeProvider)

    # Fallback chain order: command_code after codex
    assert "codex" in router.fallback_chain
    assert "command_code" in router.fallback_chain
    codex_idx = router.fallback_chain.index("codex")
    cmd_idx = router.fallback_chain.index("command_code")
    assert cmd_idx == codex_idx + 1

    # Routing prefixes
    prov, model = router.parse_model_and_provider("cmd/claude-opus-4-7")
    assert prov == "command_code"
    assert model == "claude-opus-4-7"

    prov, model = router.parse_model_and_provider("ccp/claude-opus-4-7")
    assert prov == "command_code"
    assert model == "claude-opus-4-7"

    prov, model = router.parse_model_and_provider("command-code/gpt-5.5")
    assert prov == "command_code"
    assert model == "gpt-5.5"

    prov, model = router.parse_model_and_provider("command_code/gpt-5.4")
    assert prov == "command_code"
    assert model == "gpt-5.4"

    # Verify "cc" prefix still strictly maps to claude, NOT command_code
    prov, model = router.parse_model_and_provider("cc/claude-3-5-sonnet")
    assert prov == "claude"
    assert model == "claude-3-5-sonnet"


def test_command_code_adapter_endpoints_and_auth():
    provider = CommandCodeProvider(config={"api_key": "test-cmd-key"})
    assert provider.endpoint == "https://api.commandcode.ai/provider/v1/chat/completions"
    assert provider.models_endpoint == "https://api.commandcode.ai/provider/v1/models"
    assert provider.api_key == "test-cmd-key"

    # Default catalog
    expected_models = [
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "gpt-5.5",
        "gpt-5.4",
    ]
    for m in expected_models:
        assert m in provider.supported_models or f"cmd/{m}" in provider.supported_models


@pytest.mark.asyncio
async def test_command_code_fetch_models_fallback_on_failure():
    provider = CommandCodeProvider(config={"api_key": "test-key"})
    # When network fails, fetch_models falls back to default catalog
    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Network unreachable")):
        models = await provider.fetch_models()
        assert len(models) >= 6
        assert any("claude-opus-4-7" in m for m in models)


@pytest.mark.asyncio
async def test_command_code_fetch_models_success():
    provider = CommandCodeProvider(config={"api_key": "valid-key"})
    mock_response = httpx.Response(
        200,
        json={"data": [{"id": "cmd-model-1"}, {"id": "cmd-model-2"}]},
        request=httpx.Request("GET", "https://api.commandcode.ai/provider/v1/models"),
    )
    with patch("httpx.AsyncClient.get", return_value=mock_response):
        models = await provider.fetch_models()
        assert "cmd-model-1" in models
        assert "cmd-model-2" in models


def test_command_code_token_fetcher_omniroute_mapping():
    from kiterouter.token_fetcher import TokenFetcher
    import sqlite3
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "storage.sqlite"
        conn = sqlite3.connect(fake_db)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE provider_connections (
                provider TEXT,
                auth_type TEXT,
                email TEXT,
                access_token TEXT,
                refresh_token TEXT,
                api_key TEXT,
                project_id TEXT,
                provider_specific_data TEXT,
                is_active INTEGER,
                updated_at TEXT
            )
        """)
        c.execute("""
            INSERT INTO provider_connections 
            (provider, auth_type, email, access_token, refresh_token, api_key, project_id, provider_specific_data, is_active, updated_at)
            VALUES 
            ('command-code', 'apikey', 'user@cmd.ai', NULL, NULL, 'test-cmd-key-123', NULL, NULL, 1, '2026-09-16 12:00:00')
        """)
        conn.commit()
        conn.close()

        with patch("pathlib.Path.home", return_value=Path(tmpdir)):
            omni_dir = Path(tmpdir) / ".omniroute"
            omni_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy(fake_db, omni_dir / "storage.sqlite")

            res = TokenFetcher.fetch_from_omniroute()
            assert "command_code" in res
            assert res["command_code"]["api_key"] == "test-cmd-key-123"
            assert res["command_code"]["email"] == "user@cmd.ai"
