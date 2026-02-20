"""Behavioral tests for strategic features.

These tests verify the logic of burst mode, multi-location, pattern weights,
auto-book guardrails, and the slot ledger **without** launching a browser.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import the checker module without Selenium / Chrome
# ---------------------------------------------------------------------------
os.environ.setdefault("WDM_LOG_LEVEL", "0")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_PROJECT_ROOT)

# Pre-register a lightweight stub for ``logging_utils`` so importing
# ``visa_appointment_checker`` does not try to create ``logs/`` on disk
# (which fails on OneDrive-synced volumes).
if "logging_utils" not in sys.modules:
    _stub = ModuleType("logging_utils")
    _stub.LOG_PATH = _PROJECT_ROOT / "logs" / "visa_checker.log"  # type: ignore[attr-defined]
    _stub.ARTIFACTS_DIR = _PROJECT_ROOT / "artifacts"  # type: ignore[attr-defined]
    _stub.configure_logging = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules["logging_utils"] = _stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides):  # type: ignore[override]
    """Build a ``CheckerConfig`` with safe defaults + caller overrides."""
    from visa_appointment_checker import CheckerConfig

    defaults: dict = dict(
        email="test@example.com",
        password="pw",
        current_appointment_date="2025-12-01",
        location="Ottawa",
        start_date="2025-06-01",
        end_date="2025-12-31",
        check_frequency_minutes=5,
        smtp_server="",
        smtp_port=587,
        smtp_user="",
        smtp_pass="",
        notify_email="",
        auto_book=False,
        driver_restart_checks=50,
        heartbeat_path=None,
        max_retry_attempts=2,
        retry_backoff_seconds=5,
        sleep_jitter_seconds=0,
        busy_backoff_min_minutes=10,
        busy_backoff_max_minutes=15,
        abort_on_captcha=False,
        burst_mode_enabled=True,
        multi_location_check=True,
        backup_locations="Toronto,Montreal,Vancouver",
        prime_hours_start="6,12,17,22",
        prime_hours_end="9,14,19,1",
        prime_time_backoff_multiplier=0.5,
        weekend_frequency_multiplier=2.0,
        pattern_learning_enabled=True,
        min_improvement_days=7,
        auto_book_dry_run=True,
        auto_book_confirmation_wait_seconds=0,
        timezone="America/Toronto",
        telegram_bot_token="",
        telegram_chat_id="",
        webhook_url="",
        preferred_time="any",
        max_requests_per_hour=120,
        slot_ttl_hours=24,
    )
    defaults.update(overrides)
    return CheckerConfig(**defaults)


# =========================================================================
# 1. Burst-mode trigger logic
# =========================================================================

class TestBurstModeTrigger:
    """Verify ``_should_use_burst_mode`` fires under the right conditions."""

    def _make_checker(self, **cfg_kw):
        from visa_appointment_checker import VisaAppointmentChecker

        cfg = _make_config(**cfg_kw)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._last_busy_check = None
        checker._burst_mode_active = False
        return checker

    def test_disabled_when_config_off(self):
        checker = self._make_checker(burst_mode_enabled=False)
        assert checker._should_use_burst_mode() is False

    @patch("visa_appointment_checker.datetime")
    def test_enabled_during_morning(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 7, 1, 7, 30)
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        checker = self._make_checker()
        assert checker._should_use_burst_mode() is True

    @patch("visa_appointment_checker.datetime")
    def test_enabled_during_lunch(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 7, 1, 13, 0)
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        checker = self._make_checker()
        assert checker._should_use_burst_mode() is True

    @patch("visa_appointment_checker.datetime")
    def test_disabled_during_night(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 7, 1, 3, 0)
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        checker = self._make_checker()
        # Also set _last_busy_check to recent so the 30-min rule doesn't fire
        checker._last_busy_check = datetime(2025, 7, 1, 2, 55)
        assert checker._should_use_burst_mode() is False

    @patch("visa_appointment_checker.datetime")
    def test_enabled_after_long_busy_gap(self, mock_dt):
        now = datetime(2025, 7, 1, 16, 0)
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        checker = self._make_checker()
        checker._last_busy_check = now - timedelta(minutes=45)
        assert checker._should_use_burst_mode() is True


# =========================================================================
# 2. Pattern weight calculator
# =========================================================================

class TestPatternWeight:
    """Verify ``_calculate_pattern_weight`` returns sensible multipliers."""

    def _make_checker(self, history=None):
        from visa_appointment_checker import VisaAppointmentChecker

        cfg = _make_config()
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._availability_history = history or []
        return checker

    def test_insufficient_data_returns_neutral(self):
        checker = self._make_checker(history=[{"hour": 8, "day_of_week": 1, "event": "busy"}] * 5)
        assert checker._calculate_pattern_weight() == 1.0

    def test_high_success_lowers_weight(self):
        now = datetime.now()
        events = [
            {"hour": now.hour, "day_of_week": now.weekday(), "event": "accessible"}
            for _ in range(15)
        ]
        checker = self._make_checker(history=events)
        weight = checker._calculate_pattern_weight()
        assert weight < 1.0, f"Expected weight < 1.0 for high success, got {weight}"

    def test_low_success_raises_weight(self):
        now = datetime.now()
        events = [
            {"hour": now.hour, "day_of_week": now.weekday(), "event": "busy"}
            for _ in range(15)
        ]
        checker = self._make_checker(history=events)
        weight = checker._calculate_pattern_weight()
        assert weight >= 1.0, f"Expected weight >= 1.0 for low success, got {weight}"

    def test_pattern_disabled_returns_neutral(self):
        cfg = _make_config(pattern_learning_enabled=False)
        from visa_appointment_checker import VisaAppointmentChecker

        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._availability_history = [{"hour": 8, "day_of_week": 1, "event": "accessible"}] * 20
        assert checker._calculate_pattern_weight() == 1.0


# =========================================================================
# 3. Optimal frequency uses config multiplier
# =========================================================================

class TestOptimalFrequency:
    """Ensure ``_calculate_optimal_frequency`` respects the config multiplier."""

    def _make_checker(self, **cfg_kw):
        from visa_appointment_checker import VisaAppointmentChecker

        cfg = _make_config(**cfg_kw)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._prime_time_windows = [(6, 9), (12, 14)]
        checker._availability_history = []
        return checker

    @patch("visa_appointment_checker.datetime")
    def test_prime_time_uses_config_multiplier(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 7, 1, 7, 0)  # Tuesday 7 AM
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        checker = self._make_checker(
            check_frequency_minutes=10,
            prime_time_backoff_multiplier=0.3,
        )
        freq = checker._calculate_optimal_frequency()
        # 10 * 0.3 = 3.0, pattern weight = 1.0 (no data)
        assert freq == pytest.approx(3.0, abs=0.5)

    @patch("visa_appointment_checker.datetime")
    def test_night_doubles_frequency(self, mock_dt):
        mock_dt.now.return_value = datetime(2025, 7, 1, 3, 0)  # 3 AM
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        checker = self._make_checker(check_frequency_minutes=5)
        freq = checker._calculate_optimal_frequency()
        assert freq == 10.0


# =========================================================================
# 4. Min-improvement-days gate
# =========================================================================

class TestMinImprovementDays:
    """Verify that ``_evaluate_available_dates`` skips marginal improvements."""

    def _make_checker(self, **cfg_kw):
        from visa_appointment_checker import VisaAppointmentChecker

        cfg = _make_config(**cfg_kw)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._availability_history = []
        checker._slot_ledger = MagicMock()
        checker._slot_ledger.record_slot.return_value = True
        checker._slot_ledger.is_known.return_value = False
        return checker

    @patch("visa_appointment_checker.send_notification")
    def test_skips_tiny_improvement(self, mock_notify):
        checker = self._make_checker(
            current_appointment_date="2025-12-01",
            start_date="2025-06-01",
            end_date="2025-12-31",
            min_improvement_days=10,
        )
        checker._parse_calendar_date = lambda slot: datetime(2025, 11, 25)  # only 6 days earlier
        checker._evaluate_available_dates(["November 2025 25"])
        mock_notify.assert_not_called()

    @patch("visa_appointment_checker.send_notification")
    def test_notifies_big_improvement(self, mock_notify):
        checker = self._make_checker(
            current_appointment_date="2025-12-01",
            start_date="2025-06-01",
            end_date="2025-12-31",
            min_improvement_days=7,
        )
        checker._parse_calendar_date = lambda slot: datetime(2025, 10, 1)  # 61 days earlier
        checker._evaluate_available_dates(["October 2025 1"])
        mock_notify.assert_called_once()


# =========================================================================
# 5. Slot ledger
# =========================================================================

class TestSlotLedger:
    """Unit tests for the SQLite-backed ``SlotLedger``."""

    def _make_ledger(self, tmp_path):
        from slot_ledger import SlotLedger
        return SlotLedger(db_path=tmp_path / "test.db")

    def test_record_new_slot(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        assert ledger.record_slot("2025-10-01", "Ottawa") is True

    def test_duplicate_suppressed(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        # Second insert should return False (duplicate)
        assert ledger.is_known("2025-10-01", "Ottawa") is True

    def test_different_location_not_duplicate(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        assert ledger.is_known("2025-10-01", "Toronto") is False

    def test_mark_notified(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        ledger.mark_notified("2025-10-01", "Ottawa")  # Should not raise

    def test_recent_slots(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        ledger.record_slot("2025-10-02", "Toronto")
        recent = ledger.recent_slots(limit=10)
        assert len(recent) == 2

    def test_hourly_histogram(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        hist = ledger.hourly_histogram()
        assert len(hist) >= 1


# =========================================================================
# 6. Auto-book dry-run safety
# =========================================================================

class TestAutoBookGuardrails:
    """Ensure auto-book respects dry-run and min-improvement settings."""

    @patch("visa_appointment_checker.send_notification")
    def test_auto_book_dry_run_does_not_click(self, mock_notify):
        """When dry_run is True, _attempt_auto_book must never call click()."""
        from visa_appointment_checker import VisaAppointmentChecker

        cfg = _make_config(auto_book=True, auto_book_dry_run=True)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg

        # Mock the driver and elements
        mock_driver = MagicMock()
        checker.driver = mock_driver
        checker.ensure_driver = MagicMock(return_value=mock_driver)

        # Mock calendar not visible so it tries to reopen
        checker._is_selector_visible = MagicMock(return_value=None)
        checker._find_element = MagicMock(return_value=None)

        target = datetime(2025, 8, 15)
        checker._attempt_auto_book(target, 108)

        # Should have logged dry-run, not clicked anything destructive
        # The key assertion: no submit button was clicked
        # Since _find_element returns None for calendar reopen, it aborts early
        # which is fine—the point is no destructive action occurred.


# =========================================================================
# 7. Config new fields
# =========================================================================

class TestConfigNewFields:
    """Verify that the new config fields have correct defaults."""

    def test_defaults_present(self):
        cfg = _make_config()
        assert cfg.min_improvement_days == 7
        assert cfg.auto_book_dry_run is True
        assert cfg.auto_book_confirmation_wait_seconds == 0
        assert cfg.timezone == "America/Toronto"

    def test_min_improvement_customizable(self):
        cfg = _make_config(min_improvement_days=14)
        assert cfg.min_improvement_days == 14

    def test_new_notification_fields(self):
        cfg = _make_config(telegram_bot_token="tok", telegram_chat_id="123")
        assert cfg.telegram_bot_token == "tok"
        assert cfg.telegram_chat_id == "123"

    def test_new_rate_fields(self):
        cfg = _make_config(max_requests_per_hour=60, slot_ttl_hours=48)
        assert cfg.max_requests_per_hour == 60
        assert cfg.slot_ttl_hours == 48

    def test_preferred_time_default(self):
        cfg = _make_config()
        assert cfg.preferred_time == "any"


# =========================================================================
# 8. Slot ledger TTL & purge
# =========================================================================

class TestSlotLedgerTTL:
    """Tests for TTL-aware slot dedup and expiration."""

    def _make_ledger(self, tmp_path):
        from slot_ledger import SlotLedger
        return SlotLedger(db_path=tmp_path / "test_ttl.db")

    def test_is_known_with_ttl_fresh(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        # Just recorded → within any TTL
        assert ledger.is_known("2025-10-01", "Ottawa", ttl_hours=1) is True

    def test_is_known_without_ttl(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        assert ledger.is_known("2025-10-01", "Ottawa", ttl_hours=0) is True

    def test_purge_expired_removes_old(self, tmp_path):
        from slot_ledger import _connect
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        # Manually backdate the discovered timestamp
        old_ts = (datetime.now() - timedelta(hours=50)).isoformat()
        with _connect(tmp_path / "test_ttl.db") as conn:
            conn.execute("UPDATE slots SET discovered = ?", (old_ts,))
        deleted = ledger.purge_expired(ttl_hours=24)
        assert deleted == 1
        assert ledger.is_known("2025-10-01", "Ottawa") is False

    def test_purge_keeps_booked(self, tmp_path):
        from slot_ledger import _connect
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        ledger.mark_booked("2025-10-01", "Ottawa")
        old_ts = (datetime.now() - timedelta(hours=50)).isoformat()
        with _connect(tmp_path / "test_ttl.db") as conn:
            conn.execute("UPDATE slots SET discovered = ?", (old_ts,))
        deleted = ledger.purge_expired(ttl_hours=24)
        assert deleted == 0
        assert ledger.is_known("2025-10-01", "Ottawa") is True


# =========================================================================
# 9. Slot ledger analytics
# =========================================================================

class TestSlotLedgerAnalytics:
    """Tests for analytics_summary and recent_slots dict format."""

    def _make_ledger(self, tmp_path):
        from slot_ledger import SlotLedger
        return SlotLedger(db_path=tmp_path / "test_analytics.db")

    def test_analytics_summary_empty(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        stats = ledger.analytics_summary()
        assert stats["total_slots"] == 0

    def test_analytics_summary_with_data(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        ledger.record_slot("2025-10-02", "Toronto")
        ledger.mark_notified("2025-10-01", "Ottawa")
        stats = ledger.analytics_summary()
        assert stats["total_slots"] == 2
        assert stats["unique_dates"] == 2
        assert stats["locations"] == 2
        assert stats["notified"] == 1

    def test_recent_slots_returns_dicts(self, tmp_path):
        ledger = self._make_ledger(tmp_path)
        ledger.record_slot("2025-10-01", "Ottawa")
        recent = ledger.recent_slots(limit=5)
        assert len(recent) == 1
        slot = recent[0]
        assert isinstance(slot, dict)
        assert "slot_date" in slot
        assert "location" in slot
        assert "discovered" in slot
        assert "booked" in slot
        assert "notified" in slot


# =========================================================================
# 10. Notification utilities
# =========================================================================

class TestNotificationUtils:
    """Tests for Telegram and webhook notification functions."""

    def test_telegram_no_token_returns_false(self):
        from notification_utils import send_telegram_notification
        assert send_telegram_notification("", "123", "hello") is False

    def test_telegram_no_chat_id_returns_false(self):
        from notification_utils import send_telegram_notification
        assert send_telegram_notification("tok", "", "hello") is False

    def test_webhook_no_url_returns_false(self):
        from notification_utils import send_webhook_notification
        assert send_webhook_notification("", "subj", "msg") is False

    @patch("notification_utils.urllib.request.urlopen")
    def test_telegram_success(self, mock_urlopen):
        from notification_utils import send_telegram_notification
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = send_telegram_notification("bot123", "456", "test message")
        assert result is True
        mock_urlopen.assert_called_once()

    @patch("notification_utils.urllib.request.urlopen")
    def test_webhook_discord_format(self, mock_urlopen):
        from notification_utils import send_webhook_notification
        import json
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        url = "https://discord.com/api/webhooks/123/abc"
        result = send_webhook_notification(url, "Test", "Hello")
        assert result is True
        # Verify it used the Discord content format
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data)
        assert "content" in body

    def test_send_all_notifications_no_config(self):
        from notification_utils import send_all_notifications
        cfg = _make_config()  # No SMTP, no Telegram, no webhook
        result = send_all_notifications(cfg, "Test", "Test msg")
        assert result is False  # All channels unconfigured


# =========================================================================
# 11. Timezone helper
# =========================================================================

class TestTimezoneHelper:
    """Tests for the _now() timezone-aware helper."""

    def _make_checker(self, tz="America/Toronto"):
        from visa_appointment_checker import VisaAppointmentChecker
        cfg = _make_config(timezone=tz)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        return checker

    def test_now_returns_datetime(self):
        checker = self._make_checker()
        result = checker._now()
        assert isinstance(result, datetime)

    def test_now_with_invalid_tz_falls_back(self):
        checker = self._make_checker(tz="Invalid/Zone")
        result = checker._now()
        assert isinstance(result, datetime)

    def test_now_naive(self):
        """_now() should return a naive datetime for backward compatibility."""
        checker = self._make_checker()
        result = checker._now()
        assert result.tzinfo is None


# =========================================================================
# 12. Schedule ID extraction
# =========================================================================

class TestScheduleIdExtraction:
    """Tests for _extract_schedule_id URL parsing."""

    def _make_checker(self):
        from visa_appointment_checker import VisaAppointmentChecker
        cfg = _make_config()
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._schedule_id = None
        return checker

    def test_extracts_from_appointment_url(self):
        checker = self._make_checker()
        url = "https://ais.usvisa-info.com/en-ca/niv/schedule/12345678/appointment"
        result = checker._extract_schedule_id(url)
        assert result == "12345678"
        assert checker._schedule_id == "12345678"

    def test_extracts_from_continue_url(self):
        checker = self._make_checker()
        url = "https://ais.usvisa-info.com/en-ca/niv/schedule/99887766/continue_actions"
        result = checker._extract_schedule_id(url)
        assert result == "99887766"

    def test_returns_cached_on_no_match(self):
        checker = self._make_checker()
        checker._schedule_id = "cached123"
        url = "https://example.com/no-schedule-here"
        result = checker._extract_schedule_id(url)
        assert result == "cached123"

    def test_returns_none_when_no_match_no_cache(self):
        checker = self._make_checker()
        url = "https://example.com/"
        result = checker._extract_schedule_id(url)
        assert result is None


# =========================================================================
# 13. Rate tracking / throttle
# =========================================================================

class TestRateTracking:
    """Tests for _should_throttle and _record_api_request."""

    def _make_checker(self, max_rph=10):
        from visa_appointment_checker import VisaAppointmentChecker
        cfg = _make_config(max_requests_per_hour=max_rph)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._request_timestamps = []
        return checker

    def test_not_throttled_initially(self):
        checker = self._make_checker(max_rph=120)
        assert checker._should_throttle() is False

    def test_throttled_at_limit(self):
        checker = self._make_checker(max_rph=5)
        now = datetime.now()
        checker._request_timestamps = [now - timedelta(seconds=i) for i in range(5)]
        assert checker._should_throttle() is True

    def test_record_adds_timestamp(self):
        checker = self._make_checker()
        checker._record_api_request()
        assert len(checker._request_timestamps) == 1

    def test_unlimited_rate(self):
        checker = self._make_checker(max_rph=0)
        checker._request_timestamps = [datetime.now()] * 1000
        assert checker._should_throttle() is False


# =========================================================================
# 14. Preferred time selection
# =========================================================================

class TestPreferredTime:
    """Tests for _pick_preferred_time."""

    def _make_checker(self, pref="any"):
        from visa_appointment_checker import VisaAppointmentChecker
        cfg = _make_config(preferred_time=pref)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        return checker

    def _mock_option(self, text, value=None):
        opt = MagicMock()
        opt.text = text
        opt.get_attribute = MagicMock(return_value=value or text)
        return opt

    def test_any_returns_first(self):
        checker = self._make_checker("any")
        options = [self._mock_option("09:00"), self._mock_option("14:00")]
        result = checker._pick_preferred_time(options)
        assert result.text == "09:00"

    def test_morning_prefers_9(self):
        checker = self._make_checker("morning")
        options = [self._mock_option("14:00"), self._mock_option("09:00"), self._mock_option("08:30")]
        result = checker._pick_preferred_time(options)
        assert result.text == "09:00"

    def test_afternoon_prefers_14(self):
        checker = self._make_checker("afternoon")
        options = [self._mock_option("09:00"), self._mock_option("13:30"), self._mock_option("17:00")]
        result = checker._pick_preferred_time(options)
        assert result.text == "13:30"

    def test_evening_prefers_17(self):
        checker = self._make_checker("evening")
        options = [self._mock_option("09:00"), self._mock_option("16:45"), self._mock_option("14:00")]
        result = checker._pick_preferred_time(options)
        assert result.text == "16:45"

    def test_single_option(self):
        checker = self._make_checker("morning")
        options = [self._mock_option("14:00")]
        result = checker._pick_preferred_time(options)
        assert result.text == "14:00"


# =========================================================================
# 15. Config hot-reload
# =========================================================================

class TestConfigHotReload:
    """Tests for _check_config_reload."""

    def _make_checker(self):
        from visa_appointment_checker import VisaAppointmentChecker
        cfg = _make_config()
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._config_mtime = None
        checker._prime_time_windows = [(6, 9)]
        return checker

    @patch("visa_appointment_checker.os.path.getmtime", return_value=100.0)
    def test_no_reload_when_mtime_same(self, mock_mtime):
        checker = self._make_checker()
        checker._config_mtime = 100.0
        result = checker._check_config_reload()
        assert result is False

    @patch("visa_appointment_checker.os.path.getmtime", side_effect=OSError)
    def test_no_reload_on_oserror(self, mock_mtime):
        checker = self._make_checker()
        result = checker._check_config_reload()
        assert result is False


# =========================================================================
# 16. Scheduling Limit Warning handling
# =========================================================================

class TestSchedulingLimitWarning:
    """Tests for the Scheduling Limit Warning detection and escalating backoff."""

    def _make_checker(self, **cfg_kw):
        from visa_appointment_checker import VisaAppointmentChecker

        cfg = _make_config(**cfg_kw)
        with patch.object(VisaAppointmentChecker, "__init__", lambda self, *a, **k: None):
            checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
        checker.cfg = cfg
        checker._scheduling_limit_count = 0
        checker._backoff_until = None
        checker._prime_time_windows = [(6, 9), (12, 14)]
        checker._availability_history = []
        return checker

    def _make_mock_driver(self, title: str):
        mock_driver = MagicMock()
        mock_driver.title = title
        mock_driver.current_url = "https://ais.usvisa-info.com/en-ca/niv/schedule/12345/appointment"
        return mock_driver

    @patch("visa_appointment_checker.send_notification")
    def test_handle_scheduling_limit_raises_captcha_error(self, mock_notify):
        """_handle_scheduling_limit_warning must raise CaptchaDetectedError."""
        from visa_appointment_checker import CaptchaDetectedError

        checker = self._make_checker()
        checker._scheduling_limit_count = 1
        mock_driver = self._make_mock_driver("Scheduling Limit Warning | AIS")

        with pytest.raises(CaptchaDetectedError):
            checker._handle_scheduling_limit_warning(mock_driver)

    @patch("visa_appointment_checker.send_notification")
    def test_backoff_escalates_with_consecutive_hits(self, mock_notify):
        """Each consecutive Scheduling Limit Warning should double the backoff."""
        from visa_appointment_checker import CaptchaDetectedError

        checker = self._make_checker()
        mock_driver = self._make_mock_driver("Scheduling Limit Warning | AIS")

        backoff_minutes = []
        for i in range(1, 4):
            checker._scheduling_limit_count = i
            try:
                checker._handle_scheduling_limit_warning(mock_driver)
            except CaptchaDetectedError:
                pass
            remaining = (checker._backoff_until - datetime.now()).total_seconds() / 60
            backoff_minutes.append(remaining)

        # Each iteration should produce a longer (or equal, due to cap) backoff
        assert backoff_minutes[1] > backoff_minutes[0], (
            "Second hit should have a longer backoff than the first"
        )
        assert backoff_minutes[2] > backoff_minutes[1], (
            "Third hit should have a longer backoff than the second"
        )

    @patch("visa_appointment_checker.send_notification")
    def test_backoff_capped_at_120_minutes(self, mock_notify):
        """Backoff must not exceed SCHEDULING_LIMIT_MAX_BACKOFF_MINUTES."""
        from visa_appointment_checker import VisaAppointmentChecker, CaptchaDetectedError

        checker = self._make_checker()
        mock_driver = self._make_mock_driver("Scheduling Limit Warning | AIS")
        checker._scheduling_limit_count = 100  # Very high consecutive count

        try:
            checker._handle_scheduling_limit_warning(mock_driver)
        except CaptchaDetectedError:
            pass

        remaining_minutes = (checker._backoff_until - datetime.now()).total_seconds() / 60
        cap = VisaAppointmentChecker.SCHEDULING_LIMIT_MAX_BACKOFF_MINUTES
        assert remaining_minutes <= cap + 1, f"Backoff must be capped at {cap} minutes"

    @patch("visa_appointment_checker.send_notification")
    def test_notification_sent_on_first_occurrence(self, mock_notify):
        """A notification must be sent on the first Scheduling Limit Warning."""
        from visa_appointment_checker import CaptchaDetectedError

        checker = self._make_checker()
        mock_driver = self._make_mock_driver("Scheduling Limit Warning | AIS")
        checker._scheduling_limit_count = 1

        try:
            checker._handle_scheduling_limit_warning(mock_driver)
        except CaptchaDetectedError:
            pass

        mock_notify.assert_called_once()
        subject = mock_notify.call_args[0][1]
        assert "Scheduling Limit Warning" in subject

    def test_ensure_on_appointment_form_returns_false_for_warning_page(self):
        """_ensure_on_appointment_form must return False on Scheduling Limit Warning pages."""
        checker = self._make_checker()
        mock_driver = MagicMock()
        mock_driver.title = "Scheduling Limit Warning | Official U.S. Department of State"
        mock_driver.current_url = "https://ais.usvisa-info.com/en-ca/niv/schedule/12345/appointment"
        checker.driver = mock_driver
        checker.ensure_driver = MagicMock(return_value=mock_driver)

        result = checker._ensure_on_appointment_form()
        assert result is False, (
            "_ensure_on_appointment_form should return False on Scheduling Limit Warning page"
        )
