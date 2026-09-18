"""Tests for durable request history (Phase A, Wave A1)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from kiterouter import server
from kiterouter.server import app
from kiterouter.store import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "kiterouter.db")
    yield s
    s.close()


# ── store: requests ──────────────────────────────────────────────────────────

def test_record_and_read_a_request(store):
    row_id = store.record_request(
        provider="copilot",
        model="gh/gpt-4o",
        status="ok",
        latency_ms=1831,
        ttft_ms=1690,
        tokens_in=7,
        tokens_out=3,
        rtk_saved=12,
        prompt_preview="hi",
        response_preview="OK",
    )
    assert row_id > 0

    row = store.request_by_id(row_id)
    assert row["provider"] == "copilot"
    assert row["ttft_ms"] == 1690, "TTFT must survive the round trip"
    assert row["tokens_in"] == 7
    assert row["rtk_saved"] == 12


def test_history_is_newest_first(store):
    store.record_request(provider="a", model="m", status="ok")
    store.record_request(provider="b", model="m", status="ok")
    assert [r["provider"] for r in store.recent_requests()] == ["b", "a"]


def test_filters(store):
    store.record_request(provider="a", model="m1", status="ok")
    store.record_request(provider="b", model="m2", status="error")
    store.record_request(provider="a", model="m2", status="error")

    assert len(store.recent_requests(provider="a")) == 2
    assert len(store.recent_requests(model="m2")) == 2
    assert len(store.recent_requests(status="error")) == 2
    assert len(store.recent_requests(provider="a", status="error")) == 1


def test_since_window(store):
    now = int(time.time())
    store.record_request(provider="a", model="m", status="ok", at=now - 10 * 86400)
    store.record_request(provider="a", model="m", status="ok", at=now - 60)

    assert len(store.recent_requests()) == 2
    assert len(store.recent_requests(since=now - 86400)) == 1


def test_before_id_pages_backwards_without_drift(store):
    for i in range(10):
        store.record_request(provider=f"p{i}", model="m", status="ok")

    first = store.recent_requests(limit=4)
    second = store.recent_requests(limit=4, before_id=first[-1]["id"])

    assert len(first) == 4 and len(second) == 4
    assert not ({r["id"] for r in first} & {r["id"] for r in second}), "pages must not overlap"


def test_request_stats(store):
    store.record_request(provider="a", model="m", status="ok", latency_ms=100, ttft_ms=40, tokens_in=10)
    store.record_request(provider="a", model="m", status="ok", latency_ms=300, ttft_ms=60, tokens_out=5)
    store.record_request(provider="a", model="m", status="error", latency_ms=90)

    stats = store.request_stats()
    assert stats["total"] == 3
    assert stats["ok"] == 2
    assert stats["failed"] == 1
    assert stats["ok_rate_pct"] == 66.7
    assert stats["avg_latency_ms"] == 163
    assert stats["avg_ttft_ms"] == 50, "rows without a ttft must not drag the average"
    assert stats["tokens_in"] == 10


def test_stats_on_an_empty_store_do_not_divide_by_zero(store):
    stats = store.request_stats()
    assert stats["total"] == 0
    assert stats["ok_rate_pct"] == 0.0


def test_prune_removes_rows_past_retention(store):
    now = int(time.time())
    store.record_request(provider="a", model="m", status="ok", at=now - 40 * 86400)
    store.record_request(provider="a", model="m", status="ok", at=now - 3600)

    assert store.prune({"requests": 30}, now=now) == 1
    assert store.count_requests() == 1


def test_stats_reports_every_table(store):
    store.record_request(provider="a", model="m", status="ok")
    store.record_health("a", "c1", ok=True)
    store.record_models("a", ["m1"])

    stats = store.stats()
    assert stats["requests"] == 1
    assert stats["health_checks"] == 1
    assert stats["models"] == 1
    assert stats["schema_version"] == SCHEMA_VERSION


# ── store: body artifacts ────────────────────────────────────────────────────

def test_bodies_expire_on_their_own_shorter_window(store, tmp_path):
    now = int(time.time())
    body = tmp_path / "old.json"
    body.write_text("{}")

    store.record_request(
        provider="a", model="m", status="ok", at=now - 10 * 86400, body_path=str(body)
    )
    fresh_body = tmp_path / "fresh.json"
    fresh_body.write_text("{}")
    store.record_request(provider="a", model="m", status="ok", at=now, body_path=str(fresh_body))

    removed = store.prune_bodies(3, now=now)

    assert removed == 1
    assert not body.exists(), "the expired body file should be gone"
    assert fresh_body.exists()
    # The row outlives its body; only the reference is cleared.
    assert store.count_requests() == 2
    assert store.recent_requests()[0]["body_path"] is not None
    assert store.recent_requests()[1]["body_path"] is None


def test_pruning_a_row_takes_its_body_file(store, tmp_path):
    body = tmp_path / "b.json"
    body.write_text("{}")
    now = int(time.time())
    store.record_request(
        provider="a", model="m", status="ok", at=now - 40 * 86400, body_path=str(body)
    )

    store.prune({"requests": 30}, now=now)

    assert not body.exists(), "no orphaned body files"


def test_body_retention_can_be_disabled(store, tmp_path):
    body = tmp_path / "b.json"
    body.write_text("{}")
    store.record_request(provider="a", model="m", status="ok", at=1, body_path=str(body))
    assert store.prune_bodies(0) == 0
    assert body.exists()


def test_maintain_expires_bodies_while_keeping_the_row(store, tmp_path):
    """Bodies expire on their own shorter window, so the row outlives them."""
    now = int(time.time())
    body = tmp_path / "b.json"
    body.write_text("{}")
    # Row is 10 days old: inside the 30-day row retention, past the 3-day body one.
    store.record_request(provider="a", model="m", status="ok", at=now - 10 * 86400, body_path=str(body))

    result = store.maintain({"requests": 30}, now=now, body_retention_days=3)

    assert result["bodies_removed"] == 1
    assert result["pruned"] == 0, "the row itself is still within retention"
    assert not body.exists()
    assert store.count_requests() == 1


def test_maintain_prunes_old_rows_and_their_bodies(store, tmp_path):
    now = int(time.time())
    body = tmp_path / "old.json"
    body.write_text("{}")
    store.record_request(provider="a", model="m", status="ok", at=now - 40 * 86400, body_path=str(body))

    result = store.maintain({"requests": 30}, now=now, body_retention_days=3)

    assert result["pruned"] == 1
    assert not body.exists(), "no orphaned file when the row goes"


# ── server: persistence, bodies, session keys ────────────────────────────────

def test_record_request_log_persists_to_the_store():
    entry = server.record_request_log(
        model="copilot/gh/gpt-4o",
        provider="copilot",
        tokens_in=7,
        tokens_out=3,
        status="ok",
        latency_ms=1831,
        ttft_ms=1690,
    )
    row = server.store.request_by_id(entry["store_id"])
    assert row["provider"] == "copilot"
    assert row["ttft_ms"] == 1690


def test_body_file_is_written_and_readable(tmp_path):
    path = server.write_request_body({"request": {"model": "m"}, "response": {"text": "hi"}})
    assert path and Path(path).exists()
    assert json.loads(Path(path).read_text())["response"]["text"] == "hi"


def test_bodies_can_be_disabled(monkeypatch):
    monkeypatch.setattr(server.config, "save_bodies", False)
    assert server.write_request_body({"request": {}}) is None


def test_a_body_larger_than_the_cap_is_truncated(monkeypatch):
    monkeypatch.setattr(server.config, "max_body_bytes", 200)
    path = server.write_request_body({"request": {"blob": "x" * 5000}})
    assert path
    content = Path(path).read_text()
    assert len(content) <= 260
    assert "truncated by KiteRouter" in content


def test_session_key_is_stable_for_the_same_opening_turn():
    a = server.derive_session_key([{"role": "user", "content": "fix the bug"}])
    b = server.derive_session_key(
        [{"role": "user", "content": "fix the bug"}, {"role": "assistant", "content": "ok"}]
    )
    assert a == b, "follow-up turns must group with their opening turn"


def test_session_key_differs_between_conversations():
    a = server.derive_session_key([{"role": "user", "content": "one"}])
    b = server.derive_session_key([{"role": "user", "content": "two"}])
    assert a != b


def test_session_key_is_absent_without_a_user_turn():
    assert server.derive_session_key([{"role": "system", "content": "x"}]) is None
    assert server.derive_session_key([]) is None


# ── endpoints ────────────────────────────────────────────────────────────────

def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_logs_endpoint_filters_and_stats():
    server.record_request_log(
        model="copilot/gh/gpt-4o", provider="copilot", tokens_in=7, tokens_out=3,
        status="ok", latency_ms=1800, ttft_ms=1600,
    )
    server.record_request_log(
        model="glm/glm-5.1", provider="glm", tokens_in=5, tokens_out=0,
        status="error", latency_ms=90, error="HTTP 401",
    )

    async with client() as c:
        all_logs = (await c.get("/api/logs?limit=50")).json()
        by_provider = (await c.get("/api/logs?provider=copilot")).json()
        by_status = (await c.get("/api/logs?status=error")).json()

    assert all_logs["stats"]["total"] >= 2
    assert all(r["provider"] != "glm" for r in by_provider["requests"])
    assert all(r["status"] == "error" for r in by_status["requests"])


@pytest.mark.asyncio
async def test_log_detail_returns_the_body():
    entry = server.record_request_log(
        model="copilot/gh/gpt-4o", provider="copilot", tokens_in=1, tokens_out=1,
        status="ok", latency_ms=10, body={"request": {"model": "x"}, "response": {"text": "hi"}},
    )

    async with client() as c:
        resp = await c.get(f"/api/logs/{entry['store_id']}")

    body = resp.json()
    assert body["request"]["provider"] == "copilot"
    assert body["body"]["response"]["text"] == "hi"
    assert body["body_expired"] is False


@pytest.mark.asyncio
async def test_log_detail_reports_an_expired_body_without_failing():
    entry = server.record_request_log(
        model="m", provider="p", tokens_in=1, tokens_out=1, status="ok", latency_ms=1
    )
    row = server.store.request_by_id(entry["store_id"])
    server.store._conn.execute(
        "UPDATE requests SET body_path = '/nonexistent/expired.json' WHERE id = ?",
        (row["id"],),
    )
    server.store._conn.commit()

    async with client() as c:
        resp = await c.get(f"/api/logs/{row['id']}")

    assert resp.status_code == 200
    assert resp.json()["body"] is None
    assert resp.json()["body_expired"] is True


@pytest.mark.asyncio
async def test_unknown_log_id_is_a_404():
    async with client() as c:
        assert (await c.get("/api/logs/99999999")).status_code == 404


@pytest.mark.asyncio
async def test_live_ttft_is_recorded_on_a_non_streaming_request(monkeypatch):
    """TTFT must come from the request path, not only from the prober."""
    async def fake_stream(model, messages, temperature=0.7, max_tokens=None, **kwargs):
        await asyncio.sleep(0.05)
        yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(server.router, "stream_with_fallback", fake_stream)

    async with client() as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "copilot/gh/gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        )

    assert resp.status_code == 200
    row = server.store.recent_requests(limit=1)[0]
    assert row["ttft_ms"] is not None, "a live request should record time-to-first-token"
    assert row["ttft_ms"] >= 40
    assert row["latency_ms"] >= row["ttft_ms"]
    assert row["session_key"], "the request should be grouped into a conversation"
