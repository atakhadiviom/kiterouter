"""GitHub Copilot Provider."""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines


class CopilotProvider(BaseProvider):
    name = "copilot"
    is_free = False
    supported_models = [
        "gh/gpt-4o",
        "gh/claude-3.5-sonnet",
        "gh/o1",
        "gh/o3-mini",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.token = (
            self.config.get("token")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("COPILOT_TOKEN")
        )
        self.endpoint = self.config.get(
            "endpoint", "https://api.githubcopilot.com/chat/completions"
        )
        self.mock_mode = self.config.get("mock", False)

    async def is_available(self) -> bool:
        return bool(self.token or self.mock_mode)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        clean_model = model.replace("gh/", "")
        if self.mock_mode:
            yield create_sse_chunk(f"[GitHub Copilot Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.token:
            yield create_sse_chunk("Copilot error: Token missing. Set GITHUB_TOKEN.", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "GitHubCopilotChat/0.24.0",
            "Editor-Version": "vscode/1.96.0",
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
                        yield create_sse_chunk(f"Copilot upstream HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in iter_upstream_lines(resp):
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Copilot notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
