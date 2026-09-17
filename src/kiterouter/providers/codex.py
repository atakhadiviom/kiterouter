"""OpenAI / Codex Provider."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines


class CodexProvider(BaseProvider):
    name = "codex"
    is_free = False
    supported_models = [
        "cx/gpt-5.5",
        "cx/gpt-5.4",
        "cx/gpt-5.3-codex",
        "cx/gpt-4o",
        "cx/o1",
        "cx/o3-mini",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("CODEX_API_KEY")
        )
        self.endpoint = self.config.get(
            "endpoint", "https://api.openai.com/v1/chat/completions"
        )
        self.mock_mode = self.config.get("mock", False)

    async def fetch_models(self) -> List[str]:
        if not self.api_key:
            return self.get_models()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {self.api_key}"}
                )
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
        return bool(self.api_key or self.mock_mode)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        clean_model = model.replace("cx/", "")
        if self.mock_mode:
            yield create_sse_chunk(f"[Codex Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.api_key:
            yield create_sse_chunk("Codex error: API key missing. Set OPENAI_API_KEY.", model=model)
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
                        yield create_sse_chunk(f"OpenAI/Codex HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in iter_upstream_lines(resp):
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Codex notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
