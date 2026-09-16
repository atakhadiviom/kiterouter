"""Anthropic Claude Code Provider."""
from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk


class ClaudeProvider(BaseProvider):
    name = "claude"
    is_free = False
    supported_models = [
        "cc/claude-opus-4-7",
        "cc/claude-opus-4-6",
        "cc/claude-sonnet-4-6",
        "cc/claude-3-7-sonnet-20250219",
        "cc/claude-3-5-sonnet-20241022",
        "cc/claude-3-5-haiku-20241022",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_API_KEY")
        )
        self.endpoint = self.config.get(
            "endpoint", "https://api.anthropic.com/v1/messages"
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
        clean_model = model.replace("cc/", "")
        if self.mock_mode:
            yield create_sse_chunk(f"[Claude Mock Response for {clean_model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.api_key:
            yield create_sse_chunk("Claude error: API key missing. Set ANTHROPIC_API_KEY.", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # Format messages for Anthropic (separate system message)
        anthropic_messages = []
        system_text = ""
        for m in messages:
            if m.get("role") == "system":
                system_text += f"\n{m.get('content', '')}"
            else:
                anthropic_messages.append({"role": m.get("role"), "content": m.get("content")})

        payload = {
            "model": clean_model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
            "stream": True,
        }
        if system_text.strip():
            payload["system"] = system_text.strip()

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", self.endpoint, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        yield create_sse_chunk(f"Anthropic HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            raw = line[6:].strip()
                            if raw == "[DONE]":
                                yield "data: [DONE]\n\n"
                                break
                            try:
                                event = json.loads(raw)
                                ev_type = event.get("type")
                                if ev_type == "content_block_delta":
                                    delta = event.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        yield create_sse_chunk(delta.get("text", ""), model=model)
                                elif ev_type == "message_stop":
                                    yield create_sse_chunk(finish_reason="stop", model=model)
                            except Exception:
                                continue
        except Exception as e:
            yield create_sse_chunk(f"[Claude notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
