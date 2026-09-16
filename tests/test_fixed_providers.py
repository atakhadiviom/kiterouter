"""Tests for updated providers: Antigravity, Cline, OpenCode Free, and OpenCode Go."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kiterouter.providers.antigravity import (
    AntigravityProvider,
    ANTIGRAVITY_TOKEN_URL,
)
from kiterouter.providers.cline import ClineProvider, CLINE_API_BASE, OPENROUTER_CHAT_URL as OPENROUTER_API_BASE
from kiterouter.providers.opencode_free import (
    OpenCodeFreeProvider,
    build_opencode_headers,
    OPENCODE_FREE_ENDPOINT,
)
from kiterouter.providers.opencode_go import (
    OpenCodeGoProvider,
    build_opencode_go_headers,
    OPENCODE_GO_ENDPOINT,
)


# ==========================================
# OpenCode Free Tests
# ==========================================

def test_opencode_free_headers():
    headers = build_opencode_headers()
    assert headers["Authorization"] == "Bearer public"
    assert headers["User-Agent"] == "opencode"
    assert headers["x-opencode-client"] == "desktop"
    assert headers["x-opencode-session"].startswith("ses_")
    assert headers["x-opencode-request"].startswith("msg_")


def test_opencode_free_endpoint():
    provider = OpenCodeFreeProvider()
    assert provider.endpoint == OPENCODE_FREE_ENDPOINT
    assert "nemotron-3-ultra-free" in provider.supported_models


# ==========================================
# OpenCode Go Tests
# ==========================================

def test_opencode_go_headers():
    headers = build_opencode_go_headers("sk-test-12345")
    assert headers["Authorization"] == "Bearer sk-test-12345"
    assert headers["User-Agent"] == "opencode"
    assert headers["x-opencode-client"] == "desktop"
    assert headers["x-opencode-session"].startswith("ses_")
    assert headers["x-opencode-request"].startswith("msg_")


def test_opencode_go_endpoint():
    provider = OpenCodeGoProvider({"api_key": "sk-test-12345"})
    assert provider.endpoint == OPENCODE_GO_ENDPOINT
    assert "deepseek-flash" in provider.supported_models


# ==========================================
# Cline Tests
# ==========================================

def test_cline_endpoint_resolution():
    # Cline API is always the primary endpoint; OpenRouter is only a fallback.
    p_key = ClineProvider({"api_key": "sk-or-v1-test"})
    assert p_key.endpoint == f"{CLINE_API_BASE}/chat/completions"
    assert p_key.is_cline_api

    p_cline = ClineProvider({"token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.test"})
    assert p_cline.endpoint == f"{CLINE_API_BASE}/chat/completions"

    # An explicit endpoint override leaves the Cline API path entirely.
    p_custom = ClineProvider({"endpoint": OPENROUTER_API_BASE, "api_key": "sk-or-v1-test"})
    assert p_custom.endpoint == OPENROUTER_API_BASE
    assert not p_custom.is_cline_api


# ==========================================
# Antigravity Tests
# ==========================================

def test_antigravity_token_refresh_config():
    provider = AntigravityProvider({
        "refresh_token": "1//test_refresh_token",
        "project_id": "test-project",
    })
    assert provider.refresh_token == "1//test_refresh_token"
    assert provider.project_id == "test-project"


@pytest.mark.asyncio
async def test_antigravity_refresh_mock():
    provider = AntigravityProvider({
        "refresh_token": "1//test_refresh_token",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
    })

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "ya29.test_new_access_token",
        "expires_in": 3600,
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("kiterouter.providers.antigravity.persist_provider_tokens") as mock_persist:
        mock_post.return_value = mock_resp
        token = await provider.refresh_access_token()
        assert token == "ya29.test_new_access_token"
        assert provider.auth_token == "ya29.test_new_access_token"
        assert mock_persist.called
