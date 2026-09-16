"""OpenCode Go: Authenticated subscription provider."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk


class OpenCodeGoProvider(BaseProvider):
    name = "opencode_go"
    is_free = False
    supported_models = [
        "claude-3-5-sonnet",
        "claude-3-7-sonnet",
        "gpt-4o",
        "o1",
        "o3-mini",
        "deepseek-r1",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("OPENCODE_GO_KEY")
            or os.environ.get("OPENCODE_API_KEY")
        )
        self.endpoint = self.config.get(
            "endpoint", "https://api.opencode.ai/v1/chat/completions"
        )
        self.mock_mode = self.config.get("mock", False)

    async def is_available(self) -> bool:
        return bool(self.api_key or self.mock_mode)

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
                "OpenCode Go error: API key missing. Set OPENCODE_API_KEY.",
                model=model,
            )
            yield "data: [DONE]\n\n"
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "KiteRouter/0.1.0",
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
                async with client.stream(
                    "POST", self.endpoint, headers=headers, json=payload
                ) as resp:
                    if resp.status_code != 200:
                        yield create_sse_chunk(
                            f"OpenCode Go Error: HTTP {resp.status_code}",
                            model=model,
                        )
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[OpenCode Go error: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
