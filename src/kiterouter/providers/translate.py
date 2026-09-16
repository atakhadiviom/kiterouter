"""Shared wire-format translation between OpenAI and upstream APIs.

These conversions used to live inside individual adapters. They are factored out
here so a declarative provider node and a hand-written adapter cannot drift apart
— which matters because these formats move: OpenCode Free needed a whole separate
Responses-API path just to keep one model working.

Everything here is pure: no I/O, no config, no logging. That keeps it testable
against canned upstream payloads.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

ANTHROPIC_VERSION = "2023-06-01"


def flatten_text(content: Any) -> str:
    """Pull plain text out of a message body, OpenAI or Anthropic shaped.

    Accepts a string, or a list of content parts such as
    ``[{"type": "text", "text": "..."}]``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" or "text" in part:
                    chunks.append(str(part.get("text", "")))
            elif isinstance(part, str):
                chunks.append(part)
        return "".join(chunks)
    return str(content)


def extract_delta_text(delta: Dict[str, Any]) -> Optional[str]:
    """Text carried by one OpenAI-style streaming delta, if any.

    Providers disagree on the field: ``content`` for most, ``reasoning_content``
    or ``reasoning`` for thinking models. Shared so the probe, the router and the
    adapters all agree on what counts as output.
    """
    if not isinstance(delta, dict):
        return None
    for key in ("content", "reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ── Anthropic ────────────────────────────────────────────────────────────────

def build_anthropic_request(
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    """OpenAI messages → Anthropic Messages payload (system hoisted out)."""
    converted: List[Dict[str, Any]] = []
    system_parts: List[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        text = flatten_text(message.get("content"))
        if role == "system":
            system_parts.append(text)
        else:
            converted.append({"role": "assistant" if role == "assistant" else "user", "content": text})

    if not converted:
        converted.append({"role": "user", "content": "Hello"})

    payload: Dict[str, Any] = {
        "model": model,
        "messages": converted,
        # Anthropic requires max_tokens; it is not optional there.
        "max_tokens": max_tokens or 4096,
        "temperature": temperature,
        "stream": True,
    }
    system_text = "\n".join(part for part in system_parts if part.strip()).strip()
    if system_text:
        payload["system"] = system_text
    return payload


def anthropic_headers(api_key: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "x-api-key": api_key or "",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def parse_anthropic_event(event: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Anthropic event → (text, is_finished).

    Handles ``content_block_delta`` text and thinking deltas, plus the terminal
    events. Returns ``(None, False)`` for events that carry nothing.
    """
    if not isinstance(event, dict):
        return None, False
    kind = event.get("type")
    if kind == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text") or "", False
        if delta.get("type") == "thinking_delta":
            return delta.get("thinking") or "", False
        return None, False
    if kind == "message_stop":
        return None, True
    return None, False


# ── Gemini ───────────────────────────────────────────────────────────────────

def openai_messages_to_gemini_contents(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """OpenAI messages → (contents, systemInstruction) for a Gemini-shaped API."""
    contents: List[Dict[str, Any]] = []
    system_parts: List[Dict[str, str]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        text = flatten_text(message.get("content"))
        if role == "system":
            system_parts.append({"text": text})
        elif role in ("assistant", "model"):
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    system = {"parts": system_parts} if system_parts else None
    return contents, system


def build_gemini_request(
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """OpenAI messages → a plain generateContent payload."""
    contents, system = openai_messages_to_gemini_contents(messages)
    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens or 4096,
        },
    }
    if system:
        payload["systemInstruction"] = system
    return payload


def parse_gemini_event(event: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Gemini stream chunk → (text, is_finished)."""
    if not isinstance(event, dict):
        return None, False

    candidates = event.get("candidates") or []
    text = ""
    finished = False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        parts = ((candidate.get("content") or {}).get("parts")) or []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text += part["text"]
        if candidate.get("finishReason"):
            finished = True
    return (text or None), finished


# ── OpenAI Responses API ─────────────────────────────────────────────────────

def build_responses_request(
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """OpenAI chat messages → Responses API payload."""
    instructions = "\n\n".join(
        flatten_text(m.get("content"))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
    )
    items = [
        {"role": m.get("role", "user"), "content": flatten_text(m.get("content"))}
        for m in messages
        if isinstance(m, dict) and m.get("role") != "system"
    ]

    payload: Dict[str, Any] = {
        "model": model,
        "input": items or [{"role": "user", "content": "Hello"}],
        "stream": True,
        "max_output_tokens": max(max_tokens or 0, 4096),
    }
    if instructions:
        payload["instructions"] = instructions
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


def parse_responses_event(event: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Responses API event → (text, is_finished)."""
    if not isinstance(event, dict):
        return None, False
    kind = event.get("type")
    if kind == "response.output_text.delta":
        return event.get("delta") or "", False
    if kind == "response.completed":
        return None, True
    if kind == "response.incomplete":
        return None, True
    if kind == "error":
        message = (event.get("error") or {}).get("message") or "responses API error"
        return f"[{message}]", True
    return None, False


def parse_sse_data(line: str) -> Optional[Any]:
    """Decode one ``data:`` SSE line, or None when it is a sentinel/blank."""
    if not line:
        return None
    text = line[6:].strip() if line.startswith("data: ") else line.strip()
    if not text or text == "[DONE]":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


# ── model catalogs ───────────────────────────────────────────────────────────

def parse_models_payload(payload: Any) -> List[str]:
    """Pull model ids out of a provider's catalog response.

    Providers disagree on the envelope: OpenAI uses ``{"data": [{"id": …}]}``,
    some return a bare array, and others use ``{"models": [{"name": …}]}``. All
    three shapes are accepted because getting this wrong shows up later as a
    confusing 400 from the chat endpoint.
    """
    if not isinstance(payload, (dict, list)):
        return []

    entries: Any = payload
    if isinstance(payload, dict):
        for key in ("data", "models", "model_ids", "result"):
            if isinstance(payload.get(key), (list, dict)):
                entries = payload[key]
                break
        else:
            return []

    if isinstance(entries, dict):
        entries = list(entries.values())

    ids: List[str] = []
    for entry in entries or []:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            value = entry.get("id") or entry.get("name") or entry.get("model")
            if isinstance(value, str) and value:
                ids.append(value)
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(ids))
