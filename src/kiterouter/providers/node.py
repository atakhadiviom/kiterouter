"""Declarative provider node: a provider described by config, not by code.

A node is an entry in ``config.providers`` with ``kind: "node"``. Everything the
adapter needs — endpoint, paths, wire format, auth style, extra headers — comes
from that entry, so a changed path or a new model id is a form edit rather than a
commit and a restart. That is the whole point: providers change constantly and
the gateway should not have to be redeployed for it.

Providers that need real protocol work (Cursor's Connect-RPC, Antigravity's Cloud
Code Assist) keep their hand-written adapters; this covers the large
OpenAI/Anthropic/Gemini-compatible majority.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines
from kiterouter.providers.translate import (
    anthropic_headers,
    build_anthropic_request,
    build_gemini_request,
    build_responses_request,
    parse_anthropic_event,
    parse_gemini_event,
    parse_models_payload,
    parse_responses_event,
    parse_sse_data,
)

logger = logging.getLogger(__name__)

DEFAULT_CHAT_PATH = "/chat/completions"
DEFAULT_MODELS_PATH = "/models"
DEFAULT_AUTH = "bearer"

API_TYPES = ("openai-compatible", "openai-responses", "anthropic", "gemini")
AUTH_TYPES = ("bearer", "x-api-key", "none")


def join_url(base: str, path: str) -> str:
    """Join a base URL and a path without doubling or dropping the slash."""
    if not base:
        return path or ""
    if not path:
        return base
    if path.startswith(("http://", "https://")):
        return path
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


class NodeProvider(BaseProvider):
    """A provider built entirely from its config entry."""

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.name = name

        self.enabled = self.config.get("enabled", True)
        self.api_type = str(self.config.get("api_type") or "openai-compatible").lower()
        if self.api_type not in API_TYPES:
            # Unknown wire formats fall back to the OpenAI shape rather than
            # failing at request time with something unhelpful.
            logger.warning(
                "Node %s has unknown api_type %r; using openai-compatible",
                name,
                self.api_type,
            )
            self.api_type = "openai-compatible"

        self.base_url = str(
            self.config.get("base_url") or self.config.get("endpoint") or ""
        ).rstrip("/")
        self.chat_path = self.config.get("chat_path") or self._default_chat_path()
        self.models_path = self.config.get("models_path") or DEFAULT_MODELS_PATH
        self.auth = str(self.config.get("auth") or self._default_auth()).lower()
        self.custom_headers = {
            str(k): str(v)
            for k, v in (self.config.get("custom_headers") or {}).items()
        }
        self.api_key = self.config.get("api_key") or self.config.get("token") or ""

        models = self.config.get("models") or []
        if isinstance(models, list) and models:
            self.supported_models = [
                m if isinstance(m, str) else str(m.get("id") or m.get("name"))
                for m in models
                if m
            ]
        else:
            self.supported_models = []

        self.mock_mode = self.config.get("mock", False)

    # ------------------------------------------------------------ plumbing

    def _default_chat_path(self) -> str:
        # A Gemini endpoint is addressed by model, so the path carries a
        # placeholder the caller substitutes.
        if self.api_type == "gemini":
            return "/models/{model}:streamGenerateContent?alt=sse"
        if self.api_type == "openai-responses":
            return "/responses"
        if self.api_type == "anthropic":
            return "/messages"
        return DEFAULT_CHAT_PATH

    def _default_auth(self) -> str:
        return "x-api-key" if self.api_type == "anthropic" else DEFAULT_AUTH

    def chat_url(self, model: str) -> str:
        path = str(self.chat_path).replace("{model}", model)
        if path.startswith(("http://", "https://")):
            return path
        return join_url(self.base_url, path)

    def models_url(self) -> str:
        path = str(self.models_path)
        if path.startswith(("http://", "https://")):
            return path
        return join_url(self.base_url, path)

    def headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self.api_type == "anthropic":
            headers.update(anthropic_headers(self.api_key, self.custom_headers))
            return headers
        if self.auth == "bearer" and self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        elif self.auth == "x-api-key" and self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.custom_headers)
        return headers

    async def is_available(self) -> bool:
        if self.mock_mode:
            return True
        if not self.enabled or not self.base_url:
            return False
        if self.auth == "none":
            return True
        return bool(self.api_key)

    # -------------------------------------------------------------- models

    async def fetch_models(self) -> List[str]:
        """Read the live catalog from the node's models_path."""
        if not self.base_url:
            return list(self.supported_models)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(self.models_url(), headers=self._model_headers())
                if response.status_code != 200:
                    logger.debug(
                        "Node %s catalog returned HTTP %s", self.name, response.status_code
                    )
                    return list(self.supported_models)
                found = parse_models_payload(response.json())
                if found:
                    self.supported_models = found
                    return list(found)
        except Exception as e:
            logger.debug("Node %s catalog fetch failed: %s", self.name, e)
        return list(self.supported_models)

    def _model_headers(self) -> Dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_type == "anthropic":
            headers.update(anthropic_headers(self.api_key, self.custom_headers))
        elif self.auth == "bearer" and self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        elif self.auth == "x-api-key" and self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self.custom_headers)
        return headers

    # ------------------------------------------------------------- request

    def _payload(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        if self.api_type == "anthropic":
            return build_anthropic_request(messages, model, max_tokens, temperature)
        if self.api_type == "gemini":
            return build_gemini_request(messages, temperature, max_tokens)
        if self.api_type == "openai-responses":
            return build_responses_request(messages, model, max_tokens, temperature)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return payload

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(f"[{self.name} mock] {model}", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.base_url:
            yield create_sse_chunk(
                f"{self.name} error: no base_url configured for this provider node.",
                model=model,
            )
            yield "data: [DONE]\n\n"
            return

        payload = self._payload(model, messages, temperature, max_tokens)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", self.chat_url(model), headers=self.headers(), json=payload
                ) as response:
                    if response.status_code != 200:
                        body = ""
                        try:
                            body = (await response.aread()).decode("utf-8", "replace")
                        except Exception:
                            pass
                        yield create_sse_chunk(
                            f"{self.name} upstream HTTP {response.status_code}: {body[:200]}",
                            model=model,
                        )
                        yield "data: [DONE]\n\n"
                        return

                    if self.api_type == "openai-compatible":
                        async for line in iter_upstream_lines(response):
                            if line.strip():
                                yield f"{line}\n\n"
                        return

                    async for translated in self._translate_stream(response, model):
                        yield translated
        except Exception as e:
            yield create_sse_chunk(f"[{self.name} notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"

    async def _translate_stream(
        self, response: httpx.Response, model: str
    ) -> AsyncGenerator[str, None]:
        """Convert a non-OpenAI upstream stream into OpenAI-format SSE."""
        parser = {
            "anthropic": parse_anthropic_event,
            "gemini": parse_gemini_event,
            "openai-responses": parse_responses_event,
        }[self.api_type]

        finished = False
        async for line in iter_upstream_lines(response):
            event = parse_sse_data(line)
            if event is None:
                if line.strip() == "data: [DONE]":
                    break
                continue
            text, done = parser(event)
            if text:
                yield create_sse_chunk(text, model=model)
            if done:
                # Emit the terminal chunk here: a client needs a finish_reason to
                # know the completion ended, and `[DONE]` alone does not carry one.
                finished = True
                yield create_sse_chunk(finish_reason="stop", model=model)
                break

        if not finished:
            yield create_sse_chunk(finish_reason="stop", model=model)
        yield "data: [DONE]\n\n"

    # ------------------------------------------------------------ identity

    def describe(self) -> Dict[str, Any]:
        """Non-secret summary, for the dashboard and verification."""
        return {
            "id": self.name,
            "kind": "node",
            "enabled": self.enabled,
            "api_type": self.api_type,
            "base_url": self.base_url,
            "chat_url": self.chat_url("{model}") if self.base_url else None,
            "models_url": self.models_url(),
            "auth": self.auth,
            "has_credential": bool(self.api_key),
            "custom_headers": sorted(self.custom_headers),
            "verified_at": self.config.get("verified_at"),
            "last_error": self.config.get("last_error"),
        }
