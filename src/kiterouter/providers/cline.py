"""Cline provider: Anthropic, OpenRouter, and official Cline API adapter."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx

from kiterouter.config import persist_provider_tokens
from kiterouter.providers.base import BaseProvider, create_sse_chunk

logger = logging.getLogger(__name__)

CLINE_API_BASE = "https://api.cline.bot/api/v1"
CLINE_REFRESH_URL = f"{CLINE_API_BASE}/auth/refresh"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1/chat/completions"


class ClineProvider(BaseProvider):
    name = "cline"
    is_free = False
    supported_models = [
        "claude-3-5-sonnet",
        "claude-3-7-sonnet",
        "claude-3-opus",
        "gpt-4o",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        # Check for direct API key, extension JWT token, or environment variables
        raw_key = (
            self.config.get("api_key")
            or os.environ.get("CLINE_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        if isinstance(raw_key, list):
            raw_key = raw_key[0] if raw_key and raw_key[0] != "dummy" else ""

        self.api_key: Optional[str] = raw_key if raw_key != "dummy" else None
        self.access_token: Optional[str] = (
            self.config.get("access_token")
            or self.config.get("token")
            or os.environ.get("CLINE_ACCESS_TOKEN")
        )
        self.refresh_token: Optional[str] = (
            self.config.get("refresh_token")
            or os.environ.get("CLINE_REFRESH_TOKEN")
        )

        # Custom endpoint override, or determine based on credential type
        custom_endpoint = self.config.get("endpoint")
        if custom_endpoint:
            self.endpoint = custom_endpoint
        elif self.access_token or (self.api_key and self.api_key.startswith("ey")):
            self.endpoint = f"{CLINE_API_BASE}/chat/completions"
        else:
            self.endpoint = OPENROUTER_API_BASE

        self.mock_mode = self.config.get("mock", False)

    async def refresh_access_token(self) -> Optional[str]:
        """Attempt to refresh Cline extension token."""
        if not self.refresh_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    CLINE_REFRESH_URL,
                    json={
                        "refreshToken": self.refresh_token,
                        "grantType": "refresh_token",
                        "clientType": "extension",
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                if res.status_code == 200:
                    data = res.json()
                    new_token = data.get("accessToken") or (
                        data.get("data", {}).get("accessToken")
                        if isinstance(data.get("data"), dict)
                        else None
                    )
                    if new_token:
                        self.access_token = new_token
                        persist_provider_tokens(
                            "cline",
                            {
                                "access_token": new_token,
                                "token": new_token,
                                "refresh_token": self.refresh_token,
                            },
                        )
                        logger.info("Cline token successfully refreshed")
                        return new_token
                else:
                    logger.warning(
                        "Cline refresh returned HTTP %s: %s",
                        res.status_code,
                        res.text[:150],
                    )
        except Exception as e:
            logger.warning("Cline token refresh error: %s", e)
        return None

    async def is_available(self) -> bool:
        return bool(self.api_key or self.access_token or self.refresh_token or self.mock_mode)

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

        token_to_use = self.access_token or self.api_key
        if not token_to_use and self.refresh_token:
            token_to_use = await self.refresh_access_token()

        if not token_to_use:
            yield create_sse_chunk(
                "Cline error: No API key or Cline token found. Please set CLINE_API_KEY or re-authenticate Cline.",
                model=model,
            )
            yield "data: [DONE]\n\n"
            return

        is_cline_api = "cline.bot" in self.endpoint or token_to_use.startswith("ey")
        headers = {
            "Authorization": f"Bearer {token_to_use}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Cline",
            "HTTP-Referer": "https://cline.bot",
            "X-Title": "Cline",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(self.endpoint, headers=headers, json=payload)

                # If unauthorized and refresh token available, try refresh once
                if resp.status_code == 401 and self.refresh_token and is_cline_api:
                    refreshed = await self.refresh_access_token()
                    if refreshed:
                        headers["Authorization"] = f"Bearer {refreshed}"
                        resp = await client.post(self.endpoint, headers=headers, json=payload)

                if resp.status_code == 401:
                    err_msg = "Unauthorized: Cline session expired or revoked. Please re-authenticate your Cline account."
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("error") or err_json.get("message") or err_msg
                    except Exception:
                        pass
                    yield create_sse_chunk(f"Cline upstream HTTP 401: {err_msg}", model=model)
                    yield "data: [DONE]\n\n"
                    return

                if resp.status_code != 200:
                    yield create_sse_chunk(f"Cline upstream HTTP {resp.status_code}", model=model)
                    yield "data: [DONE]\n\n"
                    return

                for line in resp.text.splitlines():
                    line = line.strip()
                    if line:
                        yield f"{line}\n\n"

        except Exception as e:
            yield create_sse_chunk(f"[Cline notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
