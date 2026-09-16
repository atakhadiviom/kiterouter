"""Antigravity (Google Cloud Code Assist / Gemini) provider with anti-ban safeguards."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx

from kiterouter.providers.base import BaseProvider, create_sse_chunk

ANTIGRAVITY_IDE_VERSION = "2.11.0"
# Chat traffic goes to v1internal generateContent (9router parity).
# loadCodeAssist/onboardUser are onboarding-only — using them for chat triggers 403.
ANTIGRAVITY_CHAT_ACTION = "streamGenerateContent?alt=sse"
ANTIGRAVITY_IDE_BASE_URL = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_IDE_USER_AGENT = f"antigravity/ide/{ANTIGRAVITY_IDE_VERSION} darwin/arm64"
MAX_ANTIGRAVITY_OUTPUT_TOKENS = 64000

# Competing-client branding that Antigravity flags with 429 Quota Exhausted.
# Mirrors 9router ANTIGRAVITY_PROMPT_REWRITES.
ANTIGRAVITY_PROMPT_REWRITES = [
    ("Zed", "Antigravity"),
    ("OpenCode", "Antigravity"),
    ("Kilo Code", "Antigravity"),
    ("kilo-code", "antigravity"),
    ("claude-code", "antigravity"),
]

# Fields Google Cloud Code Assist rejects (sending these causes 400 Bad Request abuse flags)
ANTIGRAVITY_REQUEST_BLACKLIST = {
    "output_config",
    "thinking",
    "reasoning_effort",
    "reasoning",
    "enable_thinking",
    "thinking_budget",
    "thinkingConfig",
}


def sanitize_antigravity_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip fields that Google generateContent rejects to prevent 400 abuse flags."""
    clean = dict(payload)
    for key in list(clean.keys()):
        if key in ANTIGRAVITY_REQUEST_BLACKLIST:
            del clean[key]
    return clean


def build_ide_request_id(session_id: str, model: str, step: int = 1) -> str:
    """
    Construct compliant requestId matching Antigravity IDE signature:
    agent/{conversationId}/{timestamp}/{trajectoryId}/{step}
    """
    conv_hash = hashlib.sha256(f"antigravity:conv:{session_id}".encode()).hexdigest()[:16]
    traj_hash = hashlib.sha256(f"antigravity:traj:{session_id}:{model}".encode()).hexdigest()[:16]
    now_ms = int(time.time() * 1000)
    return f"agent/{conv_hash}/{now_ms}/{traj_hash}/{step}"


def build_antigravity_headers(
    auth_token: str,
    machine_id: Optional[str] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build exact IDE headers required to prevent anomaly bans.
    Mirrors 9router: official User-Agent only, plus session identity headers.
    Never send Stainless SDK markers (x-stainless-*) — OmniRoute strips them
    because they fingerprint third-party clients.
    """
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "User-Agent": ANTIGRAVITY_IDE_USER_AGENT,
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": json.dumps({
            "ideType": "ANTIGRAVITY",
            "platform": "DARWIN_ARM64",
            "pluginType": "GEMINI"
        }),
    }
    # Session identity headers — absence triggers 403 SERVICE_DISABLED per
    # 9router issue #1138 (Antigravity-Manager reference).
    if machine_id:
        headers["x-machine-id"] = machine_id
    if session_id:
        headers["x-vscode-sessionid"] = session_id
    if project_id:
        headers["x-goog-user-project"] = project_id
    return headers


def rewrite_competing_branding(text: str) -> str:
    """Rewrite competing-client names in system prompts (9router parity)."""
    if not isinstance(text, str):
        return text
    for src, dst in ANTIGRAVITY_PROMPT_REWRITES:
        text = text.replace(src, dst)
    return text


class AntigravityProvider(BaseProvider):
    name = "antigravity"
    is_free = False
    supported_models = [
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-2.0-flash-thinking",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.auth_token = (
            self.config.get("token")
            or os.environ.get("ANTIGRAVITY_TOKEN")
            or os.environ.get("GEMINI_API_KEY")
        )
        self.project_id = self.config.get("project_id") or os.environ.get("ANTIGRAVITY_PROJECT_ID", "")
        self.machine_id = self.config.get("machine_id") or os.environ.get("ANTIGRAVITY_MACHINE_ID", "")
        # Chat endpoint parity with 9router AntigravityExecutor.buildUrl():
        # streamGenerateContent for streaming, generateContent otherwise.
        # Custom endpoint override still respected.
        self.chat_endpoint = self.config.get(
            "endpoint", f"{ANTIGRAVITY_IDE_BASE_URL}/v1internal:{ANTIGRAVITY_CHAT_ACTION}"
        )
        self.mock_mode = self.config.get("mock", False)

    async def is_available(self) -> bool:
        return bool(self.auth_token or self.mock_mode)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(f"[Antigravity Safe Bridge Mock Response for {model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.auth_token:
            yield create_sse_chunk("Antigravity error: Token missing. Set ANTIGRAVITY_TOKEN or GEMINI_API_KEY.", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = build_antigravity_headers(
            self.auth_token,
            machine_id=self.machine_id or None,
            session_id=kwargs.get("session_id"),
            project_id=self.project_id or None,
        )
        # Rewrite competing branding in system-role messages before forwarding.
        safe_messages = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str):
                safe_messages.append({**m, "content": rewrite_competing_branding(m["content"])})
            else:
                safe_messages.append(m)

        request_id = build_ide_request_id(
            session_id=kwargs.get("session_id", "default"),
            model=model,
            step=kwargs.get("step", 1),
        )

        # Cap output tokens — 9router clamps to 64k to avoid abuse flags.
        safe_max_tokens = max_tokens
        if safe_max_tokens and safe_max_tokens > MAX_ANTIGRAVITY_OUTPUT_TOKENS:
            safe_max_tokens = MAX_ANTIGRAVITY_OUTPUT_TOKENS

        raw_payload = {
            "model": model,
            "messages": safe_messages,
            "temperature": temperature,
            "requestId": request_id,
        }
        # Only forward whitelisted extras — never leak Stainless markers or
        # thinking fields (9router strips them at body root AND body.request).
        for k in ("project", "userAgent", "requestType", "session_id"):
            if k in kwargs:
                raw_payload[k] = kwargs[k]
        if safe_max_tokens:
            raw_payload["max_tokens"] = safe_max_tokens

        # Clean blacklisted fields to prevent 400 abuse triggers
        clean_payload = sanitize_antigravity_payload(raw_payload)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", self.chat_endpoint, headers=headers, json=clean_payload) as resp:
                    if resp.status_code == 429:
                        yield create_sse_chunk("Antigravity rate limit (429). Backing off to prevent suspension.", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    if resp.status_code != 200:
                        yield create_sse_chunk(f"Antigravity HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Antigravity safe notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
