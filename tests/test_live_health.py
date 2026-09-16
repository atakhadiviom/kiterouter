"""Tests for passive provider health derived from real traffic."""
from __future__ import annotations

import json
import time

import pytest

from kiterouter.live_health import MAX_ERROR_CHARS, LiveHealth


@pytest.fixture
def health(tmp_path):
    return LiveHealth(tmp_path / "live_health.json", save_interval=0)


def test_ok_result_is_recorded_without_an_error(health):
    health.record("cursor", "ok", model="composer-2.5", latency_ms=812)
    entry = health.get("cursor")
    assert entry["status"] == "ok"
    assert entry["model"] == "composer-2.5"
    assert entry["latency_ms"] == 812
    assert "error" not in entry


def test_any_non_ok_status_is_an_error(health):
    health.record("glm", "error", model="glm-5.1", error="GLM upstream HTTP 401")
    assert health.get("glm")["status"] == "error"
    assert health.get("glm")["error"] == "GLM upstream HTTP 401"


def test_error_text_is_capped(health):
    health.record("groq", "error", error="x" * 5000)
    assert len(health.get("groq")["error"]) == MAX_ERROR_CHARS


def test_a_missing_error_still_records_something_useful(health):
    health.record("kiro", "error")
    assert health.get("kiro")["error"] == "Request failed"


def test_empty_provider_is_ignored(health):
    health.record("", "ok")
    health.record(None, "ok")
    assert health.snapshot() == {}


def test_latest_result_wins(health):
    health.record("cline", "error", error="401")
    health.record("cline", "ok", latency_ms=100)
    entry = health.get("cline")
    assert entry["status"] == "ok"
    assert "error" not in entry, "a recovery must clear the previous error"


def test_snapshot_is_a_copy(health):
    health.record("cursor", "ok")
    snap = health.snapshot()
    snap["cursor"]["status"] = "tampered"
    assert health.get("cursor")["status"] == "ok"


def test_writes_are_throttled(tmp_path):
    throttled = LiveHealth(tmp_path / "h.json", save_interval=3600)
    throttled.record("cursor", "ok")  # first write goes through
    assert throttled.path.exists()
    first = throttled.path.read_text()

    throttled.record("glm", "error", error="boom")
    assert throttled.flush() is False, "flush should be throttled"
    assert throttled.path.read_text() == first, "file was rewritten inside the interval"


def test_force_flush_ignores_the_throttle(tmp_path):
    throttled = LiveHealth(tmp_path / "h.json", save_interval=3600)
    throttled.record("cursor", "ok")
    throttled.record("glm", "error", error="boom")
    assert throttled.flush(force=True) is True
    saved = json.loads(throttled.path.read_text())
    assert set(saved) == {"cursor", "glm"}


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "h.json"
    first = LiveHealth(path, save_interval=0)
    first.record("antigravity", "error", error="403")
    first.record("copilot", "ok", latency_ms=250)
    first.flush(force=True)

    restarted = LiveHealth(path, save_interval=0)
    assert restarted.get("antigravity")["status"] == "error"
    assert restarted.get("copilot")["latency_ms"] == 250


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("{ this is not json")
    assert LiveHealth(path).snapshot() == {}


def test_entries_without_a_status_are_dropped_on_load(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"cursor": {"status": "ok"}, "junk": {"nope": 1}}))
    assert set(LiveHealth(path).snapshot()) == {"cursor"}


def test_timestamp_is_recorded(health):
    before = int(time.time())
    health.record("cursor", "ok")
    assert health.get("cursor")["at"] >= before


# ── integration with request logging ─────────────────────────────────────────

def test_logged_requests_update_provider_health():
    from kiterouter import server

    server.record_request_log(
        model="copilot/gh/gpt-4o",
        provider="copilot",
        tokens_in=12,
        tokens_out=4,
        status="ok",
        latency_ms=430,
    )
    entry = server.live_health.get("copilot")
    assert entry["status"] == "ok"
    assert entry["latency_ms"] == 430
    assert entry["model"] == "copilot/gh/gpt-4o"


def test_a_failing_request_marks_the_provider_failed():
    from kiterouter import server

    server.record_request_log(
        model="cline/whatever",
        provider="cline",
        tokens_in=5,
        tokens_out=0,
        status="error",
        latency_ms=90,
        error="Cline re-authentication required",
    )
    entry = server.live_health.get("cline")
    assert entry["status"] == "error"
    assert entry["error"] == "Cline re-authentication required"


@pytest.mark.asyncio
async def test_health_endpoint_exposes_provider_health():
    from httpx import ASGITransport, AsyncClient

    from kiterouter import server
    from kiterouter.server import app

    server.record_request_log(
        model="cursor/auto",
        provider="cursor",
        tokens_in=3,
        tokens_out=2,
        status="ok",
        latency_ms=610,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    payload = resp.json()
    assert "provider_health" in payload
    assert payload["provider_health"]["cursor"]["status"] == "ok"
