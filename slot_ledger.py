"""Persistent slot ledger backed by SQLite.

Records every appointment slot the checker discovers, enabling:
* **Deduplication** – suppress repeated notifications for the same date/location.
* **Historical analytics** – query which slots were observed and when.
* **Pattern insights** – feed into scheduling weight calculations.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator, List, Optional, Tuple


DB_PATH = Path("slot_ledger.db")


@contextmanager
def _connect(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(db_path), timeout=5)
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


def _ensure_schema(db_path: Path = DB_PATH) -> None:
    with _connect(db_path) as conn:
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
            "CREATE INDEX IF NOT EXISTS idx_slot_date ON slots(slot_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discovered ON slots(discovered)"
        )


class SlotLedger:
    """Thin wrapper around the ``slots`` SQLite table."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DB_PATH
        _ensure_schema(self.db_path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    def record_slot(
        self,
        slot_date: str,
        location: str,
        *,
        notified: bool = False,
        booked: bool = False,
    ) -> bool:
        """Insert a slot if it has not been seen before.

        Returns ``True`` when a *new* row was inserted (i.e. this is not a
        duplicate), ``False`` when the slot already existed.
        """
        now = datetime.now()
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO slots
                        (slot_date, location, discovered, hour, day_of_week, notified, booked)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slot_date,
                        location,
                        now.isoformat(),
                        now.hour,
                        now.weekday(),
                        int(notified),
                        int(booked),
                    ),
                )
                inserted = conn.total_changes > 0
            if inserted:
                logging.debug("Slot ledger: recorded new slot %s @ %s", slot_date, location)
            else:
                logging.debug("Slot ledger: duplicate slot %s @ %s (suppressed)", slot_date, location)
            return inserted
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger write failed: %s", exc)
            return True  # Fail-open: treat as new so notifications still fire

    def mark_notified(self, slot_date: str, location: str) -> None:
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE slots SET notified = 1 WHERE slot_date = ? AND location = ?",
                    (slot_date, location),
                )
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger mark_notified failed: %s", exc)

    def mark_booked(self, slot_date: str, location: str) -> None:
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE slots SET booked = 1 WHERE slot_date = ? AND location = ?",
                    (slot_date, location),
                )
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger mark_booked failed: %s", exc)

    def purge_expired(self, ttl_hours: int = 24) -> int:
        """Remove slots older than *ttl_hours* (keeps booked slots).

        Returns the number of rows deleted.
        """
        cutoff = (datetime.now() - timedelta(hours=ttl_hours)).isoformat()
        try:
            with _connect(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM slots WHERE discovered < ? AND booked = 0",
                    (cutoff,),
                )
                deleted = cursor.rowcount
            if deleted:
                logging.debug("Slot ledger: purged %d expired slots (TTL=%dh)", deleted, ttl_hours)
            return deleted
        except Exception as exc:  # noqa: BLE001
            logging.debug("Slot ledger purge failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    def is_known(self, slot_date: str, location: str, *, ttl_hours: int = 0) -> bool:
        """Return ``True`` if this slot has been seen (within TTL if specified)."""
        try:
            with _connect(self.db_path) as conn:
                if ttl_hours > 0:
                    cutoff = (datetime.now() - timedelta(hours=ttl_hours)).isoformat()
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
        """Return the most recently discovered slots as dicts."""
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

    def analytics_summary(self) -> dict:
        """Return aggregate stats for the dashboard."""
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS total, "
                    "COUNT(DISTINCT slot_date) AS unique_dates, "
                    "COUNT(DISTINCT location) AS locations, "
                    "SUM(booked) AS booked, "
                    "SUM(notified) AS notified "
                    "FROM slots"
                ).fetchone()
            return {
                "total_slots": row["total"] or 0,
                "unique_dates": row["unique_dates"] or 0,
                "locations": row["locations"] or 0,
                "booked": row["booked"] or 0,
                "notified": row["notified"] or 0,
            }
        except Exception:  # noqa: BLE001
            return {}

    def hourly_histogram(self) -> List[Tuple[int, int]]:
        """Return ``(hour, count)`` pairs across all recorded slots."""
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT hour, COUNT(*) AS cnt FROM slots GROUP BY hour ORDER BY hour"
                ).fetchall()
            return [(r["hour"], r["cnt"]) for r in rows]
        except Exception:  # noqa: BLE001
            return []
