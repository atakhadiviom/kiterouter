"""Cursor provider with Jyh cipher checksum and anti-ban header protection."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
import httpx

from kiterouter.providers.base import BaseProvider, create_sse_chunk


def generate_hashed_hex(input_str: str, salt: str = "") -> str:
    """Generate a stable 64-char SHA256 hex string."""
    return hashlib.sha256((input_str + salt).encode("utf-8")).hexdigest()


def generate_session_id(auth_token: str) -> str:
    """Stable UUID derived from auth token via DNS namespace."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, auth_token))


def generate_cursor_checksum(machine_id: str) -> str:
    """
    Generate the x-cursor-checksum header required by Cursor API (Jyh cipher).
    Matches 9router / OmniRoute checksum logic.
    """
    timestamp = int(time.time() * 1000) // 1000000

    # 6 bytes big-endian
    byte_array = bytearray([
        (timestamp >> 40) & 0xFF,
        (timestamp >> 32) & 0xFF,
        (timestamp >> 24) & 0xFF,
        (timestamp >> 16) & 0xFF,
        (timestamp >> 8) & 0xFF,
        timestamp & 0xFF,
    ])

    # Jyh cipher obfuscation
    t = 165
    for i in range(len(byte_array)):
        byte_array[i] = ((byte_array[i] ^ t) + (i % 256)) & 0xFF
        t = byte_array[i]

    # URL-safe base64 without padding
    encoded = base64.urlsafe_b64encode(byte_array).decode("utf-8").rstrip("=")
    return f"{encoded}{machine_id}"


def build_cursor_anti_ban_headers(
    access_token: str,
    machine_id: Optional[str] = None,
    ghost_mode: bool = True,
) -> Dict[str, str]:
    """
    Construct safe headers matching official Cursor IDE telemetry.
    Maintains stable machine IDs and proper cipher checksums to prevent account bans.
    """
    clean_token = access_token.split("::")[1] if "::" in access_token else access_token
    effective_machine_id = machine_id or generate_hashed_hex(clean_token, "machineId")
    session_id = generate_session_id(clean_token)
    client_key = generate_hashed_hex(clean_token)
    checksum = generate_cursor_checksum(effective_machine_id)

    return {
        "authorization": f"Bearer {clean_token}",
        "content-type": "application/json",
        "user-agent": "connect-es/1.6.1",
        "x-amzn-trace-id": f"Root={uuid.uuid4()}",
        "x-client-key": client_key,
        "x-cursor-checksum": checksum,
        "x-cursor-client-version": "0.46.11",
        "x-cursor-client-commit": "0fb762053c34788bb7760d5673f8a6d4c8589d50",
        "x-cursor-client-type": "ide",
        "x-cursor-client-os": "macos",
        "x-cursor-client-arch": "aarch64",
        "x-cursor-client-device-type": "desktop",
        "x-cursor-config-version": str(uuid.uuid4()),
        "x-cursor-timezone": "UTC",
        "x-ghost-mode": "true" if ghost_mode else "false",
        "x-request-id": str(uuid.uuid4()),
        "x-session-id": session_id,
    }


class CursorProvider(BaseProvider):
    name = "cursor"
    is_free = False
    supported_models = [
        "claude-3-5-sonnet",
        "claude-3-7-sonnet",
        "gpt-4o",
        "cursor-fast",
        "cursor-small",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.access_token = (
            self.config.get("token")
            or os.environ.get("CURSOR_TOKEN")
            or self._try_harvest_local_token()
        )
        self.machine_id = self.config.get("machine_id") or os.environ.get("CURSOR_MACHINE_ID")
        # NOTE (9router parity): official Cursor IDE talks to
        # /agent.v1.AgentService/Run via ConnectRPC protobuf + gzip with the
        # x-cursor-checksum (Jyh cipher) header. A plain OpenAI-style POST is
        # fingerprinted. KiteRouter therefore sends the official IDE headers
        # below, reuses ONE stable machine/session ID per token (never rotates
        # per request), and stops on 429/403 instead of retry-spamming.
        self.endpoint = self.config.get(
            "endpoint", "https://api2.cursor.sh/aiserver.v1.AiService/StreamChat"
        )
        self.agent_endpoint = self.config.get(
            "agent_endpoint", "https://api2.cursor.sh/agent.v1.AgentService/Run"
        )
        self.mock_mode = self.config.get("mock", False)

    def _try_harvest_local_token(self) -> Optional[str]:
        """Optionally read existing local Cursor session token from disk if present."""
        possible_paths = [
            os.path.expanduser("~/.cursor/auth.json"),
            os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/storage.json"),
        ]
        for p in possible_paths:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        token = data.get("accessToken") or data.get("token")
                        if token:
                            return token
                except Exception:
                    continue
        return None

    async def is_available(self) -> bool:
        return bool(self.access_token or self.mock_mode)

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(f"[Cursor Anti-Ban Bridge Mock Response for {model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.access_token:
            yield create_sse_chunk("Cursor error: No token found. Set CURSOR_TOKEN.", model=model)
            yield "data: [DONE]\n\n"
            return

        headers = build_cursor_anti_ban_headers(
            access_token=self.access_token,
            machine_id=self.machine_id,
            ghost_mode=True,
        )

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["maxTokens"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", self.endpoint, headers=headers, json=payload) as resp:
                    if resp.status_code in (429, 403):
                        # Anti-ban: never retry-spam. Surface the block and stop
                        # so the router can fail over to another provider.
                        yield create_sse_chunk(
                            f"Cursor upstream HTTP {resp.status_code} (rate-limit/block — stopping to protect account, failing over).",
                            model=model,
                        )
                        yield "data: [DONE]\n\n"
                        return
                    if resp.status_code != 200:
                        yield create_sse_chunk(f"Cursor upstream HTTP {resp.status_code}", model=model)
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
        except Exception as e:
            yield create_sse_chunk(f"[Cursor bridge notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
