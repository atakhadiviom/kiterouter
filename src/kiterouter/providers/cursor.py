"""Cursor provider — agent.v1.AgentService/Run wire protocol (proven live).

Talks the Connect-RPC protobuf surface with CLI impersonation (matches
cursor-agent's actual headers: no checksum, no machineId, no x-amzn-trace-id).
Server-config discovery on api2.cursor.sh yields the dynamic agent URL
(api5 host). The old AiService/StreamChat path is deprecated upstream and
now returns an "outdated version" error for every identity — replaced here.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from kiterouter.providers.base import BaseProvider, create_sse_chunk

AGENT_URL_CACHE_TTL = 3600.0
CLI_VERSIONS_DIR = Path.home() / ".local" / "share" / "cursor-agent" / "versions"
_VERSION_ID_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-[0-9a-f]+$")
PINNED_CLI_VERSION = "2026.07.17-3e2a980"


def detect_cli_version() -> str:
    """Newest cursor-agent CLI build on disk, else the pinned id."""
    try:
        if CLI_VERSIONS_DIR.is_dir():
            cands = [
                (p.stat().st_mtime, p.name)
                for p in CLI_VERSIONS_DIR.iterdir()
                if p.is_dir() and _VERSION_ID_RE.match(p.name)
            ]
            if cands:
                return max(cands, key=lambda t: (t[0], t[1]))[1]
    except Exception:
        pass
    return PINNED_CLI_VERSION


# ── protobuf wire helpers ────────────────────────────────────────────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(f: int, wt: int) -> bytes:
    return _varint((f << 3) | wt)


def _enc_str(f: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return _tag(f, 2) + _varint(len(b)) + b


def _enc_msg(f: int, inner: Any) -> bytes:
    buf = b"".join(inner) if isinstance(inner, list) else inner
    return _tag(f, 2) + _varint(len(buf)) + buf


def _enc_varint_field(f: int, v: int) -> bytes:
    return _tag(f, 0) + _varint(v)


def _wrap_connect(payload: bytes, compressed: bool = False) -> bytes:
    data = gzip.compress(payload) if compressed else payload
    return bytes([1 if compressed else 0]) + len(data).to_bytes(4, "big") + data


def build_connect_envelope(payload: Dict[str, Any] | bytes) -> bytes:
    """Public helper for building raw Connect-RPC envelopes (used in tests)."""
    raw = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
    return _wrap_connect(raw)


def _decode_fields(buf: bytes):
    """Iterate protobuf fields: yields (field_number, wire_type, value)."""
    i = 0
    while i < len(buf):
        key = 0
        shift = 0
        while True:
            b = buf[i]
            i += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        f, wt = key >> 3, key & 7
        if wt == 0:
            v = 0
            s = 0
            while True:
                b = buf[i]
                i += 1
                v |= (b & 0x7F) << s
                s += 7
                if not b & 0x80:
                    break
            yield f, wt, v
        elif wt == 2:
            ln = 0
            shift = 0
            while True:
                b = buf[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            yield f, wt, buf[i:i + ln]
            i += ln
        else:
            return


# Recorder field constants (proven against OmniRoute cursorAgentProtobuf.ts)
ACM_RUN_REQUEST = 1
ARR_CONVERSATION_STATE = 1
ARR_ACTION = 2
ARR_MODEL_DETAILS = 3
ARR_MCP_TOOLS = 4
ARR_CONVERSATION_ID = 5
ARR_REQUESTED_MODEL = 9
ARR_UNKNOWN_12 = 12
ARR_REQUEST_ID = 16
CA_USER_MESSAGE_ACTION = 1
UMA_USER_MESSAGE = 1
UM_TEXT = 1
UM_MESSAGE_ID = 2
UM_SELECTED_CONTEXT = 3
UM_MODE = 4
RM_MODEL_ID = 1
MD_MODEL_ID = 1
MD_DISPLAY_MODEL_ID = 3
MD_DISPLAY_NAME = 4
ASM_INTERACTION_UPDATE = 1
IU_TEXT_DELTA = 1
IU_THINKING_DELTA = 4
IU_THINKING_COMPLETED = 5
IU_TOOL_CALL_STARTED = 2
IU_TOOL_CALL_COMPLETED = 3
IU_TOKEN_DELTA = 8
IU_HEARTBEAT = 13
IU_TURN_ENDED = 14
TDU_TEXT = 1


def build_agent_run_body(user_text: str, model_id: str) -> bytes:
    """AgentClientMessage { run_request } for a text-only user turn."""
    conv = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    # UserMessage { text, message_id, selected_context(empty), mode=1 }
    um = _enc_msg(UMA_USER_MESSAGE, [
        _enc_str(UM_TEXT, user_text),
        _enc_str(UM_MESSAGE_ID, mid),
        _enc_msg(UM_SELECTED_CONTEXT, b""),
        _enc_varint_field(UM_MODE, 1),
    ])
    # ConversationAction { user_message_action }
    action = _enc_msg(ARR_ACTION, [_enc_msg(CA_USER_MESSAGE_ACTION, [um])])
    arr = [
        _enc_msg(ARR_CONVERSATION_STATE, b""),
        action,
        _enc_msg(ARR_MODEL_DETAILS, [
            _enc_str(MD_MODEL_ID, model_id),
            _enc_str(MD_DISPLAY_MODEL_ID, model_id),
            _enc_str(MD_DISPLAY_NAME, model_id),
        ]),
        _enc_msg(ARR_MCP_TOOLS, b""),  # empty placeholder required
        _enc_str(ARR_CONVERSATION_ID, conv),
        _enc_msg(ARR_REQUESTED_MODEL, [_enc_str(RM_MODEL_ID, model_id)]),
        _enc_varint_field(ARR_UNKNOWN_12, 0),
        _enc_str(ARR_REQUEST_ID, conv),
    ]
    return _wrap_connect(_enc_msg(ACM_RUN_REQUEST, arr))


def decode_agent_stream(data: bytes) -> List[Dict[str, Any]]:
    """Decode Connect-RPC frames into deltas: {text|thinking|turn_ended|error}."""
    out: List[Dict[str, Any]] = []
    pos = 0
    while pos + 5 <= len(data):
        flag = data[pos]
        ln = int.from_bytes(data[pos + 1:pos + 5], "big")
        payload = data[pos + 5:pos + 5 + ln]
        pos += 5 + ln
        if flag & 0x01:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        if flag & 0x02:  # end-of-stream/trailer
            txt = payload.decode("utf-8", "replace")
            if txt:
                try:
                    t = json.loads(txt)
                    if "error" in t:
                        out.append({"kind": "error", "message": _summarize_error(t)})
                except Exception:
                    pass
            continue
        for f, wt, v in _decode_fields(payload):
            if f != ASM_INTERACTION_UPDATE or wt != 2 or not isinstance(v, bytes):
                continue
            for f2, wt2, v2 in _decode_fields(v):
                if wt2 != 2 or not isinstance(v2, bytes):
                    if wt2 == 0 and f2 == IU_TURN_ENDED:
                        out.append({"kind": "turn_ended"})
                    elif wt2 == 0 and f2 == IU_THINKING_COMPLETED:
                        out.append({"kind": "thinking_complete"})
                    elif wt2 == 0 and f2 == IU_TOOL_CALL_STARTED:
                        out.append({"kind": "tool_call_started"})
                    elif wt2 == 0 and f2 == IU_TOOL_CALL_COMPLETED:
                        out.append({"kind": "tool_call_completed"})
                    continue
                if f2 in (IU_TEXT_DELTA, IU_THINKING_DELTA):
                    for f3, wt3, v3 in _decode_fields(v2):
                        if f3 == TDU_TEXT and wt3 == 2 and isinstance(v3, bytes):
                            out.append({
                                "kind": "text" if f2 == IU_TEXT_DELTA else "thinking",
                                "text": v3.decode("utf-8", "replace"),
                            })
                # IU_HEARTBEAT / IU_TOKEN_DELTA: silent
    return out


def _summarize_error(trailer: Dict[str, Any]) -> str:
    debug = ((trailer.get("error") or {}).get("details") or [{}])[0].get("debug", {})
    details = debug.get("details") or {}
    title = details.get("title") or debug.get("error", "Error")
    detail = details.get("detail") or ""
    return f"{title}: {detail}" if detail else str(title)


def strip_oauth_prefix(token: str) -> str:
    return token.split("::", 1)[1] if "::" in token else token


class CursorProvider(BaseProvider):
    name = "cursor"
    is_free = False
    # Static candidate list; real availability comes only from live completions.
    supported_models = [
        "auto",
        "composer-2.5",
        "claude-4.6-opus-max-thinking-fast",
        "claude-4.6-sonnet-medium",
        "gpt-5.3-codex-spark-preview",
        "gemini-3.1-pro",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.access_token = (
            self.config.get("token")
            or os.environ.get("CURSOR_TOKEN")
            or self._try_harvest_local_token()
        )
        self.machine_id = self.config.get("machine_id") or os.environ.get("CURSOR_MACHINE_ID")
        self.api_base = self.config.get("endpoint", "https://api2.cursor.sh")
        self._agent_url: Optional[str] = None
        self._agent_url_at = 0.0
        self.mock_mode = self.config.get("mock", False)

    def _try_harvest_local_token(self) -> Optional[str]:
        paths = [
            os.path.expanduser("~/.cursor/auth.json"),
            os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/storage.json"),
        ]
        for p in paths:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                        tok = data.get("accessToken") or data.get("token")
                        if tok:
                            return tok
                except Exception:
                    continue
        return None

    async def is_available(self) -> bool:
        return bool(self.access_token or self.mock_mode)

    # ── header + URL helpers ────────────────────────────────────────────────

    def _cli_version(self) -> str:
        return self.config.get("cli_version") or detect_cli_version()

    def _base_headers(self) -> Dict[str, str]:
        clean = strip_oauth_prefix(self.access_token or "")
        rid = str(uuid.uuid4())
        trace = f"00-{uuid.uuid4().hex}-01"
        return {
            "authorization": f"Bearer {clean}",
            "connect-accept-encoding": "gzip",
            "connect-protocol-version": "1",
            "user-agent": "connect-es/1.6.1",
            "x-cursor-client-type": "cli",
            "x-cursor-client-version": f"cli-{self._cli_version()}",
            "x-ghost-mode": "true",
            "x-original-request-id": rid,
            "x-request-id": rid,
            "traceparent": trace,
            "backend-traceparent": trace,
        }

    async def _resolve_agent_url(self, client: httpx.AsyncClient) -> str:
        if self._agent_url and time.time() - self._agent_url_at < AGENT_URL_CACHE_TTL:
            return self._agent_url
        headers = dict(self._base_headers())
        headers["content-type"] = "application/proto"
        resp = await client.post(
            f"{self.api_base}/aiserver.v1.ServerConfigService/GetServerConfig",
            headers=headers,
            content=b"",
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Cursor server-config HTTP {resp.status_code}")
        m = re.search(rb"https://[a-z0-9.\-]*api5\.cursor\.sh", resp.content)
        if not m:
            raise RuntimeError("Cursor server config did not include an Agent URL")
        self._agent_url = m.group(0).decode()
        self._agent_url_at = time.time()
        return self._agent_url

    # ── main streaming path ────────────────────────────────────────────────

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        if self.mock_mode:
            yield create_sse_chunk(f"[Cursor mock response for {model}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        if not self.access_token:
            yield create_sse_chunk("Cursor error: No token found. Set CURSOR_TOKEN.", model=model)
            yield "data: [DONE]\n\n"
            return

        # Agent Run carries one user turn; system content is honored by
        # prepending into the user text (this minimal encoder has no separate
        # system-role delivery path).
        parts: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):  # multimodal not in text-only path
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if role == "system":
                parts.append(str(content))
            elif role in ("user", "assistant"):
                parts.append(str(content))
        user_text = "\n\n".join(p for p in parts if p).strip() or "Hello"

        headers = self._base_headers()
        headers["content-type"] = "application/connect+proto"
        body = build_agent_run_body(user_text, model)

        try:
            async with httpx.AsyncClient(http2=True, timeout=120.0) as client:
                try:
                    agent_url = await self._resolve_agent_url(client)
                except Exception as e:
                    yield create_sse_chunk(f"Cursor upstream error: {e}", model=model)
                    yield "data: [DONE]\n\n"
                    return
                url = f"{agent_url}/agent.v1.AgentService/Run"
                resp = await client.post(url, headers=headers, content=body)
                if resp.status_code in (429, 403):
                    yield create_sse_chunk(
                        f"Cursor upstream HTTP {resp.status_code} (rate-limit/block — stopping to protect the account).",
                        model=model,
                    )
                    yield "data: [DONE]\n\n"
                    return
                if resp.status_code != 200:
                    yield create_sse_chunk(f"Cursor upstream HTTP {resp.status_code}", model=model)
                    yield "data: [DONE]\n\n"
                    return

                deltas = decode_agent_stream(resp.content)
                emitted = False
                for d in deltas:
                    if d.get("kind") == "text" and d.get("text"):
                        yield create_sse_chunk(d["text"], model=model)
                        emitted = True
                    elif d.get("kind") == "error":
                        yield create_sse_chunk(d["message"], model=model)
                if not emitted and not deltas:
                    yield create_sse_chunk(
                        "Cursor returned no output (model may be retired or not entitled — try 'auto' or fetch-models).",
                        model=model,
                    )
                yield create_sse_chunk(finish_reason="stop", model=model)
                yield "data: [DONE]\n\n"
        except Exception as e:
            yield create_sse_chunk(f"Cursor bridge notice: {e}", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
