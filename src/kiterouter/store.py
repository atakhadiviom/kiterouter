"""Local SQLite store for health checks, and later request history and usage.

One file at ``~/.kiterouter/kiterouter.db`` via the stdlib ``sqlite3`` module —
no dependency and no build step.

Why the WAL is managed rather than left alone: the OmniRoute installation this
gateway coexists with is carrying a 185 MB WAL and 1.6 GB of unpruned artifacts.
A database that only ever grows is a slow-motion outage, so writes are followed
by a scheduled checkpoint, a weekly VACUUM, and age-based pruning.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("kiterouter.store")

SCHEMA_VERSION = 1

# (version, [statements]) — applied in order, once each, tracked by user_version.
MIGRATIONS: Sequence[Tuple[int, Sequence[str]]] = (
    (
        SCHEMA_VERSION,
        (
            """
            CREATE TABLE IF NOT EXISTS health_checks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                at          INTEGER NOT NULL,
                provider    TEXT    NOT NULL,
                connection  TEXT    NOT NULL,
                model       TEXT,
                ok          INTEGER NOT NULL,
                latency_ms  INTEGER,
                ttft_ms     INTEGER,
                error       TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_health_at ON health_checks(at)",
            "CREATE INDEX IF NOT EXISTS idx_health_conn ON health_checks(provider, connection, at)",
            """
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """,
        ),
    ),
)


class Store:
    """Thread-safe wrapper around the local database."""

    def __init__(self, path: Path, checkpoint_on_close: bool = True):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._checkpoint_on_close = checkpoint_on_close
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The prober writes from the event loop while endpoints may read from a
        # worker thread, so the connection opts out of sqlite's thread check and
        # every statement goes through the lock instead.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    # ------------------------------------------------------------- lifecycle

    def _configure(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=10000")
            self._conn.commit()

    def migrate(self) -> int:
        """Apply pending migrations; returns the resulting schema version."""
        with self._lock:
            cur = self._conn.cursor()
            current = int(cur.execute("PRAGMA user_version").fetchone()[0] or 0)
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    cur.execute(statement)
                cur.execute(f"PRAGMA user_version={version}")
                logger.info("Applied store migration %s", version)
                current = version
            self._conn.commit()
        return current

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)

    def close(self) -> None:
        if self._checkpoint_on_close:
            self.checkpoint()
        with self._lock:
            self._conn.close()

    def reopen(self, path: Path) -> None:
        """Point the store at a different file.

        Changing ``path`` alone is not enough — the sqlite connection is already
        open against the old file, so writes would keep landing there. Used by
        tests to keep a run off the developer's real database.
        """
        try:
            self._conn.close()
        except Exception:
            pass
        self.__init__(Path(path))

    # ---------------------------------------------------------------- health

    def record_health(
        self,
        provider: str,
        connection: str,
        ok: bool,
        model: Optional[str] = None,
        latency_ms: Optional[int] = None,
        ttft_ms: Optional[int] = None,
        error: Optional[str] = None,
        at: Optional[int] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO health_checks
                   (at, provider, connection, model, ok, latency_ms, ttft_ms, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(at if at is not None else time.time()),
                    provider,
                    connection,
                    model,
                    1 if ok else 0,
                    int(latency_ms) if latency_ms is not None else None,
                    int(ttft_ms) if ttft_ms is not None else None,
                    (str(error)[:500] if error else None),
                ),
            )
            self._conn.commit()

    def latest_health(self) -> List[Dict[str, Any]]:
        """Most recent probe per provider+connection."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT h.* FROM health_checks h
                JOIN (
                    SELECT provider, connection, MAX(at) AS at
                    FROM health_checks
                    GROUP BY provider, connection
                ) m ON h.provider = m.provider
                   AND h.connection = m.connection
                   AND h.at = m.at
                ORDER BY h.provider, h.connection
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def health_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM health_checks ORDER BY at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def connection_stats(self, since: Optional[int] = None) -> List[Dict[str, Any]]:
        """Success rate and latency spread per provider+connection."""
        clause, params = ("WHERE at >= ?", (int(since),)) if since is not None else ("", ())
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT provider, connection,
                       COUNT(*)                        AS checks,
                       SUM(ok)                         AS ok_count,
                       MAX(at)                         AS last_at,
                       CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms,
                       CAST(AVG(ttft_ms) AS INTEGER)    AS avg_ttft_ms,
                       MIN(latency_ms)                 AS min_latency_ms,
                       MAX(latency_ms)                 AS max_latency_ms
                FROM health_checks {clause}
                GROUP BY provider, connection
                ORDER BY provider, connection
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- maintenance

    def prune(self, retention_days: Dict[str, int], now: Optional[int] = None) -> int:
        """Delete rows past their retention window; returns the row count removed."""
        moment = int(now if now is not None else time.time())
        removed = 0
        for table, days in retention_days.items():
            if not days:
                continue
            cutoff = moment - int(days) * 86400
            with self._lock:
                cur = self._conn.execute(f"DELETE FROM {table} WHERE at < ?", (cutoff,))
                removed += max(0, cur.rowcount or 0)
                self._conn.commit()
        return removed

    def checkpoint(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
            except Exception as e:
                logger.debug("checkpoint failed: %s", e)

    def vacuum(self) -> None:
        with self._lock:
            try:
                self._conn.execute("VACUUM")
            except Exception as e:
                logger.debug("vacuum failed: %s", e)

    def get_kv(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def last_vacuum(self) -> float:
        try:
            return float(self.get_kv("last_vacuum") or 0)
        except (TypeError, ValueError):
            return 0.0

    def maintain(
        self,
        retention_days: Dict[str, int],
        vacuum_interval_days: int = 7,
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Prune, checkpoint, and vacuum when due. Safe to call often."""
        moment = int(now if now is not None else time.time())
        result: Dict[str, Any] = {"pruned": 0, "checkpointed": False, "vacuumed": False}

        result["pruned"] = self.prune(retention_days, now=moment)
        self.checkpoint()
        result["checkpointed"] = True

        if vacuum_interval_days:
            if moment - self.last_vacuum() >= vacuum_interval_days * 86400:
                self.vacuum()
                self.set_kv("last_vacuum", str(moment))
                result["vacuumed"] = True

        return result

    # ------------------------------------------------------------------ stats

    def _file_size(self, suffix: str = "") -> int:
        candidate = Path(str(self.path) + suffix)
        return candidate.stat().st_size if candidate.exists() else 0

    @property
    def wal_bytes(self) -> int:
        return self._file_size("-wal")

    def size_bytes(self) -> int:
        return sum(self._file_size(s) for s in ("", "-wal", "-shm"))

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            checks = int(
                self._conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0] or 0
            )
        return {
            "schema_version": self.schema_version,
            "health_checks": checks,
            "size_bytes": self.size_bytes(),
            "wal_bytes": self.wal_bytes,
            "last_vacuum": int(self.last_vacuum()) or None,
        }
