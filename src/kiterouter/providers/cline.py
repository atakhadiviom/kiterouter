"""Cline provider: Cline API (OAuth) with OpenRouter fallback.

Auth model
----------
Cline issues short-lived WorkOS access tokens (1h) alongside a long-lived
refresh token. The refresh token does not rotate, so it can safely be shared
with the locally installed Cline CLI — but a *stale copy* imported from
another router's database is indistinguishable from a revoked account at
call time, so credentials are always resolved against the freshest local
session and refreshed proactively before expiry.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from kiterouter.config import persist_provider_tokens
from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.token_fetcher import TokenFetcher, normalize_expires_at

logger = logging.getLogger(__name__)

CLINE_API_BASE = "https://api.cline.bot/api/v1"
CLINE_CHAT_URL = f"{CLINE_API_BASE}/chat/completions"
CLINE_REFRESH_URL = f"{CLINE_API_BASE}/auth/refresh"
CLINE_RECOMMENDED_MODELS_URL = f"{CLINE_API_BASE}/ai/cline/recommended-models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

WORKOS_DEVICE_URL = "https://api.workos.com/user_management/authorize/device"
WORKOS_AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"
CLINE_REGISTER_URL = f"{CLINE_API_BASE}/auth/register"
WORKOS_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"

TOKEN_REFRESH_BUFFER_SECONDS = 300
REAUTH_MESSAGE = (
    "Cline re-authentication required: the Cline account linked to this token "
    "is no longer authorized. Re-authenticate from the dashboard."
)

# Model ids on OpenRouter are vendor-prefixed. Cline's own catalog already uses
# that form, but its free tier does not (e.g. "cline-free/deepseek-v4.1-flash"),
# so the vendor is used to complete the id. Lookups stay exact — substituting a
# neighbouring model would silently change the request (and its cost).
FAMILY_VENDORS = {
    "claude": "anthropic",
    "gpt": "openai",
    "gemini": "google",
    "deepseek": "deepseek",
    "kimi": "moonshotai",
    "grok": "x-ai",
}
ROUTER_MODEL_PREFIXES = ("cline-free/", "cline/")


def unwrap_envelope(payload: Any) -> Any:
    """Unwrap the ``{"success": true, "data": {...}}`` Cline API envelope."""
    if isinstance(payload, dict) and "data" in payload and "success" in payload:
        return payload.get("data")
    return payload


def is_account_unlinked(user_info: Any) -> bool:
    """True when a valid token belongs to a user with no linked Cline account.

    Only an explicit ``accounts: null`` counts — a response that omits the
    field entirely says nothing about the linkage.
    """
    if not isinstance(user_info, dict):
        return False
    return "accounts" in user_info and user_info["accounts"] is None


def resolve_openrouter_model(
    model: str, catalog: List[str], override: Optional[str] = None
) -> Optional[str]:
    """Map a Cline model id onto a real OpenRouter model id.

    Only exact catalog matches are accepted. Returns None when no defensible
    equivalent exists, so the caller can say so rather than silently routing
    to a different (possibly far more expensive) model.
    """
    if override:
        return override
    if not catalog:
        return None

    stripped = model
    for prefix in ROUTER_MODEL_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break

    for candidate in (model, stripped):
        if candidate in catalog:
            return candidate

    leaf = stripped.split("/")[-1]
    for family, vendor in FAMILY_VENDORS.items():
        if family in leaf.lower():
            candidate = f"{vendor}/{leaf}"
            return candidate if candidate in catalog else None
    return None


class ClineProvider(BaseProvider):
    name = "cline"
    is_free = False
    supported_models = [
        "cline-free/deepseek-v4.1-flash",
        "cline-free/muse-spark-1.3-contributor",
        "cline-free/solar-pro4",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)

        raw_key = (
            self.config.get("api_key")
            or os.environ.get("CLINE_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        if isinstance(raw_key, list):
            raw_key = raw_key[0] if raw_key and raw_key[0] != "dummy" else ""

        self.api_key: Optional[str] = raw_key if raw_key != "dummy" else None
        self.access_token: Optional[str] = self.config.get("access_token") or self.config.get("token") or os.environ.get("CLINE_ACCESS_TOKEN")
        self.refresh_token: Optional[str] = self.config.get("refresh_token") or os.environ.get("CLINE_REFRESH_TOKEN")
        self.expires_at: int = normalize_expires_at(
            self.config.get("expires_at") or self.config.get("expiresAt")
        )
        self.email: str = self.config.get("email") or ""

        self.reauth_required: bool = bool(self.config.get("reauth_required"))
        self.last_auth_error: Optional[str] = self.config.get("last_auth_error") or None
        self.last_fallback_reason: Optional[str] = None
        self.last_fallback_model: Optional[str] = None

        self._openrouter_key: Optional[str] = None
        self._openrouter_key_resolved: bool = False
        self._openrouter_catalog_cache: Optional[List[str]] = None

        custom_endpoint = self.config.get("endpoint")
        self.endpoint = custom_endpoint or CLINE_CHAT_URL
        self.is_cline_api = "cline.bot" in self.endpoint

        self.mock_mode = self.config.get("mock", False)

        # Adopt the local Cline session when nothing usable is configured, so a
        # fresh install works without a manual credential paste.
        if self.is_cline_api and not (self.access_token or self.refresh_token):
            harvested = TokenFetcher.fetch_cline_credentials()
            if harvested:
                self._apply_credentials(harvested, persist=False)

    # ------------------------------------------------------------------ auth

    def _apply_credentials(self, creds: Dict[str, Any], persist: bool = True) -> None:
        access = creds.get("access_token") or creds.get("token")
        refresh = creds.get("refresh_token")
        expires = normalize_expires_at(creds.get("expires_at") or creds.get("expiresAt"))

        if access:
            self.access_token = access
        if refresh:
            self.refresh_token = refresh
        if expires:
            self.expires_at = expires
        if creds.get("email"):
            self.email = creds["email"]

        if persist:
            persist_provider_tokens(
                "cline",
                {
                    "access_token": self.access_token or "",
                    "token": self.access_token or "",
                    "refresh_token": self.refresh_token or "",
                    "expires_at": self.expires_at,
                    "email": self.email,
                    "source": creds.get("source", self.config.get("source", "")),
                },
            )

    def _set_auth_state(self, reauth_required: bool, message: Optional[str]) -> None:
        """Record auth health, persisting only when it actually changes.

        Surfacing an unlinked account matters across restarts: without this the
        dashboard would claim valid credentials until the next failed call.
        """
        changed = (self.reauth_required != reauth_required) or (self.last_auth_error != message)
        self.reauth_required = reauth_required
        self.last_auth_error = message
        if changed:
            persist_provider_tokens(
                "cline",
                {"reauth_required": reauth_required, "last_auth_error": message or ""},
            )

    def _token_is_fresh(self) -> bool:
        if not self.access_token:
            return False
        if not self.expires_at:
            return True
        return self.expires_at - time.time() > TOKEN_REFRESH_BUFFER_SECONDS

    def _resolve_openrouter_key(self) -> str:
        key = self.config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY")
        if key:
            return key
        try:
            from kiterouter.config import KiteConfig

            openrouter = KiteConfig.load().providers.get("openrouter") or {}
            return openrouter.get("api_key") or ""
        except Exception:
            return ""

    def openrouter_key(self) -> str:
        if not self._openrouter_key_resolved:
            self._openrouter_key_resolved = True
            self._openrouter_key = self._resolve_openrouter_key()
        return self._openrouter_key or ""

    def has_openrouter_fallback(self) -> bool:
        return bool(self.openrouter_key())

    async def _post_refresh(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """Exchange a refresh token for a fresh access token."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(
                    CLINE_REFRESH_URL,
                    json={"refreshToken": refresh_token, "grantType": "refresh_token"},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
        except Exception as e:
            self.last_auth_error = f"Cline token refresh failed: {e}"
            logger.warning("Cline token refresh error: %s", e)
            return None

        if res.status_code != 200:
            try:
                detail = unwrap_envelope(res.json()) or {}
                message = (detail or {}).get("error") or (detail or {}).get("message")
            except Exception:
                message = None
            self.last_auth_error = (
                f"Cline token refresh rejected (HTTP {res.status_code})"
                + (f": {message}" if message else "")
            )
            logger.warning("Cline refresh HTTP %s", res.status_code)
            return None

        try:
            data = unwrap_envelope(res.json())
        except Exception as e:
            self.last_auth_error = f"Cline token refresh returned invalid JSON: {e}"
            return None

        if not isinstance(data, dict) or not data.get("accessToken"):
            self.last_auth_error = "Cline token refresh response missing accessToken"
            return None

        user_info = data.get("userInfo") or {}
        if is_account_unlinked(user_info):
            self._set_auth_state(True, REAUTH_MESSAGE)
        else:
            self._set_auth_state(False, None)

        return {
            "access_token": data["accessToken"],
            "refresh_token": data.get("refreshToken") or refresh_token,
            "expires_at": normalize_expires_at(data.get("expiresAt")),
            "email": user_info.get("email") or self.email,
            "source": "cline-refresh",
        }

    async def refresh_access_token(self, allow_harvest: bool = True) -> Optional[str]:
        """Refresh the Cline access token, adopting a fresher local session once.

        A stale refresh token copied from another router fails with HTTP 400.
        When that happens the live Cline CLI session is harvested and retried,
        which recovers without any manual intervention on this machine.
        """
        for attempt in (0, 1):
            if not self.refresh_token:
                break
            credentials = await self._post_refresh(self.refresh_token)
            if credentials:
                self._apply_credentials(credentials, persist=True)
                logger.info("Cline token refreshed (expires_at=%s)", self.expires_at)
                return self.access_token

            if attempt == 0 and allow_harvest:
                harvested = TokenFetcher.fetch_cline_credentials()
                fresh = harvested.get("refresh_token")
                if fresh and fresh != self.refresh_token:
                    logger.info("Cline refresh token was stale; adopting local session")
                    self._apply_credentials(harvested, persist=False)
                    continue
            break

        if not self.refresh_token:
            self.last_auth_error = "Cline: no refresh token available"
        return None

    async def is_available(self) -> bool:
        if self.mock_mode:
            return True
        if self.is_cline_api:
            if self.access_token or self.refresh_token or self.api_key:
                return True
            return self.has_openrouter_fallback()
        return bool(self.api_key or self.access_token)

    async def auth_status(self) -> Dict[str, Any]:
        """Honest, non-secret summary of the current Cline credential state."""
        now = time.time()
        return {
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "api_key_configured": bool(self.api_key),
            "expires_at": self.expires_at or None,
            "expires_in_seconds": int(self.expires_at - now) if self.expires_at else None,
            "expired": bool(self.expires_at and self.expires_at <= now),
            "email": self.email,
            "source": self.config.get("source", ""),
            "reauth_required": self.reauth_required,
            "last_auth_error": self.last_auth_error,
            "openrouter_fallback": self.has_openrouter_fallback(),
            "last_fallback_reason": self.last_fallback_reason,
            "last_fallback_model": self.last_fallback_model,
        }

    # --------------------------------------------------------------- requests

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Cline/SDK",
            "HTTP-Referer": "https://cline.bot",
            "X-Title": "Cline",
        }

    def _payload(
        self, model: str, messages: List[Dict[str, Any]], temperature: float, max_tokens: Optional[int]
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return payload

    async def _openrouter_catalog(self) -> List[str]:
        """Fetch and cache the live OpenRouter model catalog."""
        if self._openrouter_catalog_cache is not None:
            return self._openrouter_catalog_cache
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    OPENROUTER_MODELS_URL,
                    headers={"Authorization": f"Bearer {self.openrouter_key()}"},
                )
                if resp.status_code == 200:
                    data = resp.json().get("data") or []
                    self._openrouter_catalog_cache = [
                        m["id"] for m in data if isinstance(m, dict) and m.get("id")
                    ]
        except Exception as e:
            logger.debug("OpenRouter catalog fetch failed: %s", e)
        if self._openrouter_catalog_cache is None:
            self._openrouter_catalog_cache = []
        return self._openrouter_catalog_cache

    async def _stream_openrouter(
        self, model: str, messages: List[Dict[str, Any]], temperature: float, max_tokens: Optional[int]
    ) -> AsyncGenerator[str, None]:
        """Serve the request through OpenRouter when Cline auth is unavailable."""
        key = self.openrouter_key()
        catalog = await self._openrouter_catalog()
        target_model = resolve_openrouter_model(model, catalog, self.config.get("openrouter_model"))
        if not target_model:
            yield create_sse_chunk(
                f"Cline auth failed and no OpenRouter equivalent is available for "
                f"'{model}'. Set cline.openrouter_model to choose one, or "
                f"re-authenticate Cline.",
                model=model,
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        self.last_fallback_model = target_model
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "HTTP-Referer": "https://cline.bot",
            "X-Title": "KiteRouter",
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    OPENROUTER_CHAT_URL,
                    headers=headers,
                    json=self._payload(target_model, messages, temperature, max_tokens),
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        yield create_sse_chunk(
                            f"Cline auth failed and OpenRouter fallback returned HTTP "
                            f"{resp.status_code}: {body[:200]}",
                            model=model,
                        )
                        yield "data: [DONE]\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line.strip():
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"Cline OpenRouter fallback failed: {e}", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(f"[Cline Adapter Mock Response for {model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.is_cline_api:
            async for chunk in self._stream_generic(model, messages, temperature, max_tokens):
                yield chunk
            return

        # Refresh proactively so an expired token never costs a request.
        if not self._token_is_fresh() and self.refresh_token:
            await self.refresh_access_token()

        token_to_use = self.access_token or self.api_key
        if not token_to_use:
            async for chunk in self._fallback_or_error(
                model, messages, temperature, max_tokens,
                self.last_auth_error or "Cline: no credential available",
            ):
                yield chunk
            return

        payload = self._payload(model, messages, temperature, max_tokens)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    self.endpoint, headers=self._headers(token_to_use), json=payload
                )

                if resp.status_code == 401:
                    refreshed = await self.refresh_access_token()
                    if refreshed:
                        resp = await client.post(
                            self.endpoint, headers=self._headers(refreshed), json=payload
                        )

                if resp.status_code == 401:
                    self._set_auth_state(True, REAUTH_MESSAGE)
                    async for chunk in self._fallback_or_error(
                        model, messages, temperature, max_tokens, REAUTH_MESSAGE
                    ):
                        yield chunk
                    return

                if resp.status_code != 200:
                    yield create_sse_chunk(
                        f"Cline upstream HTTP {resp.status_code}: {resp.text[:200]}", model=model
                    )
                    yield "data: [DONE]\n\n"
                    return

                self._set_auth_state(False, None)
                for line in resp.text.splitlines():
                    if line.strip():
                        yield f"{line}\n\n"

        except Exception as e:
            yield create_sse_chunk(f"[Cline notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"

    async def _fallback_or_error(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        reason: str,
    ) -> AsyncGenerator[str, None]:
        if self.has_openrouter_fallback():
            self.last_fallback_reason = reason
            logger.info("Cline auth unavailable, routing via OpenRouter: %s", reason)
            async for chunk in self._stream_openrouter(model, messages, temperature, max_tokens):
                yield chunk
            return

        yield create_sse_chunk(f"Cline error: {reason}", model=model)
        yield create_sse_chunk(finish_reason="stop", model=model)
        yield "data: [DONE]\n\n"

    async def _stream_generic(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
    ) -> AsyncGenerator[str, None]:
        """Passthrough for an explicit non-Cline endpoint override."""
        token = self.api_key or self.access_token
        if not token:
            yield create_sse_chunk(
                "Cline error: no API key configured for the custom endpoint.", model=model
            )
            yield "data: [DONE]\n\n"
            return
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    self.endpoint,
                    headers=self._headers(token),
                    json=self._payload(model, messages, temperature, max_tokens),
                )
                if resp.status_code != 200:
                    yield create_sse_chunk(
                        f"Cline upstream HTTP {resp.status_code}: {resp.text[:200]}", model=model
                    )
                    yield "data: [DONE]\n\n"
                    return
                for line in resp.text.splitlines():
                    if line.strip():
                        yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Cline notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"

    async def fetch_models(self) -> List[str]:
        """Fetch the live Cline catalog, falling back to the static list."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(CLINE_RECOMMENDED_MODELS_URL)
                if resp.status_code == 200:
                    data = unwrap_envelope(resp.json()) or {}
                    models = data.get("recommended") if isinstance(data, dict) else data
                    ids = [
                        m.get("id")
                        for m in (models or [])
                        if isinstance(m, dict) and m.get("id")
                    ]
                    if ids:
                        known = list(dict.fromkeys(ids + self.supported_models))
                        self.supported_models = known
                        return known
        except Exception as e:
            logger.debug("Cline model catalog fetch failed: %s", e)
        return self.supported_models.copy()
