from __future__ import annotations

import configparser
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config_manager import ConfigManager, DEFAULTS
import web_ui


def _write_web_config(path: Path, overrides: dict[str, str] | None = None) -> Path:
    values = dict(DEFAULTS)
    values.update(
        {
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
            "MAX_API_REQUESTS_PER_HOUR": "222",
            "MAX_UI_NAVIGATIONS_PER_HOUR": "77",
            "VPN_MIN_SESSION_MINUTES": "33",
            "VPN_REQUIRE_CONNECTED": "True",
            "VPN_ROTATE_ON_CAPTCHA": "False",
            "VPN_RECONNECT_ON_NETWORK_ERROR": "True",
        }
    )
    if overrides:
        values.update(overrides)

    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser["DEFAULT"] = values
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return path


def _build_form_payload(current: dict[str, str]) -> dict[str, str]:
    # These keys are not represented in the current HTML form and should not be clobbered.
    omitted_non_form_keys = {
        "MAX_API_REQUESTS_PER_HOUR",
        "MAX_UI_NAVIGATIONS_PER_HOUR",
        "VPN_MIN_SESSION_MINUTES",
        "VPN_REQUIRE_CONNECTED",
        "VPN_ROTATE_ON_CAPTCHA",
        "VPN_RECONNECT_ON_NETWORK_ERROR",
    }

    payload: dict[str, str] = {}
    for key, value in current.items():
        if key in omitted_non_form_keys:
            continue

        if key in web_ui.BOOLEAN_KEYS:
            # Browsers only send checkbox fields when checked.
            if key in web_ui._FORM_BOOLEAN_KEYS and value == "True":
                payload[key] = "on"
            continue

        payload[key] = value

    return payload


def test_web_ui_preserves_non_form_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_web_config(tmp_path / "config.ini")
    manager = ConfigManager(config_path=str(config_path), template_path=str(config_path))

    monkeypatch.setattr(web_ui, "CONFIG_MANAGER", manager)
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    client = web_ui.app.test_client()
    before = manager.ui_values()
    payload = _build_form_payload(before)

    response = client.post("/", data=payload, follow_redirects=False)
    assert response.status_code == 302

    after = manager.ui_values()
    assert after["MAX_API_REQUESTS_PER_HOUR"] == before["MAX_API_REQUESTS_PER_HOUR"]
    assert after["MAX_UI_NAVIGATIONS_PER_HOUR"] == before["MAX_UI_NAVIGATIONS_PER_HOUR"]
    assert after["VPN_MIN_SESSION_MINUTES"] == before["VPN_MIN_SESSION_MINUTES"]
    assert after["VPN_REQUIRE_CONNECTED"] == before["VPN_REQUIRE_CONNECTED"]
    assert after["VPN_ROTATE_ON_CAPTCHA"] == before["VPN_ROTATE_ON_CAPTCHA"]
    assert after["VPN_RECONNECT_ON_NETWORK_ERROR"] == before["VPN_RECONNECT_ON_NETWORK_ERROR"]


def test_web_ui_rejects_invalid_date_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_web_config(tmp_path / "config.ini")
    manager = ConfigManager(config_path=str(config_path), template_path=str(config_path))

    monkeypatch.setattr(web_ui, "CONFIG_MANAGER", manager)
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    client = web_ui.app.test_client()
    before = manager.ui_values()
    payload = _build_form_payload(before)
    payload["START_DATE"] = "2026-12-31"
    payload["END_DATE"] = "2026-01-01"

    response = client.post("/", data=payload, follow_redirects=True)
    assert response.status_code == 200
    assert b"Configuration not saved" in response.data

    after = manager.ui_values()
    assert after["START_DATE"] == before["START_DATE"]
    assert after["END_DATE"] == before["END_DATE"]
