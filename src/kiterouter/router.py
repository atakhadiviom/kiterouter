"""Intelligent provider routing, fallback, and metrics engine."""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.cursor import CursorProvider
from kiterouter.providers.antigravity import AntigravityProvider
from kiterouter.providers.cline import ClineProvider
from kiterouter.providers.opencode_free import OpenCodeFreeProvider
from kiterouter.providers.opencode_go import OpenCodeGoProvider
from kiterouter.providers.kiro import KiroProvider
from kiterouter.providers.glm import GLMProvider
from kiterouter.providers.minimax import MiniMaxProvider
from kiterouter.providers.codex import CodexProvider
from kiterouter.providers.command_code import CommandCodeProvider
from kiterouter.providers.claude import ClaudeProvider
from kiterouter.providers.copilot import CopilotProvider
from kiterouter.providers.vertex import VertexProvider
from kiterouter.providers.custom import CustomProvider

logger = logging.getLogger("kiterouter.router")


class ProviderRouter:
    def __init__(self, config: Optional[Any] = None):
        if config is None:
            self.config = {}
        elif hasattr(config, "providers"):
            self.config = config.providers
        elif isinstance(config, dict):
            self.config = config
        else:
            self.config = {}
        self.providers: Dict[str, BaseProvider] = {
            "cursor": CursorProvider(self.config.get("cursor")),
            "antigravity": AntigravityProvider(self.config.get("antigravity")),
            "cline": ClineProvider(self.config.get("cline")),
            "opencode_free": OpenCodeFreeProvider(self.config.get("opencode_free")),
            "opencode_go": OpenCodeGoProvider(self.config.get("opencode_go")),
            "kiro": KiroProvider(self.config.get("kiro")),
            "glm": GLMProvider(self.config.get("glm")),
            "minimax": MiniMaxProvider(self.config.get("minimax")),
            "codex": CodexProvider(self.config.get("codex")),
            "command_code": CommandCodeProvider(self.config.get("command_code")),
            "claude": ClaudeProvider(self.config.get("claude")),
            "copilot": CopilotProvider(self.config.get("copilot")),
            "vertex": VertexProvider(self.config.get("vertex")),
            "custom": CustomProvider(self.config.get("custom")),
        }
        # Dynamically register any imported/custom configured providers
        known_endpoints = {
            "groq": ("https://api.groq.com/openai/v1/chat/completions", ["llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b"]),
            "openrouter": ("https://openrouter.ai/api/v1/chat/completions", ["anthropic/claude-3.5-sonnet", "openai/gpt-4o"]),
            "deepseek": ("https://api.deepseek.com/v1/chat/completions", ["deepseek-chat", "deepseek-reasoner"]),
            "opencode": ("https://api.opencode.ai/v1/chat/completions", ["claude-3-5-sonnet", "gpt-4o"]),
        }
        for p_name, p_conf in self.config.items():
            if p_name not in self.providers and isinstance(p_conf, dict):
                ep = p_conf.get("endpoint")
                models = p_conf.get("models")
                if not ep and p_name in known_endpoints:
                    ep = known_endpoints[p_name][0]
                    if not models:
                        models = known_endpoints[p_name][1]
                custom_p = CustomProvider({
                    "endpoint": ep or "https://api.openai.com/v1/chat/completions",
                    "api_key": p_conf.get("api_key") or p_conf.get("token") or "",
                    **p_conf
                })
                custom_p.name = p_name
                if models:
                    custom_p.supported_models = [f"{p_name}/{m}" if not m.startswith(f"{p_name}/") else m for m in models]
                self.providers[p_name] = custom_p

        # 3-Tier fallback hierarchy (Subscription -> Cheap -> Free)
        self.fallback_chain = [
            "cursor",
            "antigravity",
            "claude",
            "codex",
            "command_code",
            "copilot",
            "opencode_go",
            "glm",
            "minimax",
            "kiro",
            "vertex",
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
        """Return list of provider IDs currently configured and available."""
        available = []
        for name, provider in self.providers.items():
            if await provider.is_available():
                available.append(name)
        return available

    def get_all_models(self) -> List[Dict[str, Any]]:
        """Return all available models grouped across all providers."""
        models = []
        seen_ids = set()
        for provider_id, provider in self.providers.items():
            for m in getattr(provider, "supported_models", []):
                model_id = m if "/" in m else f"{provider_id}/{m}"
                if model_id not in seen_ids:
                    seen_ids.add(model_id)
                    models.append({
                        "id": model_id,
                        "raw_id": m.split("/", 1)[1] if "/" in m else m,
                        "object": "model",
                        "owned_by": provider_id,
                        "permission": [],
                    })
        # Include any models defined or fetched under config.providers (including imported providers)
        for p_name, p_conf in self.config.items():
            if isinstance(p_conf, dict) and "models" in p_conf and isinstance(p_conf["models"], list):
                for m in p_conf["models"]:
                    model_id = m if "/" in m else f"{p_name}/{m}"
                    if model_id not in seen_ids:
                        seen_ids.add(model_id)
                        models.append({
                            "id": model_id,
                            "raw_id": m.split("/", 1)[1] if "/" in m else m,
                            "object": "model",
                            "owned_by": p_name,
                            "permission": [],
                        })
        return models

    def parse_model_and_provider(self, raw_model: str) -> tuple[Optional[str], str]:
        """
        Parse model string with prefixes.
        Examples:
          'cu/claude-3-5-sonnet' or 'cursor/claude-3-5-sonnet' -> ('cursor', 'claude-3-5-sonnet')
          'cc/claude-opus-4-7' or 'claude/claude-opus-4-7' -> ('claude', 'claude-opus-4-7')
          'kr/claude-sonnet-4.5' -> ('kiro', 'kr/claude-sonnet-4.5')
          'glm/glm-5.1' -> ('glm', 'glm/glm-5.1')
          'cx/gpt-5.4' -> ('codex', 'cx/gpt-5.4')
        """
        prefix_map = {
            "cu": "cursor",
            "cursor": "cursor",
            "ag": "antigravity",
            "antigravity": "antigravity",
            "cc": "claude",
            "claude": "claude",
            "cx": "codex",
            "codex": "codex",
            "cmd": "command_code",
            "ccp": "command_code",
            "command-code": "command_code",
            "command_code": "command_code",
            "gh": "copilot",
            "copilot": "copilot",
            "kr": "kiro",
            "kiro": "kiro",
            "glm": "glm",
            "minimax": "minimax",
            "vertex": "vertex",
            "oc": "opencode_free",
            "opencode_free": "opencode_free",
            "opencode_go": "opencode_go",
            "cline": "cline",
            "custom": "custom",
        }

        if "/" in raw_model:
            prefix, rest = raw_model.split("/", 1)
            if prefix in prefix_map:
                target_provider = prefix_map[prefix]
                if target_provider in self.providers:
                    return target_provider, rest
            elif prefix in self.providers:
                return prefix, rest

        return None, raw_model

    def select_provider(self, raw_model: str) -> tuple[BaseProvider, str]:
        """Resolve target provider and stripped model name."""
        prov_name, clean_model = self.parse_model_and_provider(raw_model)
        if prov_name and prov_name in self.providers:
            return self.providers[prov_name], clean_model
        return self.providers["opencode_free"], raw_model

    async def stream_with_fallback(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """
        Attempt streaming from requested provider, falling back on error.
        """
        self.metrics["total_requests"] += 1
        explicit_provider, clean_model = self.parse_model_and_provider(model)

        candidates: List[BaseProvider] = []
        if explicit_provider and explicit_provider in self.providers:
            candidates.append(self.providers[explicit_provider])
        else:
            for name in self.fallback_chain:
                p = self.providers.get(name)
                if p and await p.is_available():
                    candidates.append(p)

        if not candidates:
            candidates.append(self.providers["opencode_free"])

        last_error = None
        for i, provider in enumerate(candidates):
            try:
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

        yield create_sse_chunk(
            f"[KiteRouter Error: All providers exhausted. Last error: {last_error}]",
            model=clean_model,
        )
        yield create_sse_chunk(finish_reason="stop", model=clean_model)
        yield "data: [DONE]\n\n"
