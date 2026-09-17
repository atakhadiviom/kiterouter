"""OpenCode Free: Zero-auth public provider."""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx
from kiterouter.providers.base import BaseProvider, create_sse_chunk
from kiterouter.providers.streaming import UpstreamStalled, iter_upstream_lines

logger = logging.getLogger(__name__)

OPENCODE_BASE = "https://opencode.ai"
OPENCODE_FREE_ENDPOINT = f"{OPENCODE_BASE}/zen/v1/chat/completions"
OPENCODE_MODELS_ENDPOINT = f"{OPENCODE_BASE}/zen/v1/models"


def build_opencode_headers(session_id: Optional[str] = None) -> Dict[str, str]:
    """Required headers for OpenCode gateway routing."""
    sid = session_id or f"ses_{uuid.uuid4().hex[:32]}"
    req_id = f"msg_{uuid.uuid4().hex[:32]}"
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer public",
        "User-Agent": "opencode",
        "x-opencode-client": "desktop",
        "x-opencode-session": sid,
        "x-opencode-request": req_id,
        "Accept": "text/event-stream",
    }


class OpenCodeFreeProvider(BaseProvider):
    name = "opencode_free"
    is_free = True
    supported_models = [
        "nemotron-3-ultra-free",
        "mimo-v2.5-free",
        "ling-3.0-flash-fin-free",
        "deepseek-v4-flash-free",
        "muse-spark-1.3-contributor-free",
        "muse-spark-1.2-contributor-free",
        "nemotron-3.5-lightning-free",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.endpoint = self.config.get("endpoint", OPENCODE_FREE_ENDPOINT)
        self.mock_mode = self.config.get("mock", False)

    async def fetch_models(self) -> List[str]:
        """Fetch real-time live models from OpenCode catalog."""
        try:
            headers = build_opencode_headers()
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(OPENCODE_MODELS_ENDPOINT, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    # Filter free models from catalog
                    all_models = [m.get("id") for m in data.get("data", []) if m.get("id")]
                    free_models = [m for m in all_models if "-free" in m]
                    if free_models:
                        self.supported_models = free_models
                        return self.supported_models
        except Exception as e:
            logger.debug("OpenCode Free model fetch failed: %s", e)
        return self.get_models()

    async def is_available(self) -> bool:
        return True

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(
                f"[OpenCode Free Mock Response from {model}]", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = build_opencode_headers(kwargs.get("session_id"))

        # muse-spark models on OpenCode require the OpenAI Responses API at /zen/v1/responses
        if model.startswith("muse-spark"):
            async for chunk in self._stream_responses_api(
                model=model,
                messages=messages,
                headers=headers,
                max_tokens=max_tokens,
                **kwargs,
            ):
                yield chunk
            return

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST", self.endpoint, headers=headers, json=payload
                ) as resp:
                    if resp.status_code != 200:
                        err_text = ""
                        try:
                            err_bytes = await resp.aread()
                            err_json = json.loads(err_bytes.decode())
                            err_text = (
                                err_json.get("error", {}).get("message")
                                or err_json.get("message")
                                or ""
                            )
                        except Exception:
                            pass
                        msg = f"OpenCode Free Error: HTTP {resp.status_code}"
                        if err_text:
                            msg += f" - {err_text}"
                        yield create_sse_chunk(msg, model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in iter_upstream_lines(resp):
                        if line:
                            yield f"{line}\n\n"
        except UpstreamStalled as stalled:
            # This upstream is known to hold the connection open with keep-alives.
            # Say so rather than letting the client wait forever.
            yield create_sse_chunk(
                f"OpenCode Free stalled: {stalled}", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield create_sse_chunk(
                f"[OpenCode Free connection notice: {e}]", model=model
            )
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"

    async def _stream_responses_api(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        headers: Dict[str, str],
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        system_msgs = [
            m.get("content", "") for m in messages if m.get("role") == "system"
        ]
        non_system = [m for m in messages if m.get("role") != "system"]

        payload: Dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content", ""),
                }
                for m in non_system
            ],
            "stream": True,
            "max_output_tokens": max(max_tokens or 0, 4096),
            "reasoning": {"effort": "low"},
        }
        if system_msgs:
            payload["instructions"] = "\n\n".join(str(s) for s in system_msgs)

        responses_url = f"{OPENCODE_BASE}/zen/v1/responses"
        chunk_id = f"chatcmpl-{int(time.time() * 1000)}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST", responses_url, headers=headers, json=payload
                ) as resp:
                    if resp.status_code != 200:
                        err_text = ""
                        try:
                            err_bytes = await resp.aread()
                            err_json = json.loads(err_bytes.decode())
                            err_text = (
                                err_json.get("error", {}).get("message")
                                or err_json.get("message")
                                or ""
                            )
                        except Exception:
                            pass
                        msg = f"OpenCode Free Error: HTTP {resp.status_code}"
                        if err_text:
                            msg += f" - {err_text}"
                        yield create_sse_chunk(msg, model=model, chunk_id=chunk_id)
                        yield "data: [DONE]\n\n"
                        return

                    async for raw_line in iter_upstream_lines(resp):
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if not data_str or data_str == "[DONE]":
                                continue
                            try:
                                event_obj = json.loads(data_str)
                                ev_type = event_obj.get("type")
                                if ev_type == "response.output_text.delta":
                                    delta = event_obj.get("delta", "")
                                    if delta:
                                        yield create_sse_chunk(
                                            delta, model=model, chunk_id=chunk_id
                                        )
                                elif ev_type == "response.completed":
                                    yield create_sse_chunk(
                                        finish_reason="stop",
                                        model=model,
                                        chunk_id=chunk_id,
                                    )
                                    yield "data: [DONE]\n\n"
                                    return
                                elif ev_type == "response.incomplete":
                                    reason = (
                                        event_obj.get("response", {})
                                        .get("incomplete_details", {})
                                        or {}
                                    ).get("reason")
                                    yield create_sse_chunk(
                                        finish_reason=reason or "length",
                                        model=model,
                                        chunk_id=chunk_id,
                                    )
                                    yield "data: [DONE]\n\n"
                                    return
                            except Exception:
                                pass

                    yield create_sse_chunk(
                        finish_reason="stop", model=model, chunk_id=chunk_id
                    )
                    yield "data: [DONE]\n\n"
        except Exception as e:
            yield create_sse_chunk(
                f"[OpenCode Free connection notice: {e}]",
                model=model,
                chunk_id=chunk_id,
            )
            yield create_sse_chunk(finish_reason="stop", model=model, chunk_id=chunk_id)
            yield "data: [DONE]\n\n"
