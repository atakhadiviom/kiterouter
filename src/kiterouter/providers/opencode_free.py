"""OpenCode Free: Zero-auth public provider."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk

logger = logging.getLogger(__name__)

OPENCODE_BASE = "https://opencode.ai"
OPENCODE_FREE_ENDPOINT = f"{OPENCODE_BASE}/zen/v1/chat/completions"
OPENCODE_MODELS_ENDPOINT = f"{OPENCODE_BASE}/zen/v1/models"


def build_opencode_headers(session_id: Optional[str] = None) -> Dict[str, str]:
    """Required headers for OpenCode gateway routing."""
    sid = session_id or f"ses_{uuid.uuid4().hex[:32]}"
    req_id = f"msg_{uuid.uuid4().hex[:32]}"
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer public",
        "User-Agent": "opencode",
        "x-opencode-client": "desktop",
        "x-opencode-session": sid,
        "x-opencode-request": req_id,
        "Accept": "text/event-stream",
    }


class OpenCodeFreeProvider(BaseProvider):
    name = "opencode_free"
    is_free = True
    supported_models = [
        "nemotron-3-ultra-free",
        "mimo-v2.5-free",
        "ling-3.0-flash-fin-free",
        "deepseek-v4-flash-free",
        "muse-spark-1.3-contributor-free",
        "muse-spark-1.2-contributor-free",
        "nemotron-3.5-lightning-free",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.endpoint = self.config.get("endpoint", OPENCODE_FREE_ENDPOINT)
        self.mock_mode = self.config.get("mock", False)

    async def fetch_models(self) -> List[str]:
        """Fetch real-time live models from OpenCode catalog."""
        try:
            headers = build_opencode_headers()
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(OPENCODE_MODELS_ENDPOINT, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    # Filter free models from catalog
                    all_models = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    free_models = [m for m in all_models if "-free" in m]
                    if free_models:
                        self.supported_models = free_models
                        return self.supported_models
        except Exception as e:
            logger.debug("OpenCode Free model fetch failed: %s", e)
        return self.get_models()

    async def is_available(self) -> bool:
        return True

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
                f"[OpenCode Free Mock Response from {model}]", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = build_opencode_headers(kwargs.get("session_id"))
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
                        msg = f"OpenCode Free Error: HTTP {resp.status_code}"
                        if err_text:
                            msg += f" - {err_text}"
                        yield create_sse_chunk(msg, model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(
                f"[OpenCode Free connection notice: {e}]", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
