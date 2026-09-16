"""Tests for Cline credential discovery, refresh, and OpenRouter fallback."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from kiterouter import cline_auth
from kiterouter.providers.cline import (
    ClineProvider,
    is_account_unlinked,
    resolve_openrouter_model,
    unwrap_envelope,
)
from kiterouter.token_fetcher import TokenFetcher, normalize_expires_at


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- expiry parsing


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-16T18:11:42Z", 1789582302),
        ("2026-07-24T14:26:00.488Z", 1784903160),
        (1789542553000, 1789542553),
        (1789542553, 1789542553),
        ("1789542553", 1789542553),
        ("", 0),
        (None, 0),
        ("not-a-date", 0),
        (True, 0),
        (-5, 0),
    ],
)
def test_normalize_expires_at_handles_every_source_encoding(value, expected):
    assert normalize_expires_at(value) == expected


# ------------------------------------------------------------------- envelopes


def test_unwrap_envelope_handles_success_data_and_bare_payloads():
    assert unwrap_envelope({"success": True, "data": {"accessToken": "t"}}) == {"accessToken": "t"}
    assert unwrap_envelope({"accessToken": "t"}) == {"accessToken": "t"}
    assert unwrap_envelope(None) is None


def test_is_account_unlinked_detects_missing_workspace():
    assert is_account_unlinked({"accounts": None}) is True
    assert is_account_unlinked({"accounts": ["acc_1"]}) is False
    assert is_account_unlinked({}) is False
    assert is_account_unlinked(None) is False


# -------------------------------------------------------------- model mapping


CATALOG = [
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-5",
    "deepseek/deepseek-v4.1-flash",
    "moonshotai/kimi-k3",
    "openai/gpt-5",
]


def test_resolve_openrouter_model_prefers_exact_matches():
    assert resolve_openrouter_model("anthropic/claude-opus-5", CATALOG) == "anthropic/claude-opus-5"


def test_resolve_openrouter_model_completes_vendor_for_free_tier_ids():
    assert (
        resolve_openrouter_model("cline-free/deepseek-v4.1-flash", CATALOG)
        == "deepseek/deepseek-v4.1-flash"
    )


def test_resolve_openrouter_model_refuses_to_substitute_unknown_models():
    # "claude-3-5-sonnet" has no exact equivalent: picking a neighbouring model
    # would silently change the request (and its cost).
    assert resolve_openrouter_model("claude-3-5-sonnet", CATALOG) is None
    assert resolve_openrouter_model("some-unknown-model", CATALOG) is None
    assert resolve_openrouter_model("anything", []) is None


def test_resolve_openrouter_model_honours_explicit_override():
    assert resolve_openrouter_model("anything", CATALOG, override="openai/gpt-5") == "openai/gpt-5"


# ----------------------------------------------------------------- discovery


def test_fetch_cline_credentials_reads_live_cli_session(monkeypatch, tmp_path):
    settings = tmp_path / ".cline" / "data" / "settings"
    settings.mkdir(parents=True)
    (settings / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "cline": {
                        "settings": {
                            "auth": {
                                "accessToken": "live-access",
                                "refreshToken": "live-refresh",
                                "expiresAt": 1789542553000,
                                "metadata": {"userInfo": {"email": "dev@example.com"}},
                            }
                        }
                    }
                }
            }
        )
    )
    monkeypatch.setattr("kiterouter.token_fetcher.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        TokenFetcher, "fetch_from_9router", staticmethod(lambda *a, **k: {})
    )
    monkeypatch.setattr(
        TokenFetcher, "fetch_from_omniroute", staticmethod(lambda *a, **k: {})
    )

    creds = TokenFetcher.fetch_cline_credentials()
    assert creds["access_token"] == "live-access"
    assert creds["refresh_token"] == "live-refresh"
    assert creds["expires_at"] == 1789542553
    assert creds["email"] == "dev@example.com"
    assert creds["source"] == "cline-cli"


def test_fetch_cline_credentials_falls_back_to_imported_stores(monkeypatch, tmp_path):
    monkeypatch.setattr("kiterouter.token_fetcher.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        TokenFetcher,
        "fetch_from_9router",
        staticmethod(
            lambda *a, **k: {
                "cline": {
                    "accessToken": "imported",
                    "refreshToken": "imported-refresh",
                    "expiresAt": "2026-07-24T14:26:00.488Z",
                }
            }
        ),
    )
    monkeypatch.setattr(
        TokenFetcher, "fetch_from_omniroute", staticmethod(lambda *a, **k: {})
    )

    creds = TokenFetcher.fetch_cline_credentials()
    assert creds["access_token"] == "imported"
    assert creds["expires_at"] == 1784903160
    assert creds["source"] == "9router"


# ------------------------------------------------------------------- refresh


def test_refresh_marks_reauth_required_when_account_is_unlinked(monkeypatch):
    provider = ClineProvider({"refresh_token": "r", "access_token": "a"})
    response = httpx.Response(
        200,
        json={
            "success": True,
            "data": {
                "accessToken": "fresh",
                "refreshToken": "r",
                "expiresAt": "2026-09-16T18:11:42Z",
                "userInfo": {"email": "dev@example.com", "accounts": None},
            },
        },
        request=httpx.Request("POST", "https://api.cline.bot/api/v1/auth/refresh"),
    )
    monkeypatch.setattr("kiterouter.providers.cline.persist_provider_tokens", lambda *a, **k: None)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
        token = run(provider.refresh_access_token())

    assert token == "fresh"
    assert provider.reauth_required is True
    assert provider.expires_at == 1789582302


def test_refresh_adopts_local_session_when_stored_token_is_stale(monkeypatch):
    provider = ClineProvider({"refresh_token": "stale", "access_token": "a"})
    monkeypatch.setattr("kiterouter.providers.cline.persist_provider_tokens", lambda *a, **k: None)

    attempts = []

    async def fake_post(refresh_token):
        attempts.append(refresh_token)
        if refresh_token == "stale":
            provider.last_auth_error = "Cline token refresh rejected (HTTP 400)"
            return None
        return {
            "access_token": "recovered",
            "refresh_token": "live",
            "expires_at": 1789582302,
            "email": "dev@example.com",
        }

    monkeypatch.setattr(provider, "_post_refresh", fake_post)
    monkeypatch.setattr(
        TokenFetcher,
        "fetch_cline_credentials",
        staticmethod(lambda: {"refresh_token": "live", "access_token": "recovered"}),
    )

    token = run(provider.refresh_access_token())
    assert token == "recovered"
    assert attempts == ["stale", "live"]


def test_refresh_reports_failure_without_a_local_session(monkeypatch):
    provider = ClineProvider({"refresh_token": "stale"})
    monkeypatch.setattr("kiterouter.providers.cline.persist_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(
        provider, "_post_refresh", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(TokenFetcher, "fetch_cline_credentials", staticmethod(lambda: {}))

    assert run(provider.refresh_access_token()) is None
    assert provider.reauth_required is False


def test_token_freshness_uses_expiry_with_buffer():
    fresh = ClineProvider({"access_token": "a", "expires_at": 9999999999})
    stale = ClineProvider({"access_token": "a", "expires_at": 1})
    unknown = ClineProvider({"access_token": "a"})
    assert fresh._token_is_fresh() is True
    assert stale._token_is_fresh() is False
    assert unknown._token_is_fresh() is True


# ------------------------------------------------------------------ fallback


def test_401_falls_back_to_openrouter(monkeypatch):
    provider = ClineProvider({"access_token": "expired", "api_key": ""})
    monkeypatch.setattr(provider, "refresh_access_token", AsyncMock(return_value=None))
    monkeypatch.setattr(provider, "has_openrouter_fallback", lambda: True)

    async def fake_openrouter(model, messages, temperature, max_tokens):
        yield "data: {\"choices\":[{\"delta\":{\"content\":\"from-openrouter\"}}]}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(provider, "_stream_openrouter", fake_openrouter)

    unauthorized = httpx.Response(
        401,
        json={"error": "Unauthorized"},
        request=httpx.Request("POST", "https://api.cline.bot/api/v1/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=unauthorized):
        chunks = run(_collect(provider, "cline-free/deepseek-v4.1-flash"))

    assert any("from-openrouter" in c for c in chunks)
    assert provider.reauth_required is True
    assert provider.last_fallback_reason


def test_401_without_openrouter_fallback_reports_error(monkeypatch):
    provider = ClineProvider({"access_token": "expired"})
    monkeypatch.setattr(provider, "refresh_access_token", AsyncMock(return_value=None))
    monkeypatch.setattr(provider, "has_openrouter_fallback", lambda: False)

    unauthorized = httpx.Response(
        401,
        json={"error": "Unauthorized"},
        request=httpx.Request("POST", "https://api.cline.bot/api/v1/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=unauthorized):
        chunks = run(_collect(provider, "cline-free/deepseek-v4.1-flash"))

    joined = "".join(chunks)
    assert "Cline error" in joined
    assert "data: [DONE]" in joined


def test_successful_response_is_passed_through_and_clears_auth_error(monkeypatch):
    provider = ClineProvider({"access_token": "good"})
    provider.reauth_required = True
    provider.last_auth_error = "stale"

    ok = httpx.Response(
        200,
        text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
        request=httpx.Request("POST", "https://api.cline.bot/api/v1/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=ok):
        chunks = run(_collect(provider, "cline-free/deepseek-v4.1-flash"))

    assert any("hi" in c for c in chunks)
    assert provider.reauth_required is False
    assert provider.last_auth_error is None


async def _collect(provider: ClineProvider, model: str) -> list:
    out = []
    async for chunk in provider.stream_chat(
        model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=16
    ):
        out.append(chunk)
    return out


# ---------------------------------------------------------------- device flow


def test_device_flow_returns_user_code():
    response = httpx.Response(
        200,
        json={
            "device_code": "dev-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://authkit.cline.bot/device",
            "expires_in": 300,
            "interval": 5,
        },
        request=httpx.Request("POST", cline_auth.WORKOS_DEVICE_URL),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
        flow = run(cline_auth.start_device_flow())
    assert flow["user_code"] == "ABCD-EFGH"
    assert flow["device_code"] == "dev-code"


def test_poll_raises_pending_while_awaiting_approval():
    response = httpx.Response(
        400,
        json={"error": "authorization_pending"},
        request=httpx.Request("POST", cline_auth.WORKOS_AUTHENTICATE_URL),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
        with pytest.raises(cline_auth.AuthorizationPending):
            run(cline_auth.poll_device_flow("dev-code"))


def test_poll_surfaces_denial_as_terminal_error():
    response = httpx.Response(
        400,
        json={"error": "access_denied"},
        request=httpx.Request("POST", cline_auth.WORKOS_AUTHENTICATE_URL),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
        with pytest.raises(cline_auth.DeviceFlowError):
            run(cline_auth.poll_device_flow("dev-code"))


def test_register_cline_session_unwraps_envelope():
    response = httpx.Response(
        200,
        json={
            "success": True,
            "data": {
                "accessToken": "session-token",
                "refreshToken": "session-refresh",
                "expiresAt": "2026-09-16T18:11:42Z",
                "userInfo": {"email": "dev@example.com", "accounts": ["acc"]},
            },
        },
        request=httpx.Request("POST", cline_auth.CLINE_REGISTER_URL),
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
        session = run(cline_auth.register_cline_session("a", "r"))
    assert session["access_token"] == "session-token"
    assert session["email"] == "dev@example.com"
    assert session["accounts"] == ["acc"]
