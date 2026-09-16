"""OpenCode Go: Authenticated subscription provider."""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk

logger = logging.getLogger(__name__)

OPENCODE_BASE = "https://opencode.ai"
OPENCODE_GO_ENDPOINT = f"{OPENCODE_BASE}/zen/go/v1/chat/completions"
OPENCODE_GO_MODELS_ENDPOINT = f"{OPENCODE_BASE}/zen/go/v1/models"


def build_opencode_go_headers(api_key: str, session_id: Optional[str] = None) -> Dict[str, str]:
    """Build required headers for OpenCode Go gateway routing."""
    sid = session_id or f"ses_{uuid.uuid4().hex[:32]}"
    req_id = f"msg_{uuid.uuid4().hex[:32]}"
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "opencode",
        "x-opencode-client": "desktop",
        "x-opencode-session": sid,
        "x-opencode-request": req_id,
        "Accept": "text/event-stream",
    }


class OpenCodeGoProvider(BaseProvider):
    name = "opencode_go"
    is_free = False
    supported_models = [
        "deepseek-flash",
        "deepseek-v4-pro",
        "glm-5.3-flash",
        "glm-5.3",
        "glm-5.2",
        "minimax-m3",
        "minimax-m2.7",
        "minimax-m2.5",
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        "qwen3.7-max",
        "qwen3.8-max",
        "qwen3.6-plus",
        "mimo-v2.5-pro",
        "mimo-v2.5",
        "hy3-preview",
        "grok-4.5",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("OPENCODE_GO_KEY")
            or os.environ.get("OPENCODE_API_KEY")
        )
        self.endpoint = self.config.get("endpoint", OPENCODE_GO_ENDPOINT)
        self.mock_mode = self.config.get("mock", False)

    async def is_available(self) -> bool:
        return bool(self.api_key or self.mock_mode)

    async def fetch_models(self) -> List[str]:
        """Fetch live models from OpenCode Go catalog."""
        if not self.api_key:
            return self.get_models()
        try:
            headers = build_opencode_go_headers(self.api_key)
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(OPENCODE_GO_MODELS_ENDPOINT, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    models = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    if models:
                        self.supported_models = models
                        return self.supported_models
        except Exception as e:
            logger.debug("OpenCode Go fetch_models error: %s", e)
        return self.get_models()

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(
                f"[OpenCode Go Mock Response from {model}]", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.api_key:
            yield create_sse_chunk(
                "OpenCode Go error: API key missing. Set OPENCODE_API_KEY or configure opencode_go.",
                model=model,
            )
            yield "data: [DONE]\n\n"
            return

        headers = build_opencode_go_headers(self.api_key, kwargs.get("session_id"))
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
                async with client.stream(
                    "POST", self.endpoint, headers=headers, json=payload
                ) as resp:
                    if resp.status_code != 200:
                        err_text = ""
                        try:
                            err_bytes = await resp.aread()
                            err_json = json.loads(err_bytes.decode())
                            err_text = (
                                err_json.get("error", {}).get("message")
                                or err_json.get("message")
                                or ""
                            )
                        except Exception:
                            pass
                        msg = f"OpenCode Go Error: HTTP {resp.status_code}"
                        if err_text:
                            msg += f" - {err_text}"
                        yield create_sse_chunk(msg, model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[OpenCode Go error: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
