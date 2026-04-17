from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, Mapping


CONFIG_KEYS = [
    "EMAIL", "PASSWORD", "CURRENT_APPOINTMENT_DATE", "LOCATION",
    "COUNTRY_CODE", "SCHEDULE_ID", "FACILITY_ID",
    "START_DATE", "END_DATE", "CHECK_FREQUENCY_MINUTES",
    "BURST_MODE_ENABLED", "MULTI_LOCATION_CHECK", "BACKUP_LOCATIONS",
    "PRIME_HOURS_START", "PRIME_HOURS_END", "PRIME_TIME_BACKOFF_MULTIPLIER",
    "WEEKEND_FREQUENCY_MULTIPLIER", "PATTERN_LEARNING_ENABLED",
    "SMTP_SERVER", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
    "NOTIFY_EMAIL", "AUTO_BOOK", "AUTO_BOOK_DRY_RUN",
    "AUTO_BOOK_CONFIRMATION_WAIT_SECONDS", "MIN_IMPROVEMENT_DAYS",
    "TIMEZONE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "WEBHOOK_URL", "PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY",
    "SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL", "SENDGRID_TO_EMAIL",
    "PREFERRED_TIME", "MAX_REQUESTS_PER_HOUR", "MAX_API_REQUESTS_PER_HOUR",
    "MAX_UI_NAVIGATIONS_PER_HOUR", "SLOT_TTL_HOURS", "DRIVER_RESTART_CHECKS",
    "MAX_RETRY_ATTEMPTS", "SLEEP_JITTER_SECONDS", "TEST_MODE",
    "EXCLUDED_DATE_RANGES", "SAFETY_FIRST_MODE", "SAFETY_FIRST_MIN_INTERVAL_MINUTES",
    "AUDIO_ALERTS_ENABLED", "ACCOUNT_ROTATION_ENABLED", "ROTATION_ACCOUNTS",
    "ROTATION_INTERVAL_CHECKS", "VPN_PROVIDER", "VPN_CLI_PATH", "VPN_SERVER",
    "VPN_COUNTRY", "VPN_REQUIRE_CONNECTED", "VPN_ROTATE_ON_CAPTCHA",
    "VPN_RECONNECT_ON_NETWORK_ERROR", "VPN_MIN_SESSION_MINUTES",
]


BOOLEAN_KEYS = {
    "AUTO_BOOK", "BURST_MODE_ENABLED", "MULTI_LOCATION_CHECK",
    "PATTERN_LEARNING_ENABLED", "AUTO_BOOK_DRY_RUN", "TEST_MODE",
    "SAFETY_FIRST_MODE", "AUDIO_ALERTS_ENABLED", "ACCOUNT_ROTATION_ENABLED",
    "VPN_REQUIRE_CONNECTED", "VPN_ROTATE_ON_CAPTCHA", "VPN_RECONNECT_ON_NETWORK_ERROR",
}


DEFAULTS = {
    "COUNTRY_CODE": "en-ca",
    "SCHEDULE_ID": "",
    "FACILITY_ID": "",
    "CHECK_FREQUENCY_MINUTES": "3",
    "BURST_MODE_ENABLED": "True",
    "MULTI_LOCATION_CHECK": "True",
    "BACKUP_LOCATIONS": "Toronto,Montreal,Vancouver",
    "PRIME_HOURS_START": "6,12,17,22",
    "PRIME_HOURS_END": "9,14,19,1",
    "PRIME_TIME_BACKOFF_MULTIPLIER": "0.5",
    "WEEKEND_FREQUENCY_MULTIPLIER": "2.0",
    "PATTERN_LEARNING_ENABLED": "True",
    "SMTP_SERVER": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "AUTO_BOOK": "False",
    "AUTO_BOOK_DRY_RUN": "True",
    "AUTO_BOOK_CONFIRMATION_WAIT_SECONDS": "30",
    "MIN_IMPROVEMENT_DAYS": "7",
    "TIMEZONE": "America/Toronto",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "WEBHOOK_URL": "",
    "PUSHOVER_APP_TOKEN": "",
    "PUSHOVER_USER_KEY": "",
    "SENDGRID_API_KEY": "",
    "SENDGRID_FROM_EMAIL": "",
    "SENDGRID_TO_EMAIL": "",
    "PREFERRED_TIME": "any",
    "MAX_REQUESTS_PER_HOUR": "120",
    "MAX_API_REQUESTS_PER_HOUR": "120",
    "MAX_UI_NAVIGATIONS_PER_HOUR": "60",
    "SLOT_TTL_HOURS": "24",
    "DRIVER_RESTART_CHECKS": "50",
    "MAX_RETRY_ATTEMPTS": "2",
    "SLEEP_JITTER_SECONDS": "60",
    "TEST_MODE": "False",
    "EXCLUDED_DATE_RANGES": "",
    "SAFETY_FIRST_MODE": "False",
    "SAFETY_FIRST_MIN_INTERVAL_MINUTES": "10",
    "AUDIO_ALERTS_ENABLED": "False",
    "ACCOUNT_ROTATION_ENABLED": "False",
    "ROTATION_ACCOUNTS": "",
    "ROTATION_INTERVAL_CHECKS": "1",
    "VPN_PROVIDER": "none",
    "VPN_CLI_PATH": "protonvpn",
    "VPN_SERVER": "",
    "VPN_COUNTRY": "",
    "VPN_REQUIRE_CONNECTED": "False",
    "VPN_ROTATE_ON_CAPTCHA": "True",
    "VPN_RECONNECT_ON_NETWORK_ERROR": "True",
    "VPN_MIN_SESSION_MINUTES": "10",
}


class ConfigManager:
    """Central config reader/writer shared by CLI and Web UI flows."""

    def __init__(self, config_path: str = "config.ini", template_path: str = "config.ini.template") -> None:
        self.config_path = Path(config_path)
        self.template_path = Path(template_path)

    def load_parser(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        parser.optionxform = str

        if not self.config_path.exists():
            if self.template_path.exists():
                parser.read(self.template_path, encoding="utf-8")
            if "DEFAULT" not in parser:
                parser["DEFAULT"] = {}
            with self.config_path.open("w", encoding="utf-8") as handle:
                parser.write(handle)

        parser.read(self.config_path, encoding="utf-8")
        if "DEFAULT" not in parser:
            parser["DEFAULT"] = {}
        return parser

    def save_parser(self, parser: configparser.ConfigParser) -> None:
        with self.config_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)

    @staticmethod
    def get_case_insensitive(parser: configparser.ConfigParser, key: str, fallback: str = "") -> str:
        section = parser["DEFAULT"] if "DEFAULT" in parser else {}
        for existing_key, value in section.items():
            if existing_key.upper() == key.upper():
                return str(value)
        return fallback

    @staticmethod
    def set_case_insensitive(parser: configparser.ConfigParser, key: str, value: str) -> None:
        if "DEFAULT" not in parser:
            parser["DEFAULT"] = {}
        section = parser["DEFAULT"]
        for existing_key in list(section.keys()):
            if existing_key.upper() == key.upper():
                section[existing_key] = value
                return
        section[key.lower()] = value

    def save_updates(self, updates: Mapping[str, str]) -> None:
        parser = self.load_parser()
        for key, value in updates.items():
            self.set_case_insensitive(parser, key, value)
        self.save_parser(parser)

    def ui_values(self) -> Dict[str, str]:
        parser = self.load_parser()
        current: Dict[str, str] = {}
        for key in CONFIG_KEYS:
            current[key] = self.get_case_insensitive(parser, key, DEFAULTS.get(key, ""))
        return current
