from pathlib import Path
import sys
from typing import Dict, Optional

import pytest
from selenium.webdriver.common.by import By

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from visa_appointment_checker import CheckerConfig, VisaAppointmentChecker


def _write_config(path: Path, overrides: Optional[Dict[str, str]] = None) -> Path:
    values = {
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
    if overrides:
        values.update(overrides)
    content = "[DEFAULT]\n" + "\n".join(f"{k} = {v}" for k, v in values.items()) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def test_config_load_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAIL", raising=False)
    config_path = _write_config(tmp_path / "config.ini")
    cfg = CheckerConfig.load(str(config_path))
    assert cfg.email == "user@example.com"
    assert cfg.check_frequency_minutes == 5


def test_config_validation_error_is_clear(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path / "config.ini",
        {"CURRENT_APPOINTMENT_DATE": "12-15-2026", "CHECK_FREQUENCY_MINUTES": "0"},
    )
    with pytest.raises(ValueError, match="Invalid configuration"):
        CheckerConfig.load(str(config_path))


def test_login_selector_smoke() -> None:
    assert (By.ID, "user_email") in VisaAppointmentChecker.EMAIL_SELECTORS
    assert (By.ID, "user_password") in VisaAppointmentChecker.PASSWORD_SELECTORS
    assert any(
        selector[0] == By.NAME and selector[1] == "commit"
        for selector in VisaAppointmentChecker.SIGN_IN_SELECTORS
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("January 2025 15", "2025-01-15"),
        ("2025-09-25", "2025-09-25"),
        ("15 January 2025", "2025-01-15"),
    ],
)
def test_parse_calendar_date_supported_formats(raw: str, expected: str) -> None:
    checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
    parsed = checker._parse_calendar_date(raw)
    assert parsed is not None
    assert parsed.strftime("%Y-%m-%d") == expected


def test_parse_calendar_date_invalid_returns_none() -> None:
    checker = VisaAppointmentChecker.__new__(VisaAppointmentChecker)
    assert checker._parse_calendar_date("not-a-date") is None
