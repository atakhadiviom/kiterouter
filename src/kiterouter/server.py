"""KiteRouter FastAPI server: dual protocol (OpenAI + Anthropic) and RTK compression."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from kiterouter.compressor import compress_messages
from kiterouter.config import KiteConfig
from kiterouter.router import ProviderRouter

logger = logging.getLogger("kiterouter.server")

app = FastAPI(title="KiteRouter", version="0.1.0")

config = KiteConfig.load()
router = ProviderRouter(config=config.providers)
STATIC_DIR = Path(__file__).parent / "static"


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = True


class AnthropicMessageRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    system: Optional[str] = None
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = True


@app.get("/health")
async def health_check():
    available_providers = await router.get_available_providers()
    return {
        "status": "healthy",
        "service": "kiterouter",
        "rtk_enabled": config.enable_rtk,
        "available_providers": available_providers,
        "metrics": router.metrics,
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_path = STATIC_DIR / "dashboard.html"
    if html_path.exists():
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>KiteRouter is running</h1>"


@app.get("/v1/models")
async def list_models():
    models = [
        {"id": "claude-3-5-sonnet", "object": "model", "owned_by": "anthropic"},
        {"id": "claude-3-7-sonnet", "object": "model", "owned_by": "anthropic"},
        {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"},
        {"id": "deepseek-coder", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-r1", "object": "model", "owned_by": "deepseek"},
        {"id": "gemini-2.0-flash", "object": "model", "owned_by": "google"},
        {"id": "gemini-1.5-pro", "object": "model", "owned_by": "google"},
    ]
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    messages = req.messages
    if config.enable_rtk:
        messages, stats = compress_messages(
            messages, max_tool_chars=config.max_tool_chars
        )
        router.metrics["saved_tokens_approx"] += stats.saved_chars // 4

    if req.stream:
        return StreamingResponse(
            router.stream_with_fallback(
                model=req.model,
                messages=messages,
                temperature=req.temperature or 0.7,
                max_tokens=req.max_tokens,
            ),
            media_type="text/event-stream",
        )
    else:
        # Aggregate chunks into a single JSON response
        full_text = []
        async for chunk in router.stream_with_fallback(
            model=req.model,
            messages=messages,
            temperature=req.temperature or 0.7,
            max_tokens=req.max_tokens,
        ):
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    payload = json.loads(chunk[6:].strip())
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        full_text.append(delta["content"])
                except Exception:
                    continue

        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(full_text),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": len("".join(full_text)) // 4,
                "total_tokens": len("".join(full_text)) // 4,
            },
        }


@app.post("/v1/messages")
async def anthropic_messages(req: AnthropicMessageRequest):
    """Native Anthropic Messages API for Claude Code."""
    converted_messages: List[Dict[str, Any]] = []
    if req.system:
        converted_messages.append({"role": "system", "content": req.system})

    for m in req.messages:
        converted_messages.append(m)

    if config.enable_rtk:
        converted_messages, stats = compress_messages(
            converted_messages, max_tool_chars=config.max_tool_chars
        )
        router.metrics["saved_tokens_approx"] += stats.saved_chars // 4

    async def anthropic_event_stream():
        msg_id = f"msg_{int(time.time() * 1000)}"
        # 1. message_start
        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': req.model, 'stop_reason': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}})}\n\n"
        # 2. content_block_start
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

        # 3. Stream content deltas
        async for chunk in router.stream_with_fallback(
            model=req.model,
            messages=converted_messages,
            temperature=req.temperature or 0.7,
            max_tokens=req.max_tokens,
        ):
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    payload = json.loads(chunk[6:].strip())
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    content_text = delta.get("content", "")
                    if content_text:
                        event_data = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": content_text},
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(event_data)}\n\n"
                except Exception:
                    continue

        # 4. content_block_stop & message_delta & message_stop
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 50}})}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

    return StreamingResponse(anthropic_event_stream(), media_type="text/event-stream")
