"""Kiro AI provider (Claude 4.5, GLM-5, MiniMax free tier)."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk


class KiroProvider(BaseProvider):
    name = "kiro"
    is_free = True
    supported_models = [
        "kr/claude-sonnet-4.5",
        "kr/claude-haiku-4.5",
        "kr/glm-5",
        "kr/MiniMax-M2.5",
        "kr/qwen3-coder-next",
        "kr/deepseek-3.2",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.auth_token = (
            self.config.get("token")
            or os.environ.get("KIRO_TOKEN")
            or os.environ.get("KIRO_API_KEY")
        )
        base = (
            self.config.get("endpoint")
            or self.config.get("base_url")
            or "https://api.kilocode.ai/v1/chat/completions"
        )
        if base and "api.kiro.ai" in base:
            base = "https://api.kilocode.ai/v1/chat/completions"
        elif base and not base.endswith("/chat/completions") and not base.endswith("/messages"):
            if base.endswith("/v1"):
                base = f"{base}/chat/completions"
            elif base.endswith("/"):
                base = f"{base}v1/chat/completions"
            else:
                base = f"{base}/v1/chat/completions"
        self.endpoint = base
        self.mock_mode = self.config.get("mock", False)

    async def is_available(self) -> bool:
        return bool(self.auth_token or self.mock_mode)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        clean_model = model.replace("kr/", "")
        if self.mock_mode:
            yield create_sse_chunk(f"[Kiro AI Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.auth_token:
            yield create_sse_chunk("Kiro error: Token missing. Connect via Dashboard.", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
            "User-Agent": "KiteRouter/0.1.0",
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
                        yield create_sse_chunk(f"Kiro upstream HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Kiro notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
