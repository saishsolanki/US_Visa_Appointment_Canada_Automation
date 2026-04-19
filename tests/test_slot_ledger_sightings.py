from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slot_ledger import SlotLedger


def test_slot_ledger_records_full_sighting_history(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_ledger.db"
    ledger = SlotLedger(db_path=db_path)

    inserted_first = ledger.record_slot("2026-08-15", "Ottawa", source="api")
    inserted_second = ledger.record_slot("2026-08-15", "Ottawa", source="ui")

    assert inserted_first is True
    assert inserted_second is False

    dedup_stats = ledger.analytics_summary()
    assert dedup_stats["total_slots"] == 1

    sightings = ledger.recent_sightings(limit=10)
    assert len(sightings) == 2
    assert sightings[0]["slot_date"] == "2026-08-15"
    assert sightings[0]["location"] == "Ottawa"
    assert sightings[0]["source"] in {"api", "ui"}
    assert sightings[0]["discovered"]

    summary = ledger.sightings_summary()
    assert summary["total_sightings"] == 2
    assert summary["unique_dates"] == 1
    assert summary["locations"] == 1
    assert summary["by_source"]["api"] == 1
    assert summary["by_source"]["ui"] == 1


def test_slot_ledger_records_valid_reschedule_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_ledger.db"
    ledger = SlotLedger(db_path=db_path)

    ledger.record_valid_reschedule_date(
        "2026-08-15",
        "Ottawa",
        source="api",
        days_earlier=45,
    )
    ledger.record_valid_reschedule_date(
        "2026-08-20",
        "Toronto",
        source="ui",
        days_earlier=12,
    )

    recent = ledger.recent_valid_reschedule_dates(limit=10)
    assert len(recent) == 2
    assert recent[0]["slot_date"] == "2026-08-20"
    assert recent[0]["days_earlier"] == 12

    summary = ledger.valid_reschedule_summary()
    assert summary["total_valid_dates"] == 2
    assert summary["unique_valid_dates"] == 2
    assert summary["valid_locations"] == 2
    assert summary["best_days_earlier"] == 45
    assert summary["by_source"]["api"] == 1
    assert summary["by_source"]["ui"] == 1


def test_slot_ledger_idempotency_key_deduplicates_retries(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_ledger.db"
    ledger = SlotLedger(db_path=db_path)

    # Same slot/source/check_id in same run should collapse into one event row.
    inserted_first = ledger.record_slot(
        "2026-08-15",
        "Ottawa",
        source="api",
        check_id=123,
        country_code="en-ca",
        facility_id="94",
    )
    inserted_retry = ledger.record_slot(
        "2026-08-15",
        "Ottawa",
        source="api",
        check_id=123,
        country_code="en-ca",
        facility_id="94",
    )
    ledger.flush_pending()

    assert inserted_first is True
    assert inserted_retry is False

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT occurrence_count FROM slot_sightings WHERE source = 'api' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert int(row["occurrence_count"]) == 2


def test_slot_ledger_latest_state_keeps_context(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_ledger.db"
    ledger = SlotLedger(db_path=db_path)

    ledger.record_slot(
        "2026-09-01",
        "Toronto",
        source="ui",
        country_code="en-ca",
        facility_id="94",
        slot_time="09:30",
        check_id=77,
        run_mode="test",
        collector_path="ui",
        timezone_name="America/Toronto",
        latency_ms=142.5,
        rate_limited=True,
        captcha_triggered=False,
        days_earlier=12,
        metadata={"probe": "calendar"},
    )
    ledger.flush_pending()

    state = ledger.recent_latest_state(limit=10)
    assert len(state) == 1
    item = state[0]
    assert item["slot_date"] == "2026-09-01"
    assert item["slot_time"] == "09:30"
    assert item["country_code"] == "en-ca"
    assert item["facility_id"] == "94"
    assert item["last_source"] == "ui"
    assert item["last_check_id"] == 77
    assert item["run_mode"] == "test"
    assert item["collector_path"] == "ui"
    assert item["timezone"] == "America/Toronto"
    assert item["last_rate_limited"] is True
    assert item["last_captcha"] is False
    assert item["best_days_earlier"] == 12
    assert item["last_metadata"]["probe"] == "calendar"


def test_slot_ledger_async_batch_writes_flush(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_ledger.db"
    ledger = SlotLedger(
        db_path=db_path,
        async_writes=True,
        batch_size=2,
        flush_interval_seconds=0.05,
    )

    try:
        ledger.record_slot("2026-10-01", "Ottawa", source="api", check_id=1)
        ledger.record_slot("2026-10-02", "Ottawa", source="api", check_id=2)

        # Worker flushes quickly, but do a bounded wait for CI stability.
        for _ in range(40):
            if len(ledger.recent_sightings(limit=10)) >= 2:
                break
            time.sleep(0.05)

        assert len(ledger.recent_sightings(limit=10)) >= 2
    finally:
        ledger.shutdown()


def test_slot_ledger_rollup_and_prune_moves_old_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_ledger.db"
    ledger = SlotLedger(db_path=db_path)

    old_dt = datetime.now() - timedelta(hours=120)
    ledger.record_slot(
        "2026-07-01",
        "Montreal",
        source="api",
        check_id=10,
        days_earlier=30,
        discovered_at=old_dt,
    )
    ledger.flush_pending()

    stats = ledger.rollup_and_prune(raw_retention_hours=24, archive_retention_days=90)
    assert stats["archived"] >= 1
    assert stats["rolled"] >= 1

    rollup = ledger.rollup_summary()
    assert rollup["hourly_rows"] >= 1
    assert rollup["daily_rows"] >= 1
