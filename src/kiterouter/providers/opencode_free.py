"""OpenCode Free: Zero-auth public provider."""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk


class OpenCodeFreeProvider(BaseProvider):
    name = "opencode_free"
    is_free = True
    supported_models = [
        "claude-3-5-sonnet",
        "claude-3-opus",
        "gpt-4o",
        "gpt-4o-mini",
        "deepseek-coder",
        "deepseek-r1",
        "gemini-1.5-pro",
        "gemini-2.0-flash",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.endpoint = self.config.get(
            "endpoint", "https://api.opencode.ai/v1/chat/completions"
        )
        self.mock_mode = self.config.get("mock", False)

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

        headers = {
            "Content-Type": "application/json",
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
                            f"OpenCode Free Error: HTTP {resp.status_code}",
                            model=model,
                        )
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            # Fallback message
            yield create_sse_chunk(
                f"[OpenCode Free connection notice: {e}]", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
