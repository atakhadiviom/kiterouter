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

SCHEMA_VERSION = 5

# (version, [statements]) — applied in order, once each, tracked by user_version.
MIGRATIONS: Sequence[Tuple[int, Sequence[str]]] = (
    (
        1,
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
    (
        2,
        (
            # Model catalogs live here rather than in config.json, which is
            # rewritten wholesale on every save and had already reached 205 KB
            # of accumulated test results.
            """
            CREATE TABLE IF NOT EXISTS models (
                provider      TEXT    NOT NULL,
                model_id      TEXT    NOT NULL,
                source        TEXT    NOT NULL DEFAULT 'discovery',
                first_seen_at INTEGER NOT NULL,
                last_seen_at  INTEGER NOT NULL,
                synced_at     INTEGER,
                PRIMARY KEY (provider, model_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider)",
            "CREATE INDEX IF NOT EXISTS idx_models_last_seen ON models(last_seen_at)",
        ),
    ),
    (
        3,
        (
            # Real request history. Until now this was a 100-entry in-memory ring
            # buffer of 400-character previews, which cannot answer "what is p95
            # for this provider" or "what did this cost".
            """
            CREATE TABLE IF NOT EXISTS requests (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                at                   INTEGER NOT NULL,
                provider             TEXT,
                connection           TEXT,
                model                TEXT,
                status               TEXT    NOT NULL,
                latency_ms           INTEGER,
                ttft_ms              INTEGER,
                tokens_in            INTEGER DEFAULT 0,
                tokens_out           INTEGER DEFAULT 0,
                tokens_cache_read    INTEGER DEFAULT 0,
                tokens_cache_write   INTEGER DEFAULT 0,
                tokens_reasoning     INTEGER DEFAULT 0,
                rtk_saved            INTEGER DEFAULT 0,
                combo                TEXT,
                session_key          TEXT,
                api_key_id           TEXT,
                error                TEXT,
                prompt_preview       TEXT,
                response_preview     TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_requests_at ON requests(at)",
            "CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider, at)",
            "CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model, at)",
            "CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status, at)",
        ),
    ),
    (
        4,
        (
            # Bodies are the disk-heavy part, so they live as capped files rather
            # than rows, and expire on their own (shorter) retention.
            "ALTER TABLE requests ADD COLUMN body_path TEXT",
        ),
    ),
    (
        5,
        (
            # Cost is written only when an upstream reports one and stays NULL
            # otherwise — there is deliberately no price derivation, so nothing
            # here can present a guessed figure as billed.
            "ALTER TABLE requests ADD COLUMN cost_usd REAL",
            "ALTER TABLE requests ADD COLUMN cost_is_estimate INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE requests ADD COLUMN tokens_is_estimate INTEGER NOT NULL DEFAULT 0",
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

    # ---------------------------------------------------------------- models

    def record_models(
        self,
        provider: str,
        model_ids: Sequence[str],
        source: str = "discovery",
        now: Optional[int] = None,
    ) -> int:
        """Upsert a provider's discovered catalog.

        A model already known as `manual` keeps that source: discovery must never
        silently reclassify something the operator pinned.
        """
        moment = int(now if now is not None else time.time())
        written = 0
        with self._lock:
            for model_id in model_ids:
                if not model_id:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO models (provider, model_id, source, first_seen_at, last_seen_at, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, model_id) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        synced_at    = excluded.synced_at,
                        source       = CASE WHEN models.source = 'manual'
                                            THEN 'manual' ELSE excluded.source END
                    """,
                    (provider, str(model_id), source, moment, moment, moment),
                )
                written += 1
            self._conn.commit()
        return written

    def record_manual_model(self, provider: str, model_id: str, now: Optional[int] = None) -> None:
        """Pin a model so discovery will not remove or reclassify it."""
        moment = int(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO models (provider, model_id, source, first_seen_at, last_seen_at, synced_at)
                VALUES (?, ?, 'manual', ?, ?, NULL)
                ON CONFLICT(provider, model_id) DO UPDATE SET source = 'manual'
                """,
                (provider, str(model_id), moment, moment),
            )
            self._conn.commit()

    def models_for(self, provider: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT model_id FROM models WHERE provider = ? ORDER BY model_id",
                (provider,),
            ).fetchall()
        return [r["model_id"] for r in rows]

    def all_models(self) -> Dict[str, List[str]]:
        """Every catalogued model, grouped by provider."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, model_id FROM models ORDER BY provider, model_id"
            ).fetchall()
        grouped: Dict[str, List[str]] = {}
        for row in rows:
            grouped.setdefault(row["provider"], []).append(row["model_id"])
        return grouped

    def catalog_freshness(self) -> Dict[str, Dict[str, Any]]:
        """Per provider: how many models, and when the catalog last synced."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT provider,
                       COUNT(*)          AS models,
                       MAX(synced_at)    AS synced_at,
                       MAX(last_seen_at) AS last_seen_at,
                       SUM(CASE WHEN source = 'manual' THEN 1 ELSE 0 END) AS manual
                FROM models GROUP BY provider ORDER BY provider
                """
            ).fetchall()
        return {r["provider"]: dict(r) for r in rows}

    def expire_models(self, unseen_days: int, now: Optional[int] = None) -> int:
        """Drop discovered models not seen for a while.

        Manual entries are exempt: they are operator intent, not observation, so
        an absent model upstream does not mean the operator was wrong.
        """
        if not unseen_days:
            return 0
        moment = int(now if now is not None else time.time())
        cutoff = moment - int(unseen_days) * 86400
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM models WHERE source != 'manual' AND last_seen_at < ?",
                (cutoff,),
            )
            removed = max(0, cursor.rowcount or 0)
            self._conn.commit()
        return removed

    # -------------------------------------------------------------- requests

    def record_request(
        self,
        provider: Optional[str],
        model: Optional[str],
        status: str,
        latency_ms: Optional[int] = None,
        ttft_ms: Optional[int] = None,
        connection: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tokens_cache_read: int = 0,
        tokens_cache_write: int = 0,
        tokens_reasoning: int = 0,
        rtk_saved: int = 0,
        combo: Optional[str] = None,
        session_key: Optional[str] = None,
        api_key_id: Optional[str] = None,
        error: Optional[str] = None,
        prompt_preview: Optional[str] = None,
        response_preview: Optional[str] = None,
        body_path: Optional[str] = None,
        cost_usd: Optional[float] = None,
        cost_is_estimate: int = 0,
        tokens_is_estimate: int = 0,
        at: Optional[int] = None,
    ) -> int:
        """Persist one request. Returns its row id."""
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO requests (
                    at, provider, connection, model, status, latency_ms, ttft_ms,
                    tokens_in, tokens_out, tokens_cache_read, tokens_cache_write,
                    tokens_reasoning, rtk_saved, combo, session_key, api_key_id,
                    error, prompt_preview, response_preview, body_path,
                    cost_usd, cost_is_estimate, tokens_is_estimate
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(at if at is not None else time.time()),
                    provider,
                    connection,
                    model,
                    status,
                    int(latency_ms) if latency_ms is not None else None,
                    int(ttft_ms) if ttft_ms is not None else None,
                    int(tokens_in or 0),
                    int(tokens_out or 0),
                    int(tokens_cache_read or 0),
                    int(tokens_cache_write or 0),
                    int(tokens_reasoning or 0),
                    int(rtk_saved or 0),
                    combo,
                    session_key,
                    api_key_id,
                    (str(error)[:1000] if error else None),
                    (str(prompt_preview)[:400] if prompt_preview else None),
                    (str(response_preview)[:400] if response_preview else None),
                    body_path,
                    float(cost_usd) if cost_usd is not None else None,
                    int(cost_is_estimate or 0),
                    int(tokens_is_estimate or 0),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def recent_requests(
        self,
        limit: int = 50,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first. ``before_id`` pages backwards without OFFSET drift."""
        clauses: List[str] = []
        params: List[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("at >= ?")
            params.append(int(since))
        if before_id is not None:
            clauses.append("id < ?")
            params.append(int(before_id))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def request_by_id(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE id = ?", (int(request_id),)
            ).fetchone()
        return dict(row) if row else None

    def request_stats(self, since: Optional[int] = None) -> Dict[str, Any]:
        """Counts, success rate and totals over a window."""
        clause, params = ("WHERE at >= ?", (int(since),)) if since is not None else ("", ())
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*)                              AS total,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                       CAST(AVG(latency_ms) AS INTEGER)      AS avg_latency_ms,
                       CAST(AVG(ttft_ms) AS INTEGER)         AS avg_ttft_ms,
                       SUM(tokens_in)                       AS tokens_in,
                       SUM(tokens_out)                      AS tokens_out,
                       SUM(rtk_saved)                       AS rtk_saved,
                       MAX(at)                              AS last_at
                FROM requests {clause}
                """,
                params,
            ).fetchone()
        stats = dict(row) if row else {}
        total = int(stats.get("total") or 0)
        ok = int(stats.get("ok") or 0)
        stats["total"] = total
        stats["ok"] = ok
        stats["failed"] = total - ok
        stats["ok_rate_pct"] = round(ok * 100.0 / total, 1) if total else 0.0
        return stats

    def count_requests(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] or 0)

    # ------------------------------------------------------------ aggregation

    @staticmethod
    def _percentile(values: List[int], pct: float) -> Optional[int]:
        """Linear-interpolated percentile. None for an empty sample."""
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * (pct / 100.0)
        low = int(rank)
        high = min(low + 1, len(ordered) - 1)
        return int(round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low)))

    def _window(self, since: Optional[int]) -> Tuple[str, tuple]:
        return ("WHERE at >= ?", (int(since),)) if since is not None else ("WHERE 1=1", ())

    def usage_summary(
        self, since: Optional[int] = None, group_by: str = "provider", limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Request and token totals grouped by provider, model or combo."""
        column = {"provider": "provider", "model": "model", "combo": "combo"}.get(group_by)
        if column is None:
            raise ValueError(f"unsupported group_by: {group_by}")
        clause, params = self._window(since)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT COALESCE({column}, '(unknown)')              AS key,
                       COUNT(*)                                     AS requests,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                       SUM(tokens_in)                               AS tokens_in,
                       SUM(tokens_out)                              AS tokens_out,
                       SUM(tokens_cache_read)                       AS tokens_cache_read,
                       SUM(tokens_cache_write)                      AS tokens_cache_write,
                       SUM(tokens_reasoning)                        AS tokens_reasoning,
                       SUM(rtk_saved)                               AS rtk_saved,
                       MAX(at)                                      AS last_at
                FROM requests {clause}
                GROUP BY key
                ORDER BY requests DESC
                LIMIT ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            total = int(item.get("requests") or 0)
            ok = int(item.get("ok") or 0)
            item["failed"] = total - ok
            item["ok_rate_pct"] = round(ok * 100.0 / total, 1) if total else 0.0
            out.append(item)
        return out

    def usage_daily(self, since: Optional[int] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """Per-day request and token totals, newest first."""
        clause, params = self._window(since)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT strftime('%Y-%m-%d', at, 'unixepoch', 'localtime') AS day,
                       COUNT(*)                                     AS requests,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                       SUM(tokens_in)                               AS tokens_in,
                       SUM(tokens_out)                              AS tokens_out,
                       SUM(rtk_saved)                               AS rtk_saved
                FROM requests {clause}
                GROUP BY day
                ORDER BY day DESC
                LIMIT ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def token_breakdown(self, since: Optional[int] = None) -> Dict[str, Any]:
        """Token totals split exactly as recorded — no derived or estimated parts."""
        clause, params = self._window(since)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT SUM(tokens_in)            AS tokens_in,
                       SUM(tokens_out)           AS tokens_out,
                       SUM(tokens_cache_read)    AS tokens_cache_read,
                       SUM(tokens_cache_write)   AS tokens_cache_write,
                       SUM(tokens_reasoning)     AS tokens_reasoning,
                       SUM(rtk_saved)            AS rtk_saved,
                       COUNT(*)                  AS requests
                FROM requests {clause}
                """,
                params,
            ).fetchone()
        out = {k: int(v or 0) for k, v in dict(row).items()}
        out["tokens_total"] = out["tokens_in"] + out["tokens_out"]
        return out

    def provider_metrics(self, since: Optional[int] = None) -> List[Dict[str, Any]]:
        """Per-provider counts, success rate and latency/TTFT percentiles.

        Percentiles are computed from the recorded values rather than approximated
        in SQL, and TTFT is only averaged over rows that actually reported one.
        """
        clause, params = self._window(since)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT provider,
                       COUNT(*)                                     AS requests,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                       MAX(at)                                      AS last_at
                FROM requests {clause}
                GROUP BY provider
                ORDER BY requests DESC
                """,
                params,
            ).fetchall()
            samples = self._conn.execute(
                f"SELECT provider, latency_ms, ttft_ms, error FROM requests {clause}",
                params,
            ).fetchall()

        latencies: Dict[str, List[int]] = {}
        ttfts: Dict[str, List[int]] = {}
        last_error: Dict[str, str] = {}
        for sample in samples:
            provider = sample["provider"] or "(unknown)"
            if sample["latency_ms"] is not None:
                latencies.setdefault(provider, []).append(int(sample["latency_ms"]))
            if sample["ttft_ms"] is not None:
                ttfts.setdefault(provider, []).append(int(sample["ttft_ms"]))
            if sample["error"]:
                last_error.setdefault(provider, sample["error"])

        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            provider = item.get("provider") or "(unknown)"
            total = int(item.get("requests") or 0)
            ok = int(item.get("ok") or 0)
            item["provider"] = provider
            item["failed"] = total - ok
            item["ok_rate_pct"] = round(ok * 100.0 / total, 1) if total else 0.0
            item["p50_latency_ms"] = self._percentile(latencies.get(provider, []), 50)
            item["p95_latency_ms"] = self._percentile(latencies.get(provider, []), 95)
            item["p50_ttft_ms"] = self._percentile(ttfts.get(provider, []), 50)
            item["p95_ttft_ms"] = self._percentile(ttfts.get(provider, []), 95)
            item["ttft_samples"] = len(ttfts.get(provider, []))
            item["last_error"] = (last_error.get(provider) or "")[:200] or None
            out.append(item)
        return out

    def cost_summary(
        self, since: Optional[int] = None, group_by: str = "provider", limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Spend grouped by provider, model or key.

        Cost only exists where an upstream reported it. Rows with NULL cost are
        counted as unknown, never zeroed — so until any provider reports cost
        this honestly reports unknowns rather than figures.
        """
        column = {"provider": "provider", "model": "model", "key": "api_key_id"}.get(group_by)
        if column is None:
            raise ValueError(f"unsupported group_by: {group_by}")
        clause, params = self._window(since)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT COALESCE({column}, '(unattributed)')                    AS key,
                       COUNT(*)                                                 AS requests,
                       SUM(cost_usd)                                            AS cost_usd,
                       SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END)        AS unknown_cost_requests,
                       SUM(CASE WHEN cost_is_estimate = 1 THEN cost_usd ELSE 0 END) AS cost_estimated_usd,
                       SUM(CASE WHEN cost_is_estimate = 0 THEN cost_usd ELSE 0 END) AS cost_billed_usd,
                       MAX(at)                                                  AS last_at
                FROM requests {clause}
                GROUP BY key
                ORDER BY cost_usd DESC
                LIMIT ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ bodies

    def prune_bodies(self, retention_days: int, now: Optional[int] = None) -> int:
        """Delete body files past their (shorter) retention, then clear the refs.

        Bodies are the disk-heavy part, so they expire well before the request
        rows that point at them.
        """
        if not retention_days:
            return 0
        moment = int(now if now is not None else time.time())
        cutoff = moment - int(retention_days) * 86400

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, body_path FROM requests WHERE body_path IS NOT NULL AND at < ?",
                (cutoff,),
            ).fetchall()

        removed = 0
        for row in rows:
            path = Path(row["body_path"]) if row["body_path"] else None
            try:
                if path and path.exists():
                    path.unlink()
                    removed += 1
            except Exception as e:
                logger.debug("Could not remove body %s: %s", path, e)

        if rows:
            ids = [int(r["id"]) for r in rows]
            with self._lock:
                self._conn.executemany(
                    "UPDATE requests SET body_path = NULL WHERE id = ?", [(i,) for i in ids]
                )
                self._conn.commit()
        return removed

    # ------------------------------------------------------------- maintenance

    def prune(self, retention_days: Dict[str, int], now: Optional[int] = None) -> int:
        """Delete rows past their retention window; returns the row count removed."""
        moment = int(now if now is not None else time.time())
        removed = 0
        for table, days in retention_days.items():
            if not days:
                continue
            cutoff = moment - int(days) * 86400

            # Bodies carry their own, shorter retention, but if a row is being
            # dropped take its file with it rather than leaving an orphan.
            if table == "requests":
                with self._lock:
                    rows = self._conn.execute(
                        "SELECT body_path FROM requests WHERE at < ? AND body_path IS NOT NULL",
                        (cutoff,),
                    ).fetchall()
                for row in rows:
                    try:
                        candidate = Path(row["body_path"])
                        if candidate.exists():
                            candidate.unlink()
                    except Exception as e:
                        logger.debug("Could not remove body %s: %s", row["body_path"], e)

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
        body_retention_days: int = 0,
    ) -> Dict[str, Any]:
        """Prune, expire bodies, checkpoint, and vacuum when due."""
        moment = int(now if now is not None else time.time())
        result: Dict[str, Any] = {
            "pruned": 0,
            "bodies_removed": 0,
            "checkpointed": False,
            "vacuumed": False,
        }

        result["pruned"] = self.prune(retention_days, now=moment)
        # Bodies expire on their own, shorter window: they are the disk-heavy part.
        if body_retention_days:
            result["bodies_removed"] = self.prune_bodies(body_retention_days, now=moment)
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
            requests = int(
                self._conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] or 0
            )
            models = int(self._conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] or 0)
            bodies = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE body_path IS NOT NULL"
                ).fetchone()[0]
                or 0
            )
        return {
            "schema_version": self.schema_version,
            "health_checks": checks,
            "requests": requests,
            "models": models,
            "bodies": bodies,
            "size_bytes": self.size_bytes(),
            "wal_bytes": self.wal_bytes,
            "last_vacuum": int(self.last_vacuum()) or None,
        }
