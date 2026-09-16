import pytest
from kiterouter.providers.cursor import CursorProvider
from kiterouter.providers.antigravity import (
    sanitize_antigravity_payload,
    build_ide_request_id,
    build_antigravity_headers,
)


def test_cursor_cli_headers():
    """The Agent-Run path uses CLI impersonation (checksums are not required there)."""
    p = CursorProvider({"mock": False})
    headers = p._base_headers()
    assert headers["x-cursor-client-type"] == "cli"
    assert headers["x-cursor-client-version"].startswith("cli-")
    assert headers["x-ghost-mode"] == "true"
    assert "user-agent" in headers
    assert headers["authorization"].startswith("Bearer ")
    # one-stable-identity rule: no per-request machine rotation in the wire
    assert headers["authorization"] == p._base_headers()["authorization"]


def test_antigravity_payload_sanitization():
    raw = {
        "model": "gemini-2.0-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"budget": 1000},
        "reasoning_effort": "high",
        "output_config": {"max_tokens": 100},
        "temperature": 0.7,
    }
    cleaned = sanitize_antigravity_payload(raw)
    assert "thinking" not in cleaned
    assert "reasoning_effort" not in cleaned
    assert "output_config" not in cleaned
    assert cleaned["model"] == "gemini-2.0-flash"
    assert cleaned["temperature"] == 0.7


def test_antigravity_ide_request_id():
    req_id = build_ide_request_id(session_id="session-42", model="gemini-2.0-flash", step=3)
    assert req_id.startswith("agent/")
    assert req_id.endswith("/3")
    parts = req_id.split("/")
    assert len(parts) == 5


def test_antigravity_anti_ban_headers():
    headers = build_antigravity_headers("google_oauth_token")
    assert headers["Authorization"] == "Bearer google_oauth_token"
    assert "antigravity/ide" in headers["User-Agent"]
    assert "vscode_cloudshelleditor" in headers["X-Goog-Api-Client"]
    assert "ANTIGRAVITY" in headers["Client-Metadata"]
