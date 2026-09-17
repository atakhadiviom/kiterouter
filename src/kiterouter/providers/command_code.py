"""Command Code Provider (OpenAI-compatible, 200k context)."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines


DEFAULT_CATALOG = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "gpt-5.5",
    "gpt-5.4",
]


class CommandCodeProvider(BaseProvider):
    name = "command_code"
    is_free = False
    context_window = 200000
    base_url = "https://api.commandcode.ai"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("COMMAND_CODE_API_KEY")
            or os.environ.get("CMD_API_KEY")
        )
        self.endpoint = self.config.get(
            "endpoint", f"{self.base_url}/provider/v1/chat/completions"
        )
        self.models_endpoint = self.config.get(
            "models_endpoint", f"{self.base_url}/provider/v1/models"
        )
        self.mock_mode = self.config.get("mock", False)

        # Set initial supported models
        self.supported_models = [f"cmd/{m}" for m in DEFAULT_CATALOG] + list(DEFAULT_CATALOG)

    async def is_available(self) -> bool:
        return bool(self.api_key or self.mock_mode)

    async def fetch_models(self) -> List[str]:
        """Fetch remote models catalog via Bearer token, fallback to default catalog."""
        if not self.api_key:
            return list(DEFAULT_CATALOG)

        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(self.models_endpoint, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    models_data = data.get("data") if isinstance(data, dict) else data
                    if isinstance(models_data, list):
                        ids = [m.get("id") for m in models_data if isinstance(m, dict) and m.get("id")]
                        if ids:
                            self.supported_models = [f"cmd/{i}" for i in ids] + ids
                            return ids
        except Exception:
            pass

        return list(DEFAULT_CATALOG)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        clean_model = model
        for prefix in ("cmd/", "ccp/", "command-code/", "command_code/"):
            if clean_model.startswith(prefix):
                clean_model = clean_model[len(prefix):]
                break

        if self.mock_mode:
            yield create_sse_chunk(f"[Command Code Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.api_key:
            yield create_sse_chunk(
                "Command Code error: API key missing. Configure api_key in settings or COMMAND_CODE_API_KEY.",
                model=model,
            )
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
                        yield create_sse_chunk(
                            f"Command Code upstream HTTP {resp.status_code}", model=model
                        )
                        yield "data: [DONE]\n\n"
                        return

                    async for line in iter_upstream_lines(resp):
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Command Code notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
