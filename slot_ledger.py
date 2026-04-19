"""Persistent slot ledger backed by SQLite.

This module keeps two synchronized representations of slot sightings:

1. ``slot_sightings`` (event log): immutable sighting events with rich context.
2. ``slot_latest_state`` (latest-state table): one compact row per normalized slot key.

The dual-model design preserves full historical fidelity while enabling fast dashboard
reads from the latest-state table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generator, List, Optional, Tuple


APP_STATE_DIR_NAME = "us_visa_checker"
RETENTION_RAW_HOURS_DEFAULT = 72
RETENTION_ARCHIVE_DAYS_DEFAULT = 30


def default_slot_ledger_db_path() -> Path:
    """Return the default slot-ledger DB location in writable app state storage."""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA")
        if base:
            return Path(base) / APP_STATE_DIR_NAME / "slot_ledger.db"
        return Path.home() / "AppData" / "Local" / APP_STATE_DIR_NAME / "slot_ledger.db"

    xdg_state_home = os.getenv("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / APP_STATE_DIR_NAME / "slot_ledger.db"
    return Path.home() / ".local" / "state" / APP_STATE_DIR_NAME / "slot_ledger.db"


def fallback_slot_ledger_db_path() -> Path:
    """Return a temp-directory fallback DB location."""
    return Path(tempfile.gettempdir()) / APP_STATE_DIR_NAME / "slot_ledger.db"


def resolve_slot_ledger_db_path(db_path: Optional[Path] = None) -> Path:
    """Resolve DB path (explicit arg > env > app-data default)."""
    if db_path is not None:
        raw = str(db_path).strip()
        if raw:
            return Path(raw).expanduser()

    env_path = os.getenv("SLOT_LEDGER_DB_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()

    return default_slot_ledger_db_path()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_fragment: str) -> None:
    """Add a column to an existing table if it does not already exist."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row["name"]) for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment}")


def _normalize_text(value: str) -> str:
    return " ".join((value or "").strip().split()).lower()


def _normalize_slot_time(slot_time: str) -> str:
    return "".join((slot_time or "").strip().split())


def _normalized_slot_key(
    *,
    country_code: str,
    facility_id: str,
    slot_date: str,
    slot_time: str,
    location: str,
) -> str:
    return "|".join(
        [
            _normalize_text(country_code),
            _normalize_text(facility_id),
            (slot_date or "").strip(),
            _normalize_slot_time(slot_time),
            _normalize_text(location),
        ]
    )


def _build_idempotency_key(
    *,
    normalized_key: str,
    source: str,
    check_id: Optional[int],
    run_mode: str,
    collector_path: str,
    discovered_iso: str,
) -> str:
    """Build a deterministic fingerprint for retry-safe event deduplication."""
    cycle_token = str(check_id) if check_id is not None else discovered_iso[:16]
    raw = "|".join(
        [
            normalized_key,
            _normalize_text(source),
            cycle_token,
            _normalize_text(run_mode),
            _normalize_text(collector_path),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def initialize_slot_ledger_path(db_path: Optional[Path] = None) -> Tuple[Path, Optional[str]]:
    """Initialize schema at preferred path, fallback to temp path if needed."""
    primary = resolve_slot_ledger_db_path(db_path)
    try:
        _ensure_schema(primary)
        return primary, None
    except Exception as primary_exc:  # noqa: BLE001
        fallback = fallback_slot_ledger_db_path()
        if fallback == primary:
            raise RuntimeError(
                f"Unable to initialize slot ledger at {primary}: {primary_exc}"
            ) from primary_exc

        try:
            _ensure_schema(fallback)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                "Unable to initialize slot ledger at primary path "
                f"{primary} ({primary_exc}) or fallback path {fallback} ({fallback_exc})"
            ) from fallback_exc

        message = (
            f"Slot ledger primary path {primary} was not writable ({primary_exc}). "
            f"Using fallback path {fallback}."
        )
        return fallback, message


DB_PATH = default_slot_ledger_db_path()


@contextmanager
def _connect(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    resolved_db_path = resolve_slot_ledger_db_path(db_path)
    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(resolved_db_path), timeout=8)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(db_path: Optional[Path] = None) -> None:
    with _connect(db_path) as conn:
        # Legacy dedup/status table retained for compatibility with existing code paths.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date   TEXT    NOT NULL,
                location    TEXT    NOT NULL,
                discovered  TEXT    NOT NULL,
                hour        INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                notified    INTEGER NOT NULL DEFAULT 0,
                booked      INTEGER NOT NULL DEFAULT 0,
                UNIQUE(slot_date, location)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_sightings (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key    TEXT    NOT NULL,
                normalized_key     TEXT    NOT NULL,
                slot_date          TEXT    NOT NULL,
                slot_time          TEXT    NOT NULL DEFAULT '',
                location           TEXT    NOT NULL,
                country_code       TEXT    NOT NULL DEFAULT '',
                facility_id        TEXT    NOT NULL DEFAULT '',
                discovered         TEXT    NOT NULL,
                last_seen          TEXT    NOT NULL,
                source             TEXT    NOT NULL DEFAULT 'unknown',
                check_id           INTEGER,
                run_mode           TEXT    NOT NULL DEFAULT '',
                collector_path     TEXT    NOT NULL DEFAULT '',
                timezone           TEXT    NOT NULL DEFAULT '',
                latency_ms         REAL,
                rate_limited       INTEGER NOT NULL DEFAULT 0,
                captcha_triggered  INTEGER NOT NULL DEFAULT 0,
                days_earlier       INTEGER NOT NULL DEFAULT 0,
                hour               INTEGER NOT NULL,
                day_of_week        INTEGER NOT NULL,
                occurrence_count   INTEGER NOT NULL DEFAULT 1,
                metadata_json      TEXT    NOT NULL DEFAULT '{}'
            )
            """
        )

        # Migrate earlier lightweight schema safely.
        _ensure_column(conn, "slot_sightings", "idempotency_key", "idempotency_key TEXT")
        _ensure_column(conn, "slot_sightings", "normalized_key", "normalized_key TEXT")
        _ensure_column(conn, "slot_sightings", "slot_time", "slot_time TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "slot_sightings", "country_code", "country_code TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "slot_sightings", "facility_id", "facility_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "slot_sightings", "last_seen", "last_seen TEXT")
        _ensure_column(conn, "slot_sightings", "check_id", "check_id INTEGER")
        _ensure_column(conn, "slot_sightings", "run_mode", "run_mode TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "slot_sightings", "collector_path", "collector_path TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "slot_sightings", "timezone", "timezone TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "slot_sightings", "latency_ms", "latency_ms REAL")
        _ensure_column(conn, "slot_sightings", "rate_limited", "rate_limited INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "slot_sightings", "captcha_triggered", "captcha_triggered INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "slot_sightings", "days_earlier", "days_earlier INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "slot_sightings", "occurrence_count", "occurrence_count INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "slot_sightings", "metadata_json", "metadata_json TEXT NOT NULL DEFAULT '{}'" )

        conn.execute(
            """
            UPDATE slot_sightings
            SET
                slot_time = COALESCE(slot_time, ''),
                country_code = COALESCE(country_code, ''),
                facility_id = COALESCE(facility_id, ''),
                last_seen = COALESCE(last_seen, discovered),
                run_mode = COALESCE(run_mode, ''),
                collector_path = COALESCE(collector_path, ''),
                timezone = COALESCE(timezone, ''),
                rate_limited = COALESCE(rate_limited, 0),
                captcha_triggered = COALESCE(captcha_triggered, 0),
                days_earlier = COALESCE(days_earlier, 0),
                occurrence_count = CASE
                    WHEN occurrence_count IS NULL OR occurrence_count < 1 THEN 1
                    ELSE occurrence_count
                END,
                metadata_json = COALESCE(metadata_json, '{}')
            """
        )
        conn.execute(
            """
            UPDATE slot_sightings
            SET normalized_key =
                lower(trim(COALESCE(country_code, ''))) || '|' ||
                lower(trim(COALESCE(facility_id, ''))) || '|' ||
                trim(COALESCE(slot_date, '')) || '|' ||
                replace(trim(COALESCE(slot_time, '')), ' ', '') || '|' ||
                lower(trim(COALESCE(location, '')))
            WHERE COALESCE(normalized_key, '') = ''
            """
        )
        conn.execute(
            """
            UPDATE slot_sightings
            SET idempotency_key = 'legacy:' || id
            WHERE COALESCE(idempotency_key, '') = ''
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_latest_state (
                normalized_key      TEXT    PRIMARY KEY,
                slot_date           TEXT    NOT NULL,
                slot_time           TEXT    NOT NULL DEFAULT '',
                location            TEXT    NOT NULL,
                country_code        TEXT    NOT NULL DEFAULT '',
                facility_id         TEXT    NOT NULL DEFAULT '',
                first_seen          TEXT    NOT NULL,
                last_seen           TEXT    NOT NULL,
                sightings_count     INTEGER NOT NULL DEFAULT 0,
                last_source         TEXT    NOT NULL DEFAULT 'unknown',
                last_check_id       INTEGER,
                run_mode            TEXT    NOT NULL DEFAULT '',
                collector_path      TEXT    NOT NULL DEFAULT '',
                timezone            TEXT    NOT NULL DEFAULT '',
                last_latency_ms     REAL,
                last_rate_limited   INTEGER NOT NULL DEFAULT 0,
                last_captcha        INTEGER NOT NULL DEFAULT 0,
                best_days_earlier   INTEGER NOT NULL DEFAULT 0,
                last_metadata_json  TEXT    NOT NULL DEFAULT '{}'
            )
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO slot_latest_state (
                normalized_key,
                slot_date,
                slot_time,
                location,
                country_code,
                facility_id,
                first_seen,
                last_seen,
                sightings_count,
                last_source,
                last_check_id,
                run_mode,
                collector_path,
                timezone,
                last_latency_ms,
                last_rate_limited,
                last_captcha,
                best_days_earlier,
                last_metadata_json
            )
            SELECT
                normalized_key,
                slot_date,
                COALESCE(slot_time, ''),
                location,
                COALESCE(country_code, ''),
                COALESCE(facility_id, ''),
                discovered,
                COALESCE(last_seen, discovered),
                1,
                COALESCE(source, 'unknown'),
                check_id,
                COALESCE(run_mode, ''),
                COALESCE(collector_path, ''),
                COALESCE(timezone, ''),
                latency_ms,
                COALESCE(rate_limited, 0),
                COALESCE(captcha_triggered, 0),
                COALESCE(days_earlier, 0),
                COALESCE(metadata_json, '{}')
            FROM slot_sightings
            """
        )

        conn.execute(
            """
            UPDATE slot_latest_state
            SET
                sightings_count = (
                    SELECT COUNT(*)
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                ),
                first_seen = (
                    SELECT MIN(s.discovered)
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                ),
                last_seen = (
                    SELECT MAX(s.last_seen)
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                ),
                best_days_earlier = (
                    SELECT MAX(COALESCE(s.days_earlier, 0))
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                ),
                last_source = COALESCE((
                    SELECT s.source
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), 'unknown'),
                last_check_id = (
                    SELECT s.check_id
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ),
                run_mode = COALESCE((
                    SELECT s.run_mode
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), ''),
                collector_path = COALESCE((
                    SELECT s.collector_path
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), ''),
                timezone = COALESCE((
                    SELECT s.timezone
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), ''),
                last_latency_ms = (
                    SELECT s.latency_ms
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ),
                last_rate_limited = COALESCE((
                    SELECT s.rate_limited
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), 0),
                last_captcha = COALESCE((
                    SELECT s.captcha_triggered
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), 0),
                last_metadata_json = COALESCE((
                    SELECT s.metadata_json
                    FROM slot_sightings s
                    WHERE s.normalized_key = slot_latest_state.normalized_key
                    ORDER BY s.last_seen DESC, s.id DESC
                    LIMIT 1
                ), '{}')
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_sightings_archive (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key    TEXT    NOT NULL,
                normalized_key     TEXT    NOT NULL,
                slot_date          TEXT    NOT NULL,
                slot_time          TEXT    NOT NULL DEFAULT '',
                location           TEXT    NOT NULL,
                country_code       TEXT    NOT NULL DEFAULT '',
                facility_id        TEXT    NOT NULL DEFAULT '',
                discovered         TEXT    NOT NULL,
                last_seen          TEXT    NOT NULL,
                source             TEXT    NOT NULL DEFAULT 'unknown',
                check_id           INTEGER,
                run_mode           TEXT    NOT NULL DEFAULT '',
                collector_path     TEXT    NOT NULL DEFAULT '',
                timezone           TEXT    NOT NULL DEFAULT '',
                latency_ms         REAL,
                rate_limited       INTEGER NOT NULL DEFAULT 0,
                captcha_triggered  INTEGER NOT NULL DEFAULT 0,
                days_earlier       INTEGER NOT NULL DEFAULT 0,
                hour               INTEGER NOT NULL,
                day_of_week        INTEGER NOT NULL,
                occurrence_count   INTEGER NOT NULL DEFAULT 1,
                metadata_json      TEXT    NOT NULL DEFAULT '{}',
                archived_at        TEXT    NOT NULL,
                rolled_up          INTEGER NOT NULL DEFAULT 0,
                UNIQUE(idempotency_key)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_sightings_rollup_hourly (
                bucket_hour       TEXT    NOT NULL,
                location          TEXT    NOT NULL,
                source            TEXT    NOT NULL,
                country_code      TEXT    NOT NULL,
                facility_id       TEXT    NOT NULL,
                sightings_count   INTEGER NOT NULL DEFAULT 0,
                total_occurrences INTEGER NOT NULL DEFAULT 0,
                unique_slot_keys  INTEGER NOT NULL DEFAULT 0,
                first_seen        TEXT    NOT NULL,
                last_seen         TEXT    NOT NULL,
                best_days_earlier INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(bucket_hour, location, source, country_code, facility_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_sightings_rollup_daily (
                bucket_day        TEXT    NOT NULL,
                location          TEXT    NOT NULL,
                source            TEXT    NOT NULL,
                country_code      TEXT    NOT NULL,
                facility_id       TEXT    NOT NULL,
                sightings_count   INTEGER NOT NULL DEFAULT 0,
                total_occurrences INTEGER NOT NULL DEFAULT 0,
                unique_slot_keys  INTEGER NOT NULL DEFAULT 0,
                first_seen        TEXT    NOT NULL,
                last_seen         TEXT    NOT NULL,
                best_days_earlier INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(bucket_day, location, source, country_code, facility_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS valid_reschedule_dates (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date    TEXT    NOT NULL,
                location     TEXT    NOT NULL,
                discovered   TEXT    NOT NULL,
                source       TEXT    NOT NULL DEFAULT 'unknown',
                days_earlier INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_date ON slots(slot_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered ON slots(discovered)")

        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sightings_idempotency ON slot_sightings(idempotency_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_normalized_key ON slot_sightings(normalized_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_discovered ON slot_sightings(discovered)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_last_seen ON slot_sightings(last_seen)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_location ON slot_sightings(location)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_source ON slot_sightings(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_check_id ON slot_sightings(check_id)")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_last_seen ON slot_latest_state(last_seen)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_slot_date ON slot_latest_state(slot_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_location ON slot_latest_state(location)")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_discovered ON slot_sightings_archive(discovered)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_rolled_up ON slot_sightings_archive(rolled_up)")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_valid_dates_slot_date ON valid_reschedule_dates(slot_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_valid_dates_discovered ON valid_reschedule_dates(discovered)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_valid_dates_location ON valid_reschedule_dates(location)")


class SlotLedger:
    """Slot ledger with event-log/history and latest-state snapshots."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        async_writes: bool = False,
        batch_size: int = 32,
        flush_interval_seconds: float = 1.5,
        dead_letter_path: Optional[Path] = None,
    ) -> None:
        self.db_path, fallback_message = initialize_slot_ledger_path(db_path)
        if fallback_message:
            logging.warning(fallback_message)

        self._async_writes = bool(async_writes)
        self._batch_size = max(1, int(batch_size))
        self._flush_interval_seconds = max(0.25, float(flush_interval_seconds))
        self._pending_records: list[dict[str, Any]] = []
        self._queue_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._known_slot_keys = self._load_known_slot_keys()

        self._dead_letter_path = (
            dead_letter_path
            if dead_letter_path is not None
            else self.db_path.parent / "slot_ledger_dead_letter.jsonl"
        )

        self._last_retention_run = 0.0
        self._retention_interval_seconds = 900.0

        if self._async_writes:
            self._worker_thread = threading.Thread(
                target=self._flush_worker,
                daemon=True,
                name="SlotLedgerFlushWorker",
            )
            self._worker_thread.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop background flush worker and persist pending records."""
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=3)
            self._worker_thread = None
        self.flush_pending()

    def _flush_worker(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._flush_interval_seconds)
            try:
                self.flush_pending(max_batch=self._batch_size)
            except Exception as exc:  # noqa: BLE001
                logging.debug("Slot ledger worker flush failed: %s", exc)
        self.flush_pending()

    def _load_known_slot_keys(self) -> set[str]:
        keys: set[str] = set()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT normalized_key FROM slot_latest_state"
                ).fetchall()
                keys.update(str(row["normalized_key"]) for row in rows if row["normalized_key"])

                # Fallback for very old data before normalized_key existed.
                if not keys:
                    rows = conn.execute(
                        "SELECT slot_date, location FROM slots"
                    ).fetchall()
                    for row in rows:
                        keys.add(
                            _normalized_slot_key(
                                country_code="",
                                facility_id="",
                                slot_date=str(row["slot_date"]),
                                slot_time="",
                                location=str(row["location"]),
                            )
                        )
        except Exception:  # noqa: BLE001
            return set()
        return keys

    # ------------------------------------------------------------------
    # Buffered write path
    # ------------------------------------------------------------------
    def _queue_record(self, record: dict[str, Any]) -> None:
        pending_count = 0
        with self._queue_lock:
            self._pending_records.append(record)
            pending_count = len(self._pending_records)

        if not self._async_writes:
            self.flush_pending()
        elif pending_count >= self._batch_size:
            self.flush_pending(max_batch=self._batch_size)

    def flush_pending(self, max_batch: Optional[int] = None) -> int:
        """Flush queued event records to SQLite in transactional batches."""
        flushed = 0
        while True:
            with self._queue_lock:
                if not self._pending_records:
                    break
                if max_batch is None:
                    batch = self._pending_records[:]
                    self._pending_records.clear()
                else:
                    batch = self._pending_records[:max_batch]
                    del self._pending_records[:max_batch]

            try:
                self._write_batch(batch)
                flushed += len(batch)
            except Exception as exc:  # noqa: BLE001
                self._write_dead_letter(batch, error=str(exc))
                logging.debug("Slot ledger batch write failed, sent to dead-letter: %s", exc)

            if max_batch is not None:
                break

        return flushed

    def _write_dead_letter(self, batch: list[dict[str, Any]], *, error: str) -> None:
        try:
            self._dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
            with self._dead_letter_path.open("a", encoding="utf-8") as handle:
                for record in batch:
                    payload = {
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "error": error,
                        "record": record,
                    }
                    handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            logging.error("Slot ledger dead-letter write failed: %s", exc)

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        with self._write_lock:
            with _connect(self.db_path) as conn:
                for record in batch:
                    is_new_event = self._upsert_sighting_event(conn, record)
                    self._upsert_latest_state(conn, record, increment=1 if is_new_event else 0)
                    self._upsert_legacy_slot(conn, record)

    def _upsert_sighting_event(self, conn: sqlite3.Connection, record: dict[str, Any]) -> bool:
        row = conn.execute(
            """
            INSERT INTO slot_sightings (
                idempotency_key,
                normalized_key,
                slot_date,
                slot_time,
                location,
                country_code,
                facility_id,
                discovered,
                last_seen,
                source,
                check_id,
                run_mode,
                collector_path,
                timezone,
                latency_ms,
                rate_limited,
                captcha_triggered,
                days_earlier,
                hour,
                day_of_week,
                occurrence_count,
                metadata_json
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?
            )
            ON CONFLICT(idempotency_key) DO UPDATE SET
                occurrence_count = slot_sightings.occurrence_count + 1,
                last_seen = excluded.last_seen,
                source = excluded.source,
                check_id = excluded.check_id,
                run_mode = excluded.run_mode,
                collector_path = excluded.collector_path,
                timezone = excluded.timezone,
                latency_ms = COALESCE(excluded.latency_ms, slot_sightings.latency_ms),
                rate_limited = excluded.rate_limited,
                captcha_triggered = excluded.captcha_triggered,
                days_earlier = MAX(slot_sightings.days_earlier, excluded.days_earlier),
                metadata_json = excluded.metadata_json
            RETURNING occurrence_count
            """,
            (
                record["idempotency_key"],
                record["normalized_key"],
                record["slot_date"],
                record["slot_time"],
                record["location"],
                record["country_code"],
                record["facility_id"],
                record["discovered"],
                record["last_seen"],
                record["source"],
                record["check_id"],
                record["run_mode"],
                record["collector_path"],
                record["timezone"],
                record["latency_ms"],
                record["rate_limited"],
                record["captcha_triggered"],
                record["days_earlier"],
                record["hour"],
                record["day_of_week"],
                record["metadata_json"],
            ),
        ).fetchone()
        occurrence_count = int(row["occurrence_count"] if row else 1)
        return occurrence_count == 1

    def _upsert_latest_state(self, conn: sqlite3.Connection, record: dict[str, Any], *, increment: int) -> None:
        conn.execute(
            """
            INSERT INTO slot_latest_state (
                normalized_key,
                slot_date,
                slot_time,
                location,
                country_code,
                facility_id,
                first_seen,
                last_seen,
                sightings_count,
                last_source,
                last_check_id,
                run_mode,
                collector_path,
                timezone,
                last_latency_ms,
                last_rate_limited,
                last_captcha,
                best_days_earlier,
                last_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_key) DO UPDATE SET
                last_seen = excluded.last_seen,
                sightings_count = slot_latest_state.sightings_count + ?,
                last_source = excluded.last_source,
                last_check_id = excluded.last_check_id,
                run_mode = excluded.run_mode,
                collector_path = excluded.collector_path,
                timezone = excluded.timezone,
                last_latency_ms = excluded.last_latency_ms,
                last_rate_limited = excluded.last_rate_limited,
                last_captcha = excluded.last_captcha,
                best_days_earlier = MAX(slot_latest_state.best_days_earlier, excluded.best_days_earlier),
                last_metadata_json = excluded.last_metadata_json
            """,
            (
                record["normalized_key"],
                record["slot_date"],
                record["slot_time"],
                record["location"],
                record["country_code"],
                record["facility_id"],
                record["discovered"],
                record["last_seen"],
                record["source"],
                record["check_id"],
                record["run_mode"],
                record["collector_path"],
                record["timezone"],
                record["latency_ms"],
                record["rate_limited"],
                record["captcha_triggered"],
                record["days_earlier"],
                record["metadata_json"],
                max(0, int(increment)),
            ),
        )

    def _upsert_legacy_slot(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        dedup_cursor = conn.execute(
            """
            INSERT OR IGNORE INTO slots
                (slot_date, location, discovered, hour, day_of_week, notified, booked)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["slot_date"],
                record["location"],
                record["discovered"],
                record["hour"],
                record["day_of_week"],
                int(record["notified"]),
                int(record["booked"]),
            ),
        )
        if dedup_cursor.rowcount == 0:
            conn.execute(
                """
                UPDATE slots
                SET discovered = MAX(discovered, ?)
                WHERE slot_date = ? AND location = ?
                """,
                (
                    record["discovered"],
                    record["slot_date"],
                    record["location"],
                ),
            )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    def record_slot(
        self,
        slot_date: str,
        location: str,
        *,
        source: str = "unknown",
        notified: bool = False,
        booked: bool = False,
        country_code: str = "",
        facility_id: str = "",
        slot_time: str = "",
        check_id: Optional[int] = None,
        run_mode: str = "",
        collector_path: str = "",
        timezone_name: str = "",
        latency_ms: Optional[float] = None,
        rate_limited: bool = False,
        captcha_triggered: bool = False,
        days_earlier: int = 0,
        metadata: Optional[dict[str, Any]] = None,
        discovered_at: Optional[datetime] = None,
    ) -> bool:
        """Queue a sighting event and return True if this looks like a new slot key.

        `record_slot` is retry-safe through idempotency keys. Duplicate writes for the
        same check cycle update `occurrence_count` instead of creating extra rows.
        """
        now = discovered_at or datetime.now()
        discovered = now.isoformat(timespec="seconds")
        source_name = (source or "unknown").strip() or "unknown"
        zone = (timezone_name or "").strip() or time.tzname[0]
        normalized_key = _normalized_slot_key(
            country_code=country_code,
            facility_id=facility_id,
            slot_date=slot_date,
            slot_time=slot_time,
            location=location,
        )
        idem_key = _build_idempotency_key(
            normalized_key=normalized_key,
            source=source_name,
            check_id=check_id,
            run_mode=run_mode,
            collector_path=collector_path or source_name,
            discovered_iso=discovered,
        )

        record = {
            "idempotency_key": idem_key,
            "normalized_key": normalized_key,
            "slot_date": (slot_date or "").strip(),
            "slot_time": _normalize_slot_time(slot_time),
            "location": (location or "").strip(),
            "country_code": (country_code or "").strip(),
            "facility_id": (facility_id or "").strip(),
            "discovered": discovered,
            "last_seen": discovered,
            "source": source_name,
            "check_id": check_id,
            "run_mode": (run_mode or "").strip(),
            "collector_path": (collector_path or source_name).strip(),
            "timezone": zone,
            "latency_ms": float(latency_ms) if latency_ms is not None else None,
            "rate_limited": int(bool(rate_limited)),
            "captcha_triggered": int(bool(captcha_triggered)),
            "days_earlier": max(0, int(days_earlier)),
            "hour": now.hour,
            "day_of_week": now.weekday(),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            "notified": bool(notified),
            "booked": bool(booked),
        }

        try:
            with self._queue_lock:
                inserted = normalized_key not in self._known_slot_keys
                if inserted:
                    self._known_slot_keys.add(normalized_key)

            self._queue_record(record)

            if inserted:
                logging.debug("Slot ledger: recorded new slot state %s", normalized_key)
            else:
                logging.debug("Slot ledger: existing slot state %s (event still logged)", normalized_key)

            return inserted
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger write failed: %s", exc)
            return True  # fail-open for notifier safety

    def mark_notified(self, slot_date: str, location: str) -> None:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE slots SET notified = 1 WHERE slot_date = ? AND location = ?",
                    (slot_date, location),
                )
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger mark_notified failed: %s", exc)

    def mark_booked(self, slot_date: str, location: str) -> None:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE slots SET booked = 1 WHERE slot_date = ? AND location = ?",
                    (slot_date, location),
                )
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger mark_booked failed: %s", exc)

    def record_valid_reschedule_date(
        self,
        slot_date: str,
        location: str,
        *,
        source: str = "unknown",
        days_earlier: int = 0,
    ) -> None:
        """Persist a date that qualifies as a valid reschedule candidate."""
        self.flush_pending()
        now = datetime.now().isoformat(timespec="seconds")
        source_name = (source or "unknown").strip() or "unknown"
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO valid_reschedule_dates
                        (slot_date, location, discovered, source, days_earlier)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slot_date, location, now, source_name, max(0, int(days_earlier))),
                )
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger record_valid_reschedule_date failed: %s", exc)

    def purge_expired(self, ttl_hours: int = 24) -> int:
        """Remove stale rows from the legacy dedup table and run retention tiers."""
        self.flush_pending()

        cutoff = (datetime.now() - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        deleted = 0
        try:
            with _connect(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM slots WHERE discovered < ? AND booked = 0",
                    (cutoff,),
                )
                deleted = int(cursor.rowcount or 0)

            if deleted:
                logging.debug("Slot ledger: purged %d expired slots (TTL=%dh)", deleted, ttl_hours)

            self._maybe_run_retention()
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger purge failed: %s", exc)

        return deleted

    # ------------------------------------------------------------------
    # Retention + rollups
    # ------------------------------------------------------------------
    def _maybe_run_retention(self) -> None:
        now_ts = time.time()
        if (now_ts - self._last_retention_run) < self._retention_interval_seconds:
            return
        self._last_retention_run = now_ts
        try:
            self.rollup_and_prune()
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger rollup/prune skipped due to error: %s", exc)

    def rollup_and_prune(
        self,
        *,
        raw_retention_hours: int = RETENTION_RAW_HOURS_DEFAULT,
        archive_retention_days: int = RETENTION_ARCHIVE_DAYS_DEFAULT,
    ) -> dict[str, int]:
        """Apply retention tiers and maintain hourly/daily rollup tables."""
        self.flush_pending()

        raw_cutoff = (datetime.now() - timedelta(hours=max(1, int(raw_retention_hours)))).isoformat(timespec="seconds")
        archive_cutoff = (
            datetime.now() - timedelta(days=max(1, int(archive_retention_days)))
        ).isoformat(timespec="seconds")

        archived = 0
        rolled = 0
        pruned = 0

        with self._write_lock:
            with _connect(self.db_path) as conn:
                archive_cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO slot_sightings_archive (
                        idempotency_key,
                        normalized_key,
                        slot_date,
                        slot_time,
                        location,
                        country_code,
                        facility_id,
                        discovered,
                        last_seen,
                        source,
                        check_id,
                        run_mode,
                        collector_path,
                        timezone,
                        latency_ms,
                        rate_limited,
                        captcha_triggered,
                        days_earlier,
                        hour,
                        day_of_week,
                        occurrence_count,
                        metadata_json,
                        archived_at,
                        rolled_up
                    )
                    SELECT
                        idempotency_key,
                        normalized_key,
                        slot_date,
                        COALESCE(slot_time, ''),
                        location,
                        COALESCE(country_code, ''),
                        COALESCE(facility_id, ''),
                        discovered,
                        COALESCE(last_seen, discovered),
                        source,
                        check_id,
                        COALESCE(run_mode, ''),
                        COALESCE(collector_path, ''),
                        COALESCE(timezone, ''),
                        latency_ms,
                        COALESCE(rate_limited, 0),
                        COALESCE(captcha_triggered, 0),
                        COALESCE(days_earlier, 0),
                        hour,
                        day_of_week,
                        CASE
                            WHEN occurrence_count IS NULL OR occurrence_count < 1 THEN 1
                            ELSE occurrence_count
                        END,
                        COALESCE(metadata_json, '{}'),
                        ?,
                        0
                    FROM slot_sightings
                    WHERE discovered < ?
                    """,
                    (datetime.now().isoformat(timespec="seconds"), raw_cutoff),
                )
                archived = int(archive_cursor.rowcount or 0)

                conn.execute("DELETE FROM slot_sightings WHERE discovered < ?", (raw_cutoff,))

                unrolled_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM slot_sightings_archive WHERE rolled_up = 0"
                ).fetchone()
                rolled = int(unrolled_row["cnt"] if unrolled_row else 0)

                if rolled:
                    conn.execute(
                        """
                        INSERT INTO slot_sightings_rollup_hourly (
                            bucket_hour,
                            location,
                            source,
                            country_code,
                            facility_id,
                            sightings_count,
                            total_occurrences,
                            unique_slot_keys,
                            first_seen,
                            last_seen,
                            best_days_earlier
                        )
                        SELECT
                            substr(discovered, 1, 13) || ':00:00' AS bucket_hour,
                            location,
                            source,
                            country_code,
                            facility_id,
                            COUNT(*) AS sightings_count,
                            SUM(COALESCE(occurrence_count, 1)) AS total_occurrences,
                            COUNT(DISTINCT normalized_key) AS unique_slot_keys,
                            MIN(discovered) AS first_seen,
                            MAX(last_seen) AS last_seen,
                            MAX(COALESCE(days_earlier, 0)) AS best_days_earlier
                        FROM slot_sightings_archive
                        WHERE rolled_up = 0
                        GROUP BY 1,2,3,4,5
                        ON CONFLICT(bucket_hour, location, source, country_code, facility_id) DO UPDATE SET
                            sightings_count = slot_sightings_rollup_hourly.sightings_count + excluded.sightings_count,
                            total_occurrences = slot_sightings_rollup_hourly.total_occurrences + excluded.total_occurrences,
                            unique_slot_keys = slot_sightings_rollup_hourly.unique_slot_keys + excluded.unique_slot_keys,
                            first_seen = MIN(slot_sightings_rollup_hourly.first_seen, excluded.first_seen),
                            last_seen = MAX(slot_sightings_rollup_hourly.last_seen, excluded.last_seen),
                            best_days_earlier = MAX(slot_sightings_rollup_hourly.best_days_earlier, excluded.best_days_earlier)
                        """
                    )

                    conn.execute(
                        """
                        INSERT INTO slot_sightings_rollup_daily (
                            bucket_day,
                            location,
                            source,
                            country_code,
                            facility_id,
                            sightings_count,
                            total_occurrences,
                            unique_slot_keys,
                            first_seen,
                            last_seen,
                            best_days_earlier
                        )
                        SELECT
                            substr(discovered, 1, 10) AS bucket_day,
                            location,
                            source,
                            country_code,
                            facility_id,
                            COUNT(*) AS sightings_count,
                            SUM(COALESCE(occurrence_count, 1)) AS total_occurrences,
                            COUNT(DISTINCT normalized_key) AS unique_slot_keys,
                            MIN(discovered) AS first_seen,
                            MAX(last_seen) AS last_seen,
                            MAX(COALESCE(days_earlier, 0)) AS best_days_earlier
                        FROM slot_sightings_archive
                        WHERE rolled_up = 0
                        GROUP BY 1,2,3,4,5
                        ON CONFLICT(bucket_day, location, source, country_code, facility_id) DO UPDATE SET
                            sightings_count = slot_sightings_rollup_daily.sightings_count + excluded.sightings_count,
                            total_occurrences = slot_sightings_rollup_daily.total_occurrences + excluded.total_occurrences,
                            unique_slot_keys = slot_sightings_rollup_daily.unique_slot_keys + excluded.unique_slot_keys,
                            first_seen = MIN(slot_sightings_rollup_daily.first_seen, excluded.first_seen),
                            last_seen = MAX(slot_sightings_rollup_daily.last_seen, excluded.last_seen),
                            best_days_earlier = MAX(slot_sightings_rollup_daily.best_days_earlier, excluded.best_days_earlier)
                        """
                    )

                    conn.execute("UPDATE slot_sightings_archive SET rolled_up = 1 WHERE rolled_up = 0")

                prune_cursor = conn.execute(
                    "DELETE FROM slot_sightings_archive WHERE discovered < ?",
                    (archive_cutoff,),
                )
                pruned = int(prune_cursor.rowcount or 0)

        return {
            "archived": archived,
            "rolled": rolled,
            "pruned": pruned,
        }

    def rollup_summary(self) -> dict[str, int]:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                hourly_rows = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM slot_sightings_rollup_hourly"
                ).fetchone()
                daily_rows = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM slot_sightings_rollup_daily"
                ).fetchone()
                archive_rows = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM slot_sightings_archive"
                ).fetchone()
            return {
                "hourly_rows": int(hourly_rows["cnt"] if hourly_rows else 0),
                "daily_rows": int(daily_rows["cnt"] if daily_rows else 0),
                "archive_rows": int(archive_rows["cnt"] if archive_rows else 0),
            }
        except Exception:  # noqa: BLE001
            return {
                "hourly_rows": 0,
                "daily_rows": 0,
                "archive_rows": 0,
            }

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    def is_known(self, slot_date: str, location: str, *, ttl_hours: int = 0) -> bool:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                if ttl_hours > 0:
                    cutoff = (datetime.now() - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
                    row = conn.execute(
                        "SELECT 1 FROM slots WHERE slot_date = ? AND location = ? AND discovered >= ?",
                        (slot_date, location, cutoff),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT 1 FROM slots WHERE slot_date = ? AND location = ?",
                        (slot_date, location),
                    ).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001
            return False

    def recent_slots(self, limit: int = 20) -> list:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT slot_date, location, discovered, notified, booked "
                    "FROM slots ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    "slot_date": r["slot_date"],
                    "location": r["location"],
                    "discovered": r["discovered"],
                    "notified": bool(r["notified"]),
                    "booked": bool(r["booked"]),
                }
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return []

    def recent_sightings(self, limit: int = 200) -> list:
        """Return recent sighting events with context-rich metadata."""
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT
                        slot_date,
                        slot_time,
                        location,
                        discovered,
                        last_seen,
                        source,
                        check_id,
                        run_mode,
                        collector_path,
                        timezone,
                        latency_ms,
                        rate_limited,
                        captcha_triggered,
                        days_earlier,
                        occurrence_count,
                        country_code,
                        facility_id,
                        metadata_json,
                        idempotency_key
                    FROM slot_sightings
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                {
                    "slot_date": r["slot_date"],
                    "slot_time": r["slot_time"],
                    "location": r["location"],
                    "discovered": r["discovered"],
                    "last_seen": r["last_seen"],
                    "source": r["source"],
                    "check_id": r["check_id"],
                    "run_mode": r["run_mode"],
                    "collector_path": r["collector_path"],
                    "timezone": r["timezone"],
                    "latency_ms": r["latency_ms"],
                    "rate_limited": bool(r["rate_limited"]),
                    "captcha_triggered": bool(r["captcha_triggered"]),
                    "days_earlier": int(r["days_earlier"] or 0),
                    "occurrence_count": int(r["occurrence_count"] or 1),
                    "country_code": r["country_code"],
                    "facility_id": r["facility_id"],
                    "metadata": json.loads(r["metadata_json"] or "{}"),
                    "idempotency_key": r["idempotency_key"],
                }
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return []

    def recent_latest_state(self, limit: int = 200) -> list:
        """Return the compact latest-state snapshot rows for fast UI reads."""
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT
                        normalized_key,
                        slot_date,
                        slot_time,
                        location,
                        country_code,
                        facility_id,
                        first_seen,
                        last_seen,
                        sightings_count,
                        last_source,
                        last_check_id,
                        run_mode,
                        collector_path,
                        timezone,
                        last_latency_ms,
                        last_rate_limited,
                        last_captcha,
                        best_days_earlier,
                        last_metadata_json
                    FROM slot_latest_state
                    ORDER BY last_seen DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                {
                    "normalized_key": r["normalized_key"],
                    "slot_date": r["slot_date"],
                    "slot_time": r["slot_time"],
                    "location": r["location"],
                    "country_code": r["country_code"],
                    "facility_id": r["facility_id"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "sightings_count": int(r["sightings_count"] or 0),
                    "last_source": r["last_source"],
                    "last_check_id": r["last_check_id"],
                    "run_mode": r["run_mode"],
                    "collector_path": r["collector_path"],
                    "timezone": r["timezone"],
                    "last_latency_ms": r["last_latency_ms"],
                    "last_rate_limited": bool(r["last_rate_limited"]),
                    "last_captcha": bool(r["last_captcha"]),
                    "best_days_earlier": int(r["best_days_earlier"] or 0),
                    "last_metadata": json.loads(r["last_metadata_json"] or "{}"),
                }
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return []

    def sightings_summary(self) -> dict:
        """Return aggregate metrics, preferring fast latest-state reads."""
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_slots,
                        COUNT(DISTINCT slot_date) AS unique_dates,
                        COUNT(DISTINCT location) AS locations,
                        SUM(COALESCE(sightings_count, 0)) AS total_sightings,
                        MIN(first_seen) AS first_seen,
                        MAX(last_seen) AS last_seen,
                        SUM(COALESCE(best_days_earlier, 0)) AS cumulative_days_earlier
                    FROM slot_latest_state
                    """
                ).fetchone()

                source_rows = conn.execute(
                    """
                    SELECT source, COUNT(*) AS cnt
                    FROM slot_sightings
                    GROUP BY source
                    ORDER BY cnt DESC
                    """
                ).fetchall()

                occurrence_row = conn.execute(
                    "SELECT SUM(COALESCE(occurrence_count, 1)) AS total_occurrences FROM slot_sightings"
                ).fetchone()

            return {
                "total_sightings": int(row["total_sightings"] or 0),
                "total_occurrences": int(occurrence_row["total_occurrences"] or 0),
                "total_slots": int(row["total_slots"] or 0),
                "unique_dates": int(row["unique_dates"] or 0),
                "locations": int(row["locations"] or 0),
                "first_seen": row["first_seen"] or "",
                "last_seen": row["last_seen"] or "",
                "cumulative_days_earlier": int(row["cumulative_days_earlier"] or 0),
                "by_source": {str(r["source"]): int(r["cnt"]) for r in source_rows},
            }
        except Exception:  # noqa: BLE001
            return {
                "total_sightings": 0,
                "total_occurrences": 0,
                "total_slots": 0,
                "unique_dates": 0,
                "locations": 0,
                "first_seen": "",
                "last_seen": "",
                "cumulative_days_earlier": 0,
                "by_source": {},
            }

    def recent_valid_reschedule_dates(self, limit: int = 200) -> list:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT slot_date, location, discovered, source, days_earlier "
                    "FROM valid_reschedule_dates ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    "slot_date": r["slot_date"],
                    "location": r["location"],
                    "discovered": r["discovered"],
                    "source": r["source"],
                    "days_earlier": int(r["days_earlier"] or 0),
                }
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            return []

    def valid_reschedule_summary(self) -> dict:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS total_valid_dates, "
                    "COUNT(DISTINCT slot_date) AS unique_valid_dates, "
                    "COUNT(DISTINCT location) AS valid_locations, "
                    "MIN(discovered) AS first_valid_seen, "
                    "MAX(discovered) AS last_valid_seen, "
                    "MAX(days_earlier) AS best_days_earlier "
                    "FROM valid_reschedule_dates"
                ).fetchone()
                source_rows = conn.execute(
                    "SELECT source, COUNT(*) AS cnt "
                    "FROM valid_reschedule_dates GROUP BY source ORDER BY cnt DESC"
                ).fetchall()

            return {
                "total_valid_dates": int(row["total_valid_dates"] or 0),
                "unique_valid_dates": int(row["unique_valid_dates"] or 0),
                "valid_locations": int(row["valid_locations"] or 0),
                "first_valid_seen": row["first_valid_seen"] or "",
                "last_valid_seen": row["last_valid_seen"] or "",
                "best_days_earlier": int(row["best_days_earlier"] or 0),
                "by_source": {
                    str(r["source"]): int(r["cnt"])
                    for r in source_rows
                },
            }
        except Exception:  # noqa: BLE001
            return {
                "total_valid_dates": 0,
                "unique_valid_dates": 0,
                "valid_locations": 0,
                "first_valid_seen": "",
                "last_valid_seen": "",
                "best_days_earlier": 0,
                "by_source": {},
            }

    def analytics_summary(self) -> dict:
        """Return aggregate stats for dashboards (fast path via latest-state)."""
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(DISTINCT slot_date) AS unique_dates,
                        COUNT(DISTINCT location) AS locations,
                        SUM(COALESCE(last_rate_limited, 0)) AS rate_limited_slots,
                        SUM(COALESCE(last_captcha, 0)) AS captcha_slots
                    FROM slot_latest_state
                    """
                ).fetchone()
                legacy = conn.execute(
                    "SELECT SUM(booked) AS booked, SUM(notified) AS notified FROM slots"
                ).fetchone()

            return {
                "total_slots": int(row["total"] or 0),
                "unique_dates": int(row["unique_dates"] or 0),
                "locations": int(row["locations"] or 0),
                "booked": int(legacy["booked"] or 0),
                "notified": int(legacy["notified"] or 0),
                "rate_limited_slots": int(row["rate_limited_slots"] or 0),
                "captcha_slots": int(row["captcha_slots"] or 0),
            }
        except Exception:  # noqa: BLE001
            return {}

    def is_notified(self, slot_date: str, location: str, *, ttl_hours: int = 0) -> bool:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                if ttl_hours > 0:
                    cutoff = (datetime.now() - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
                    row = conn.execute(
                        "SELECT 1 FROM slots WHERE slot_date = ? AND location = ? AND notified = 1 AND discovered >= ?",
                        (slot_date, location, cutoff),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT 1 FROM slots WHERE slot_date = ? AND location = ? AND notified = 1",
                        (slot_date, location),
                    ).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001
            return False

    def location_histogram(self, *, since_hours: int = 168) -> List[Tuple[str, int]]:
        self.flush_pending()
        cutoff = (datetime.now() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT location, SUM(COALESCE(sightings_count, 0)) AS cnt "
                    "FROM slot_latest_state WHERE last_seen >= ? "
                    "GROUP BY location ORDER BY cnt DESC",
                    (cutoff,),
                ).fetchall()
            return [(str(r["location"]), int(r["cnt"] or 0)) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def location_score(self, location: str, *, since_hours: int = 168) -> float:
        self.flush_pending()
        cutoff = (datetime.now() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT SUM(COALESCE(sightings_count, 0)) AS cnt, MAX(last_seen) AS last_seen "
                    "FROM slot_latest_state WHERE location = ? AND last_seen >= ?",
                    (location, cutoff),
                ).fetchone()
            cnt = float(row["cnt"] or 0)
            last_seen = row["last_seen"]
            if not last_seen:
                return cnt
            try:
                last_dt = datetime.fromisoformat(str(last_seen))
            except ValueError:
                return cnt
            age_hours = max(0.0, (datetime.now() - last_dt).total_seconds() / 3600.0)
            recency_boost = max(0.1, 1.0 - min(1.0, age_hours / max(1.0, since_hours)))
            return round(cnt + recency_boost, 3)
        except Exception:  # noqa: BLE001
            return 0.0

    def hourly_histogram(self) -> List[Tuple[int, int]]:
        self.flush_pending()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT hour, COUNT(*) AS cnt FROM slot_sightings GROUP BY hour ORDER BY hour"
                ).fetchall()
            return [(int(r["hour"]), int(r["cnt"] or 0)) for r in rows]
        except Exception:  # noqa: BLE001
            return []
