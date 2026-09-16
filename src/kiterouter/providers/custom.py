"""Custom OpenAI-compatible provider."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk


class CustomProvider(BaseProvider):
    name = "custom"
    is_free = False
    supported_models = [
        "custom/default",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("CUSTOM_API_KEY")
        )
        self.endpoint = (
            self.config.get("endpoint")
            or os.environ.get("CUSTOM_BASE_URL", "https://api.openai.com/v1/chat/completions")
        )
        self.mock_mode = self.config.get("mock", False)

    async def fetch_models(self) -> List[str]:
        if not self.endpoint:
            return self.get_models()
        # Derive /v1/models from /v1/chat/completions
        base_models_url = self.endpoint.replace("/chat/completions", "/models")
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(base_models_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    models = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    if models:
                        self.supported_models = models
                        return self.supported_models
        except Exception:
            pass
        return self.get_models()

    async def is_available(self) -> bool:
        return bool((self.api_key and self.endpoint) or self.mock_mode)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        clean_model = model.replace("custom/", "")
        if self.mock_mode:
            yield create_sse_chunk(f"[Custom Provider Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

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
                        yield create_sse_chunk(f"Custom HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Custom notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
