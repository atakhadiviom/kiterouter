"""Base class for all upstream LLM providers."""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional


def create_sse_chunk(
    content: str = "",
    model: str = "kiterouter-model",
    finish_reason: Optional[str] = None,
    chunk_id: Optional[str] = None,
) -> str:
    """Format an OpenAI-compatible SSE chunk line."""
    if chunk_id is None:
        chunk_id = f"chatcmpl-{int(time.time() * 1000)}"

    delta: Dict[str, Any] = {}
    if content:
        delta["content"] = content
    if finish_reason:
        delta["role"] = None

    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(data)}\n\n"


class BaseProvider(ABC):
    name: str = "base"
    is_free: bool = False
    supported_models: List[str] = []

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if provider is configured and reachable."""
        pass

    @abstractmethod
    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Yield OpenAI-compatible SSE strings ('data: {...}\\n\\n')."""
        yield ""  # pragma: no cover

    async def chat_complete(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Non-streaming completion by aggregating SSE chunks."""
        full_content = []
        async for chunk in self.stream_chat(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        ):
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    payload = json.loads(chunk[6:].strip())
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        full_content.append(delta["content"])
                except Exception:
                    continue

        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(full_content),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
