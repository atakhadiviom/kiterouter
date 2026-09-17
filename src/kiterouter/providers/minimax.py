"""MiniMax Provider ($0.20/1M tokens, 1M context)."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines


class MiniMaxProvider(BaseProvider):
    name = "minimax"
    is_free = False
    supported_models = [
        "minimax/MiniMax-M2.7",
        "minimax/MiniMax-M2.5",
        "minimax/abab6.5s-chat",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("MINIMAX_API_KEY")
        )
        self.endpoint = self.config.get(
            "endpoint", "https://api.minimax.io/v1/text/chatcompletion_v2"
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
        clean_model = model.replace("minimax/", "")
        if self.mock_mode:
            yield create_sse_chunk(f"[MiniMax Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.api_key:
            yield create_sse_chunk("MiniMax error: API key missing. Set MINIMAX_API_KEY.", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": clean_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", self.endpoint, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        yield create_sse_chunk(f"MiniMax upstream HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in iter_upstream_lines(resp):
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[MiniMax notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
