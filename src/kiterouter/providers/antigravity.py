"""Antigravity (Google Cloud Code Assist / Gemini) provider with anti-ban safeguards."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
import httpx

from kiterouter.config import persist_provider_tokens
from kiterouter.providers.base import BaseProvider, create_sse_chunk

logger = logging.getLogger(__name__)

ANTIGRAVITY_IDE_VERSION = "2.11.0"
ANTIGRAVITY_CHAT_ACTION = "streamGenerateContent?alt=sse"
ANTIGRAVITY_IDE_BASE_URL = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_IDE_USER_AGENT = f"antigravity/ide/{ANTIGRAVITY_IDE_VERSION} darwin/arm64"
MAX_ANTIGRAVITY_OUTPUT_TOKENS = 64000

ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"


def discover_antigravity_oauth() -> Tuple[Optional[str], Optional[str]]:
    """Discover Google Cloud Code IDE OAuth client credentials from local environment or 9router."""
    cid = os.environ.get("ANTIGRAVITY_CLIENT_ID")
    sec = os.environ.get("ANTIGRAVITY_CLIENT_SECRET")
    if cid and sec:
        return cid, sec

    patterns = [
        os.path.expanduser("~/.local/lib/node_modules/9router/app/.next-cli-build/server/chunks/*.js"),
        "/usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/*.js",
    ]
    import glob
    for p in patterns:
        for fpath in glob.glob(p):
            try:
                with open(fpath, "r", errors="ignore") as f:
                    content = f.read()
                    if "oauth2.googleapis.com" in content and "apps.googleusercontent.com" in content:
                        m_id = re.search(r"(\d+-[a-z0-9]+\.apps\.googleusercontent\.com)", content)
                        m_sec = re.search(r"(GOCSPX-[A-Za-z0-9_-]{28})", content)
                        if m_id and m_sec:
                            return m_id.group(1), m_sec.group(1)
            except Exception:
                pass
    return None, None

# Competing-client branding that Antigravity flags with 429 Quota Exhausted.
# Mirrors 9router ANTIGRAVITY_PROMPT_REWRITES.
ANTIGRAVITY_PROMPT_REWRITES = [
    ("Zed", "Antigravity"),
    ("OpenCode", "Antigravity"),
    ("Kilo Code", "Antigravity"),
    ("kilo-code", "antigravity"),
    ("claude-code", "antigravity"),
]

# Fields Google Cloud Code Assist rejects at request root
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
    """
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "User-Agent": ANTIGRAVITY_IDE_USER_AGENT,
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1 vscode-antigravity/1.107.0",
        "Client-Metadata": json.dumps({
            "ideType": "ANTIGRAVITY",
            "platform": "DARWIN_ARM64",
            "pluginType": "GEMINI",
        }),
        "Accept": "text/event-stream",
    }
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


def convert_openai_to_gemini_request(
    messages: List[Dict[str, Any]],
    model: str,
    project_id: str,
    session_id: str,
    request_id: str,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Convert OpenAI-format messages to Cloud Code Assist v1internal generateContent payload.
    Google API requires wrapping contents inside {project, model, userAgent, request: {contents, generationConfig}}.
    """
    contents: List[Dict[str, Any]] = []
    system_parts: List[Dict[str, str]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            # Extract plain text from content part lists
            text_val = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text_val = str(content)

        if role == "system":
            cleaned = rewrite_competing_branding(text_val)
            system_parts.append({"text": cleaned})
        elif role in ("assistant", "model"):
            contents.append({"role": "model", "parts": [{"text": text_val}]})
        else:
            cleaned = rewrite_competing_branding(text_val)
            contents.append({"role": "user", "parts": [{"text": cleaned}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    safe_max_tokens = max_tokens or 4096
    if safe_max_tokens > MAX_ANTIGRAVITY_OUTPUT_TOKENS:
        safe_max_tokens = MAX_ANTIGRAVITY_OUTPUT_TOKENS

    req_body: Dict[str, Any] = {
        "sessionId": session_id,
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": safe_max_tokens,
        },
    }
    if system_parts:
        req_body["systemInstruction"] = {"parts": system_parts}

    return {
        "project": project_id or "reference-project",
        "model": model,
        "userAgent": "antigravity",
        "requestId": request_id,
        "requestType": "agent",
        "request": req_body,
    }


class AntigravityProvider(BaseProvider):
    name = "antigravity"
    is_free = False
    supported_models = [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.8-flash",
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
        "gemini-3-flash",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.auth_token = (
            self.config.get("token")
            or self.config.get("access_token")
            or self.config.get("accessToken")
            or os.environ.get("ANTIGRAVITY_TOKEN")
            or os.environ.get("GEMINI_API_KEY")
        )
        self.refresh_token = (
            self.config.get("refresh_token")
            or self.config.get("refreshToken")
            or os.environ.get("ANTIGRAVITY_REFRESH_TOKEN")
        )
        self.project_id = (
            self.config.get("project_id")
            or self.config.get("projectId")
            or os.environ.get("ANTIGRAVITY_PROJECT_ID", "reference-project")
        )
        self.machine_id = self.config.get("machine_id") or os.environ.get("ANTIGRAVITY_MACHINE_ID", "")
        self.chat_endpoint = self.config.get(
            "endpoint", f"{ANTIGRAVITY_IDE_BASE_URL}/v1internal:{ANTIGRAVITY_CHAT_ACTION}"
        )
        self.client_id = self.config.get("client_id") or os.environ.get("ANTIGRAVITY_CLIENT_ID")
        self.client_secret = self.config.get("client_secret") or os.environ.get("ANTIGRAVITY_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            disc_id, disc_sec = discover_antigravity_oauth()
            self.client_id = self.client_id or disc_id
            self.client_secret = self.client_secret or disc_sec

        self.mock_mode = self.config.get("mock", False)

    async def refresh_access_token(self) -> Optional[str]:
        """Auto-refresh Google OAuth access token using refresh_token."""
        if not self.refresh_token or not self.client_id or not self.client_secret:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    ANTIGRAVITY_TOKEN_URL,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                    },
                )
                if res.status_code == 200:
                    data = res.json()
                    new_token = data.get("access_token")
                    if new_token:
                        self.auth_token = new_token
                        persist_provider_tokens(
                            "antigravity",
                            {
                                "token": new_token,
                                "access_token": new_token,
                                "refresh_token": self.refresh_token,
                            },
                        )
                        logger.info("Antigravity OAuth token successfully refreshed")
                        return new_token
                else:
                    logger.warning(
                        "Antigravity token refresh failed: HTTP %s: %s",
                        res.status_code,
                        res.text[:200],
                    )
        except Exception as e:
            logger.warning("Antigravity token refresh exception: %s", e)
        return None

    async def is_available(self) -> bool:
        return bool(self.auth_token or self.refresh_token or self.mock_mode)

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

        if not self.auth_token and self.refresh_token:
            await self.refresh_access_token()

        if not self.auth_token:
            yield create_sse_chunk(
                "Antigravity error: Token missing and no refresh_token found. Re-authenticate Antigravity in 9router/OmniRoute.",
                model=model,
            )
            yield "data: [DONE]\n\n"
            return

        session_id = kwargs.get("session_id") or f"ses_{uuid.uuid4().hex[:16]}"
        request_id = build_ide_request_id(session_id=session_id, model=model, step=kwargs.get("step", 1))

        payload = convert_openai_to_gemini_request(
            messages=messages,
            model=model,
            project_id=self.project_id,
            session_id=session_id,
            request_id=request_id,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        headers = build_antigravity_headers(
            self.auth_token,
            machine_id=self.machine_id or None,
            session_id=session_id,
            project_id=self.project_id or None,
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(self.chat_endpoint, headers=headers, json=payload)

                # If token expired (401), attempt auto-refresh once
                if resp.status_code == 401 and self.refresh_token:
                    new_token = await self.refresh_access_token()
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        resp = await client.post(self.chat_endpoint, headers=headers, json=payload)

                if resp.status_code == 401:
                    yield create_sse_chunk(
                        "Antigravity upstream HTTP 401: Google OAuth session expired or revoked. Please re-authenticate Antigravity in 9router/OmniRoute.",
                        model=model,
                    )
                    yield "data: [DONE]\n\n"
                    return

                if resp.status_code == 429:
                    err_msg = "Resource has been exhausted (e.g. check quota)."
                    try:
                        err_data = resp.json()
                        err_msg = err_data.get("error", {}).get("message", err_msg)
                    except Exception:
                        pass
                    yield create_sse_chunk(
                        f"Antigravity rate limit (429): {err_msg}",
                        model=model,
                    )
                    yield "data: [DONE]\n\n"
                    return

                if resp.status_code != 200:
                    err_body = resp.text[:200]
                    yield create_sse_chunk(f"Antigravity HTTP {resp.status_code}: {err_body}", model=model)
                    yield "data: [DONE]\n\n"
                    return

                # Stream Google SSE and translate to standard OpenAI chunks
                for line in resp.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        raw_data = line[5:].strip()
                        if raw_data == "[DONE]":
                            break
                        try:
                            parsed = json.loads(raw_data)
                            candidates = parsed.get("candidates", [])
                            if candidates:
                                content = candidates[0].get("content", {})
                                parts = content.get("parts", [])
                                for part in parts:
                                    text_delta = part.get("text")
                                    if text_delta:
                                        yield create_sse_chunk(text_delta, model=model)
                        except Exception:
                            continue

                yield create_sse_chunk(finish_reason="stop", model=model)
                yield "data: [DONE]\n\n"

        except Exception as e:
            yield create_sse_chunk(f"[Antigravity notice: {e}]", model=model)
            yield create_sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
