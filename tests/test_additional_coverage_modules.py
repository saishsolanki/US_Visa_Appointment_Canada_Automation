from __future__ import annotations

import configparser
import json
import logging
import smtplib
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import browser_session
import config_wizard
import logging_utils
import notification_utils
import scheduling_utils
import selector_registry


def test_selector_registry_load_json_and_apply_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = {
        "EMAIL_SELECTORS": [
            {"by": "id", "value": "user_email_override"},
            {"by": "name", "value": "user_email"},
            {"by": "bad", "value": "ignored"},
        ],
        "IGNORED": "not-a-list",
    }
    registry_path = tmp_path / "selectors.yml"
    registry_path.write_text(json.dumps(content), encoding="utf-8")

    class Dummy:
        EMAIL_SELECTORS = [("id", "user_email")]

    monkeypatch.setattr(selector_registry, "_APPLIED_TARGETS", set())
    selector_registry.apply_selector_overrides(Dummy, str(registry_path))
    assert Dummy.EMAIL_SELECTORS[0][1] == "user_email_override"
    assert ("id", "user_email") in Dummy.EMAIL_SELECTORS

    # second apply should be idempotent
    before = list(Dummy.EMAIL_SELECTORS)
    selector_registry.apply_selector_overrides(Dummy, str(registry_path))
    assert Dummy.EMAIL_SELECTORS == before


def test_selector_registry_manual_yaml_like_parser(tmp_path: Path) -> None:
    registry_path = tmp_path / "selectors.yml"
    registry_path.write_text(
        """
EMAIL_SELECTORS:
  - by: ID
    value: user_email
PASSWORD_SELECTORS:
  - by: NAME
    value: user_password
""".strip(),
        encoding="utf-8",
    )
    parsed = selector_registry.load_selector_registry(str(registry_path))
    assert parsed["EMAIL_SELECTORS"][0][1] == "user_email"
    assert parsed["PASSWORD_SELECTORS"][0][1] == "user_password"


def test_compute_sleep_seconds_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(scheduling_utils.random, "randint", lambda a, b: 10)

    class _DT:
        @staticmethod
        def now() -> datetime:
            return now

    monkeypatch.setattr(scheduling_utils, "datetime", _DT)

    sleep, backoff = scheduling_utils.compute_sleep_seconds(
        base_minutes=5,
        optimal_minutes=2.0,
        dynamic_backoff_minutes=9,
        sleep_jitter_seconds=30,
        is_prime_time=True,
        backoff_until=now + timedelta(seconds=200),
    )
    assert sleep >= 200
    assert backoff is not None

    sleep2, backoff2 = scheduling_utils.compute_sleep_seconds(
        base_minutes=5,
        optimal_minutes=10.0,
        dynamic_backoff_minutes=8,
        sleep_jitter_seconds=0,
        is_prime_time=False,
        backoff_until=now - timedelta(seconds=1),
    )
    assert sleep2 >= 30
    assert backoff2 is None


def test_json_log_formatter_emits_expected_fields() -> None:
    formatter = logging_utils.JsonLogFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello"
    assert "timestamp" in payload


def test_configure_logging_uses_fallback_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(logging_utils, "FALLBACK_BASE_DIR", fallback)

    def _raise_oserror(*args, **kwargs):
        raise OSError("no write")

    monkeypatch.setattr(logging_utils, "LOG_DIR", SimpleNamespace(mkdir=_raise_oserror))
    monkeypatch.setattr(logging_utils, "ARTIFACTS_DIR", SimpleNamespace(mkdir=lambda *a, **k: None))
    monkeypatch.setattr(logging_utils, "RotatingFileHandler", lambda *a, **k: MagicMock())
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)

    logging_utils.configure_logging(debug=True, json_logs=True)
    assert str(logging_utils.LOG_PATH).startswith(str(fallback))
    assert str(logging_utils.ARTIFACTS_DIR).startswith(str(fallback))


def test_send_notification_success_and_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(
        smtp_server="smtp.gmail.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_pass="app-pass",
        notify_email="notify@example.com",
        is_smtp_configured=lambda: True,
    )

    smtp_mock = MagicMock()
    smtp_mock.__enter__.return_value = smtp_mock
    smtp_mock.__exit__.return_value = False
    monkeypatch.setattr(notification_utils.smtplib, "SMTP", lambda *a, **k: smtp_mock)
    assert notification_utils.send_notification(cfg, "Subject", "Body") is True
    smtp_mock.sendmail.assert_called_once()

    def _raise_auth(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad auth")

    monkeypatch.setattr(notification_utils.smtplib, "SMTP", _raise_auth)
    assert notification_utils.send_notification(cfg, "Subject", "Body") is False


def test_send_webhook_slack_non_2xx_and_telegram_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Slack webhook path + non-2xx response
    bad_resp = MagicMock()
    bad_resp.status = 500
    bad_resp.__enter__.return_value = bad_resp
    bad_resp.__exit__.return_value = False
    opener = MagicMock(return_value=bad_resp)
    monkeypatch.setattr(notification_utils.urllib.request, "urlopen", opener)
    assert notification_utils.send_webhook_notification("https://hooks.slack.com/services/x", "S", "M") is False

    req = opener.call_args[0][0]
    body = json.loads(req.data)
    assert "text" in body

    # Telegram HTTP error path
    def _raise_http(*args, **kwargs):
        raise notification_utils.urllib.error.HTTPError(
            url="https://api.telegram.org", code=400, msg="bad", hdrs=None, fp=None
        )

    monkeypatch.setattr(notification_utils.urllib.request, "urlopen", _raise_http)
    assert notification_utils.send_telegram_notification("bot", "chat", "hello") is False


def test_send_all_notifications_aggregates_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(
        telegram_bot_token="tok",
        telegram_chat_id="chat",
        webhook_url="https://example.com/hook",
    )
    monkeypatch.setattr(notification_utils, "send_notification", lambda *a, **k: False)
    monkeypatch.setattr(notification_utils, "send_telegram_notification", lambda *a, **k: True)
    monkeypatch.setattr(notification_utils, "send_webhook_notification", lambda *a, **k: False)
    assert notification_utils.send_all_notifications(cfg, "Sub", "Msg") is True


def test_build_chrome_options_modes_and_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIMAL_BROWSER", raising=False)
    monkeypatch.delenv("CHECKER_USER_AGENT", raising=False)

    opts = browser_session.build_chrome_options(headless=True, mode="weird-mode")
    args = opts.arguments
    assert "--headless=new" in args
    # invalid mode falls back to balanced => no minimal-only args
    assert "--disable-images" not in args

    monkeypatch.setenv("MINIMAL_BROWSER", "true")
    opts_min = browser_session.build_chrome_options(headless=False, mode="balanced")
    assert "--disable-images" in opts_min.arguments
    prefs = opts_min.experimental_options["prefs"]
    assert prefs["profile.default_content_setting_values"]["images"] == 2


def test_config_wizard_writes_expected_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = tmp_path / "template.ini"
    template.write_text(
        """
[DEFAULT]
email = old@example.com
smtp_server = smtp.gmail.com
smtp_port = 587
""".strip(),
        encoding="utf-8",
    )
    out_cfg = tmp_path / "config.ini"

    inputs = iter(
        [
            "new@example.com",  # EMAIL
            "2026-12-01",  # CURRENT_APPOINTMENT_DATE
            "Ottawa",  # LOCATION
            "2026-10-01",  # START_DATE
            "2026-12-31",  # END_DATE
            "5",  # CHECK_FREQUENCY_MINUTES
            "2",  # provider Outlook
            "smtp-user@example.com",  # SMTP_USER
            "",  # NOTIFY_EMAIL -> auto use smtp_user
            "False",  # AUTO_BOOK
        ]
    )
    secrets = iter(["secret-pass", "smtp-pass"])

    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    monkeypatch.setattr(config_wizard, "getpass", lambda *_: next(secrets))

    config_wizard.run_cli_setup_wizard(str(out_cfg), str(template))

    parser = configparser.ConfigParser()
    parser.read(out_cfg)
    defaults = parser["DEFAULT"]
    assert defaults["email"] == "new@example.com"
    assert defaults["smtp_server"] == "smtp.office365.com"
    assert defaults["smtp_port"] == "587"
    assert defaults["notify_email"] == "smtp-user@example.com"
