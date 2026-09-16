"""Tests for the Cursor Agent-Run provider rewrite."""
import gzip

from kiterouter.providers.cursor import (
    CursorProvider,
    build_agent_run_body,
    decode_agent_stream,
    detect_cli_version,
)


def test_build_agent_run_body_is_connect_frame():
    body = build_agent_run_body("hello", "composer-2.5")
    assert body[0] == 0  # uncompressed connect frame
    assert int.from_bytes(body[1:5], "big") == len(body) - 5


def test_decode_agent_stream_turn_ended_and_text():
    # IU_TURN_ENDED (field 14) as varint, and a text delta nested 1>1>1
    iu = bytes([0x78])  # field 15 wt 0? build explicitly:
    # InteractionUpdate: field 14 varint 0 => tag 0x70
    frame_inner = bytes([0x70, 0x00]) + (
        bytes([0x0A]) + bytes([len(b := b"\x0A\x05hello")]) + b  # f1 msg{ f1 str "hello" }
    )
    payload = b"\n" + bytes([len(frame_inner)]) + frame_inner  # ASM f1 wrapped
    data = b"\x00" + len(payload).to_bytes(4, "big") + payload
    deltas = decode_agent_stream(data)
    kinds = [d["kind"] for d in deltas]
    assert "turn_ended" in kinds
    assert any(d.get("kind") == "text" and d.get("text") == "hello" for d in deltas)


def test_decode_agent_stream_decodes_gzip_and_error_trailer():
    import json
    payload = gzip.compress(json.dumps({"error": {"details": [{"debug": {"error": "ERROR_RATE_LIMITED", "details": {"title": "Out of usage"}}}]}}).encode())
    data = bytes([3]) + len(payload).to_bytes(4, "big") + payload
    deltas = decode_agent_stream(data)
    assert deltas and deltas[0]["kind"] == "error"
    assert "Out of usage" in deltas[0]["message"]


def test_detect_cli_version_returns_dated_id():
    v = detect_cli_version()
    assert v and any(c.isdigit() for c in v)


def test_provider_missing_token_yields_error_message():
    p = CursorProvider({"mock": False})

    async def run():
        chunks = []
        async for c in p.stream_chat("auto", [{"role": "user", "content": "hi"}]):
            chunks.append(c)
        return chunks

    import asyncio
    chunks = asyncio.run(run())
    assert any("No token found" in c for c in chunks)
