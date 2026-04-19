from __future__ import annotations

import configparser
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_manager
import visa_appointment_checker as checker_module


def _write_min_config(path: Path) -> Path:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser["DEFAULT"] = {
        "EMAIL": "user@example.com",
        "PASSWORD": "secret-pass",
        "CURRENT_APPOINTMENT_DATE": "2026-12-15",
        "LOCATION": "Ottawa - U.S. Embassy",
        "START_DATE": "2026-10-01",
        "END_DATE": "2026-12-31",
        "CHECK_FREQUENCY_MINUTES": "5",
        "SMTP_SERVER": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "smtp-user@example.com",
        "SMTP_PASS": "smtp-pass",
        "NOTIFY_EMAIL": "notify@example.com",
        "AUTO_BOOK": "False",
    }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return path


def test_config_manager_migrates_new_keys(tmp_path: Path) -> None:
    config_path = _write_min_config(tmp_path / "config.ini")
    manager = config_manager.ConfigManager(config_path=str(config_path), template_path=str(config_path))

    parser = manager.load_parser()

    assert manager.get_case_insensitive(parser, "CONFIG_VERSION") == str(config_manager.CURRENT_CONFIG_VERSION)
    assert manager.get_case_insensitive(parser, "TEST_MODE_SEND_NOTIFICATIONS") == "False"
    assert manager.get_case_insensitive(parser, "SLOT_LEDGER_DB_PATH")


def test_send_notification_suppressed_in_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"count": 0}

    def _fake_send_all(cfg, subject: str, message: str) -> bool:
        called["count"] += 1
        return True

    monkeypatch.setattr(checker_module, "send_all_notifications", _fake_send_all)

    cfg = SimpleNamespace(test_mode=True, test_mode_send_notifications=False)
    assert checker_module.send_notification(cfg, "sub", "body") is False
    assert called["count"] == 0

    cfg2 = SimpleNamespace(test_mode=True, test_mode_send_notifications=True)
    assert checker_module.send_notification(cfg2, "sub", "body") is True
    assert called["count"] == 1


def test_startup_preflight_updates_ledger_path_when_fallback_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_path = tmp_path / "fallback" / "slot_ledger.db"

    monkeypatch.setattr(checker_module, "LOG_PATH", tmp_path / "logs" / "visa_checker.log")
    monkeypatch.setattr(checker_module, "ARTIFACTS_DIR", tmp_path / "artifacts")

    def _fake_init(path):
        return fallback_path, "using fallback"

    monkeypatch.setattr(checker_module, "initialize_slot_ledger_path", _fake_init)

    cfg = SimpleNamespace(slot_ledger_db_path=str(tmp_path / "primary" / "slot_ledger.db"))
    ok, messages = checker_module.run_startup_preflight(cfg)

    assert ok is True
    assert cfg.slot_ledger_db_path == str(fallback_path)
    assert any("using fallback" in message for message in messages)


def test_self_check_reports_success_with_mocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver_path = tmp_path / "chromedriver"
    driver_path.write_text("stub", encoding="utf-8")

    monkeypatch.setattr(checker_module, "run_startup_preflight", lambda cfg: (True, ["ok"]))
    monkeypatch.setattr(checker_module, "apply_selector_overrides", lambda cls, selectors_file: None)

    class _FakeManager:
        def install(self):
            return str(driver_path)

    monkeypatch.setattr(checker_module, "ChromeDriverManager", _FakeManager)

    class _FakeDriver:
        def quit(self):
            return None

    monkeypatch.setattr(checker_module.webdriver, "Chrome", lambda *a, **k: _FakeDriver())

    cfg = SimpleNamespace(
        ensure_runtime_credentials_ready=lambda: None,
        slot_ledger_db_path=str(tmp_path / "slot_ledger.db"),
    )

    assert checker_module.run_self_check(cfg, selectors_file="selectors.yml") is True
