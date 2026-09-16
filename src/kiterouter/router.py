"""Intelligent provider routing, fallback, and metrics engine."""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.opencode_free import OpenCodeFreeProvider
from kiterouter.providers.opencode_go import OpenCodeGoProvider
from kiterouter.providers.cursor import CursorProvider
from kiterouter.providers.antigravity import AntigravityProvider
from kiterouter.providers.cline import ClineProvider

logger = logging.getLogger("kiterouter.router")


class ProviderRouter:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.providers: Dict[str, BaseProvider] = {
            "opencode_free": OpenCodeFreeProvider(self.config.get("opencode_free")),
            "opencode_go": OpenCodeGoProvider(self.config.get("opencode_go")),
            "cursor": CursorProvider(self.config.get("cursor")),
            "antigravity": AntigravityProvider(self.config.get("antigravity")),
            "cline": ClineProvider(self.config.get("cline")),
        }
        self.fallback_chain = [
            "cursor",
            "antigravity",
            "opencode_go",
            "cline",
            "opencode_free",
        ]
        self.metrics = {
            "total_requests": 0,
            "successful_requests": 0,
            "fallback_events": 0,
            "saved_tokens_approx": 0,
        }

    async def get_available_providers(self) -> List[str]:
        """Return list of provider IDs currently available and healthy."""
        available = []
        for name, provider in self.providers.items():
            if await provider.is_available():
                available.append(name)
        return available

    def parse_model_and_provider(self, raw_model: str) -> tuple[Optional[str], str]:
        """
        Parse model string.
        Examples:
          'cursor/claude-3-5-sonnet' -> ('cursor', 'claude-3-5-sonnet')
          'antigravity/gemini-2.0-flash' -> ('antigravity', 'gemini-2.0-flash')
          'claude-3-5-sonnet' -> (None, 'claude-3-5-sonnet')
        """
        if "/" in raw_model:
            parts = raw_model.split("/", 1)
            if parts[0] in self.providers:
                return parts[0], parts[1]
        return None, raw_model

    async def stream_with_fallback(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """
        Attempt streaming from primary provider, falling back on error.
        """
        self.metrics["total_requests"] += 1
        explicit_provider, clean_model = self.parse_model_and_provider(model)

        candidates: List[BaseProvider] = []
        if explicit_provider and explicit_provider in self.providers:
            candidates.append(self.providers[explicit_provider])
        else:
            # Build candidates list based on availability
            for name in self.fallback_chain:
                p = self.providers[name]
                if await p.is_available():
                    candidates.append(p)

        if not candidates:
            # Always have at least OpenCode Free as emergency fallback
            candidates.append(self.providers["opencode_free"])

        last_error = None
        for i, provider in enumerate(candidates):
            try:
                # Stream from candidate provider
                got_chunk = False
                async for chunk in provider.stream_chat(
                    model=clean_model,
                    messages=messages,
                    **kwargs,
                ):
                    got_chunk = True
                    yield chunk

                if got_chunk:
                    self.metrics["successful_requests"] += 1
                    return
            except Exception as e:
                last_error = e
                self.metrics["fallback_events"] += 1
                logger.warning(
                    f"Provider {provider.name} failed for model {clean_model}: {e}. Trying fallback..."
                )
                continue

        # If all candidates failed
        yield create_sse_chunk(
            f"[KiteRouter Error: All providers exhausted. Last error: {last_error}]",
            model=clean_model,
        )
        yield create_sse_chunk(finish_reason="stop", model=clean_model)
        yield "data: [DONE]\n\n"
