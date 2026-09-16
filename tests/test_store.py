"""Tests for the local SQLite store."""
from __future__ import annotations

import sqlite3
import time

import pytest

from kiterouter.store import MIGRATIONS, SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "kiterouter.db")
    yield s
    s.close()


# ── schema and migrations ────────────────────────────────────────────────────

def test_migrations_apply_and_record_the_version(store):
    assert store.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION == max(v for v, _ in MIGRATIONS)


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "k.db"
    first = Store(path)
    first.record_health("cursor", "abc", ok=True)
    first.close()

    # Re-opening must not re-run migrations or lose data.
    again = Store(path)
    assert again.schema_version == SCHEMA_VERSION
    assert len(again.health_history()) == 1
    again.close()


def test_wal_mode_is_enabled(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", "an unmanaged rollback journal would block readers"


def test_parent_directory_is_created(tmp_path):
    nested = tmp_path / "a" / "b" / "k.db"
    s = Store(nested)
    assert nested.exists()
    s.close()


# ── health rows ──────────────────────────────────────────────────────────────

def test_record_and_read_back(store):
    store.record_health(
        "antigravity", "acct1", ok=True, model="gemini-3.8", latency_ms=12274, ttft_ms=9132
    )
    rows = store.health_history()
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "antigravity"
    assert row["connection"] == "acct1"
    assert row["ok"] == 1
    assert row["latency_ms"] == 12274
    assert row["ttft_ms"] == 9132


def test_failure_rows_keep_the_error_and_cap_it(store):
    store.record_health("glm", "acct1", ok=False, error="x" * 2000)
    row = store.health_history()[0]
    assert row["ok"] == 0
    assert len(row["error"]) == 500


def test_a_success_does_not_erase_the_previous_failure(store):
    store.record_health("cline", "c1", ok=False, error="401")
    store.record_health("cline", "c1", ok=True)
    assert len(store.health_history()) == 2, "history is append-only, not a status field"


def test_latest_health_returns_the_newest_row_per_connection(store):
    store.record_health("cursor", "a", ok=True, latency_ms=10, at=1000)
    store.record_health("cursor", "a", ok=False, latency_ms=20, at=2000)
    store.record_health("cursor", "b", ok=True, latency_ms=30, at=1500)

    latest = {(r["provider"], r["connection"]): r for r in store.latest_health()}
    assert set(latest) == {("cursor", "a"), ("cursor", "b")}
    assert latest[("cursor", "a")]["at"] == 2000
    assert latest[("cursor", "a")]["ok"] == 0
    assert latest[("cursor", "b")]["latency_ms"] == 30


def test_connections_are_tracked_separately(store):
    """The whole point: a second account must not inherit the first's failures."""
    store.record_health("antigravity", "good-account", ok=True, latency_ms=100)
    store.record_health("antigravity", "bad-account", ok=False, error="403")

    stats = {(r["provider"], r["connection"]): r for r in store.connection_stats()}
    assert stats[("antigravity", "good-account")]["ok_count"] == 1
    assert stats[("antigravity", "bad-account")]["ok_count"] == 0


def test_connection_stats_aggregate_latency_and_ttft(store):
    for latency, ttft in ((100, 20), (200, 40), (300, 60)):
        store.record_health("codex", "c1", ok=True, latency_ms=latency, ttft_ms=ttft)
    store.record_health("codex", "c1", ok=False, latency_ms=900, ttft_ms=None, error="boom")

    row = store.connection_stats()[0]
    assert row["checks"] == 4
    assert row["ok_count"] == 3
    assert row["min_latency_ms"] == 100
    assert row["max_latency_ms"] == 900
    assert row["avg_ttft_ms"] == 40, "rows without a ttft must not drag the average down"


def test_connection_stats_can_be_windowed(store):
    now = int(time.time())
    store.record_health("groq", "c1", ok=True, at=now - 10 * 86400)
    store.record_health("groq", "c1", ok=True, at=now - 3600)

    assert store.connection_stats()[0]["checks"] == 2
    assert store.connection_stats(since=now - 86400)[0]["checks"] == 1


# ── maintenance ──────────────────────────────────────────────────────────────

def test_prune_respects_the_cutoff_and_reports_what_it_removed(store):
    now = int(time.time())
    store.record_health("cursor", "c1", ok=True, at=now - 40 * 86400)  # older than 30d
    store.record_health("cursor", "c1", ok=True, at=now - 1 * 86400)   # kept

    removed = store.prune({"health_checks": 30}, now=now)

    assert removed == 1
    assert len(store.health_history()) == 1


def test_prune_ignores_tables_with_no_retention(store):
    store.record_health("cursor", "c1", ok=True, at=1)
    assert store.prune({"health_checks": 0}) == 0
    assert len(store.health_history()) == 1


def test_maintain_prunes_and_checkpoints(store):
    now = int(time.time())
    store.record_health("cursor", "c1", ok=True, at=now - 40 * 86400)

    result = store.maintain({"health_checks": 30}, now=now)

    assert result["pruned"] == 1
    assert result["checkpointed"] is True
    assert result["vacuumed"] is True, "a first run has never vacuumed"


def test_vacuum_only_runs_when_due(store):
    now = int(time.time())
    first = store.maintain({"health_checks": 30}, vacuum_interval_days=7, now=now)
    assert first["vacuumed"] is True
    assert store.last_vacuum() == now

    second = store.maintain({"health_checks": 30}, vacuum_interval_days=7, now=now + 60)
    assert second["vacuumed"] is False, "vacuum must not run on every maintenance pass"

    third = store.maintain({"health_checks": 30}, vacuum_interval_days=7, now=now + 8 * 86400)
    assert third["vacuumed"] is True


def test_vacuum_can_be_disabled(store):
    result = store.maintain({"health_checks": 30}, vacuum_interval_days=0)
    assert result["vacuumed"] is False
    assert store.last_vacuum() == 0.0


def test_close_truncates_the_wal(tmp_path):
    path = tmp_path / "k.db"
    s = Store(path)
    for _ in range(50):
        s.record_health("cursor", "c1", ok=True, latency_ms=10)
    s.close()

    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0, "closing should truncate the WAL"


def test_stats_report_sizes_and_rows(store):
    store.record_health("cursor", "c1", ok=True)
    stats = store.stats()

    assert stats["schema_version"] == SCHEMA_VERSION
    assert stats["health_checks"] == 1
    assert stats["size_bytes"] > 0
    assert "wal_bytes" in stats


def test_kv_round_trip(store):
    assert store.get_kv("missing") is None
    store.set_kv("last_vacuum", "123")
    assert store.get_kv("last_vacuum") == "123"
    store.set_kv("last_vacuum", "456")
    assert store.get_kv("last_vacuum") == "456", "set_kv should upsert, not fail on conflict"


def test_last_vacuum_tolerates_junk(store):
    store.set_kv("last_vacuum", "not-a-number")
    assert store.last_vacuum() == 0.0
