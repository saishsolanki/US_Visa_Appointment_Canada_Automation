import argparse
import configparser
import json
import logging
import os
import random
import re
import smtplib
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc,assignment]

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)

from webdriver_manager.chrome import ChromeDriverManager
from browser_session import build_chrome_options
from config_wizard import run_cli_setup_wizard as run_cli_setup_wizard_external
from logging_utils import LOG_PATH, ARTIFACTS_DIR, configure_logging
from notification_utils import send_all_notifications
from scheduling_utils import compute_sleep_seconds as compute_sleep_seconds_external
from selector_registry import apply_selector_overrides
from slot_ledger import SlotLedger
from vpn_utils import ProtonVpnManager

# Keep webdriver-manager quiet unless user overrides
os.environ.setdefault("WDM_LOG_LEVEL", "0")

if sys.platform == "win32":
    try:
        import winsound  # type: ignore[import-not-found]
    except ImportError:
        winsound = None
else:
    winsound = None

Selector = Tuple[str, str]


class CaptchaDetectedError(RuntimeError):
    """Raised when the AIS site presents a CAPTCHA that blocks automation."""


class AccountLockedError(RuntimeError):
    """Raised when the AIS site reports the account is temporarily locked."""

    def __init__(self, message: str, unlock_at: Optional[datetime] = None) -> None:
        super().__init__(message)
        self.unlock_at = unlock_at


class UiRateLimitError(RuntimeError):
    """Raised when UI navigation rate limits are reached."""


def run_cli_setup_wizard(config_path: str = "config.ini", template_path: str = "config.ini.template") -> None:
    run_cli_setup_wizard_external(config_path=config_path, template_path=template_path)


@dataclass
class CheckerConfig:
    email: str
    password: str
    current_appointment_date: str
    location: str
    start_date: str
    end_date: str
    check_frequency_minutes: int
    smtp_server: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    notify_email: str
    country_code: str
    schedule_id: str
    facility_id: str
    auto_book: bool
    driver_restart_checks: int
    heartbeat_path: Optional[str]
    max_retry_attempts: int
    retry_backoff_seconds: int
    sleep_jitter_seconds: int
    busy_backoff_min_minutes: int
    busy_backoff_max_minutes: int
    abort_on_captcha: bool
    # Strategic optimization settings
    burst_mode_enabled: bool
    multi_location_check: bool
    backup_locations: str
    prime_hours_start: str
    prime_hours_end: str
    prime_time_backoff_multiplier: float
    weekend_frequency_multiplier: float
    pattern_learning_enabled: bool
    # Auto-book guardrails
    min_improvement_days: int
    auto_book_dry_run: bool
    auto_book_confirmation_wait_seconds: int
    timezone: str
    # Notification channels
    telegram_bot_token: str
    telegram_chat_id: str
    webhook_url: str
    pushover_app_token: str
    pushover_user_key: str
    sendgrid_api_key: str
    sendgrid_from_email: str
    sendgrid_to_email: str
    # Time & rate preferences
    preferred_time: str
    max_requests_per_hour: int
    max_api_requests_per_hour: int
    max_ui_navigations_per_hour: int
    slot_ttl_hours: int
    # Safety and advanced behaviors
    test_mode: bool
    excluded_date_ranges: str
    safety_first_mode: bool
    safety_first_min_interval_minutes: int
    audio_alerts_enabled: bool
    account_rotation_enabled: bool
    rotation_accounts: str
    rotation_interval_checks: int
    # VPN integration
    vpn_provider: str
    vpn_cli_path: str
    vpn_server: str
    vpn_country: str
    vpn_require_connected: bool
    vpn_rotate_on_captcha: bool
    vpn_reconnect_on_network_error: bool
    vpn_min_session_minutes: int

    @classmethod
    def load(cls, path: str = "config.ini") -> "CheckerConfig":
        parser = configparser.ConfigParser()
        parser.optionxform = str

        if not parser.read(path):
            raise FileNotFoundError(
                f"Unable to load configuration. Expected file at '{path}'. "
                "Run '--setup', configure.sh, the installer, or the web UI to create one."
            )

        if "DEFAULT" not in parser:
            raise KeyError("Configuration missing DEFAULT section.")

        raw_defaults = {k.upper(): v for k, v in parser["DEFAULT"].items()}

        required = [
            "EMAIL",
            "PASSWORD",
            "CURRENT_APPOINTMENT_DATE",
            "LOCATION",
            "START_DATE",
            "END_DATE",
            "CHECK_FREQUENCY_MINUTES",
            "SMTP_SERVER",
            "SMTP_PORT",
            "SMTP_USER",
            "SMTP_PASS",
            "NOTIFY_EMAIL",
            "AUTO_BOOK",
        ]

        missing = [key for key in required if key not in raw_defaults and os.getenv(key) is None]
        if missing:
            raise KeyError(
                "Configuration missing required keys: " + ", ".join(sorted(missing))
            )

        def _get(key: str, fallback: Optional[str] = None) -> str:
            value = os.getenv(key, raw_defaults.get(key, fallback))
            if value is None:
                raise KeyError(f"Missing configuration value for {key}")
            return str(value).strip()

        def _to_bool(value: str) -> bool:
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        def _get_int(key: str, fallback: Optional[int] = None) -> int:
            try:
                return int(_get(key, str(fallback) if fallback is not None else None))
            except ValueError as exc:  # noqa: B904
                raise ValueError(f"{key} must be an integer") from exc

        frequency = _get_int("CHECK_FREQUENCY_MINUTES", 5)
        smtp_port = _get_int("SMTP_PORT", 587)
        current_appointment_date = _get("CURRENT_APPOINTMENT_DATE")
        start_date = _get("START_DATE")
        end_date = _get("END_DATE")

        validation_errors: List[str] = []
        try:
            datetime.strptime(current_appointment_date, "%Y-%m-%d")
        except ValueError:
            validation_errors.append("CURRENT_APPOINTMENT_DATE must be formatted as YYYY-MM-DD")
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            if start_dt > end_dt:
                validation_errors.append("START_DATE must be earlier than or equal to END_DATE")
        except ValueError:
            validation_errors.append("START_DATE and END_DATE must be formatted as YYYY-MM-DD")
        if frequency < 1:
            validation_errors.append("CHECK_FREQUENCY_MINUTES must be greater than or equal to 1")
        if not 1 <= smtp_port <= 65535:
            validation_errors.append("SMTP_PORT must be between 1 and 65535")
        if validation_errors:
            raise ValueError("Invalid configuration:\n- " + "\n- ".join(validation_errors))

        def _get_float(key: str, fallback: Optional[float] = None) -> float:
            try:
                return float(_get(key, str(fallback) if fallback is not None else None))
            except ValueError as exc:  # noqa: B904
                raise ValueError(f"{key} must be a float") from exc

        return cls(
            email=_get("EMAIL"),
            password=_get("PASSWORD"),
            current_appointment_date=current_appointment_date,
            location=_get("LOCATION"),
            start_date=start_date,
            end_date=end_date,
            check_frequency_minutes=frequency,
            smtp_server=_get("SMTP_SERVER", "smtp.gmail.com"),
            smtp_port=smtp_port,
            smtp_user=_get("SMTP_USER"),
            smtp_pass=_get("SMTP_PASS"),
            notify_email=_get("NOTIFY_EMAIL"),
            country_code=_get("COUNTRY_CODE", "en-ca"),
            schedule_id=_get("SCHEDULE_ID", ""),
            facility_id=_get("FACILITY_ID", ""),
            auto_book=_to_bool(_get("AUTO_BOOK", "False")),
            driver_restart_checks=max(1, _get_int("DRIVER_RESTART_CHECKS", 50)),  # Increased default
            heartbeat_path=os.getenv("HEARTBEAT_PATH", raw_defaults.get("HEARTBEAT_PATH")),
            max_retry_attempts=max(1, _get_int("MAX_RETRY_ATTEMPTS", 2)),  # Reduced default
            retry_backoff_seconds=max(1, _get_int("RETRY_BACKOFF_SECONDS", 5)),
            sleep_jitter_seconds=max(0, _get_int("SLEEP_JITTER_SECONDS", 15)),
            busy_backoff_min_minutes=max(1, _get_int("BUSY_BACKOFF_MIN_MINUTES", 10)),
            busy_backoff_max_minutes=max(1, _get_int("BUSY_BACKOFF_MAX_MINUTES", 15)),
            abort_on_captcha=_to_bool(_get("ABORT_ON_CAPTCHA", "False")),
            # Strategic optimization settings
            burst_mode_enabled=_to_bool(_get("BURST_MODE_ENABLED", "True")),
            multi_location_check=_to_bool(_get("MULTI_LOCATION_CHECK", "True")),
            backup_locations=_get("BACKUP_LOCATIONS", "Toronto,Montreal,Vancouver"),
            prime_hours_start=_get("PRIME_HOURS_START", "6,12,17,22"),
            prime_hours_end=_get("PRIME_HOURS_END", "9,14,19,1"),
            prime_time_backoff_multiplier=_get_float("PRIME_TIME_BACKOFF_MULTIPLIER", 0.5),
            weekend_frequency_multiplier=_get_float("WEEKEND_FREQUENCY_MULTIPLIER", 2.0),
            pattern_learning_enabled=_to_bool(_get("PATTERN_LEARNING_ENABLED", "True")),
            # Auto-book guardrails
            min_improvement_days=max(1, _get_int("MIN_IMPROVEMENT_DAYS", 2)),
            auto_book_dry_run=_to_bool(_get("AUTO_BOOK_DRY_RUN", "True")),
            auto_book_confirmation_wait_seconds=max(0, _get_int("AUTO_BOOK_CONFIRMATION_WAIT_SECONDS", 10)),
            timezone=_get("TIMEZONE", "America/Toronto"),
            # Notification channels
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
            webhook_url=_get("WEBHOOK_URL", ""),
            pushover_app_token=_get("PUSHOVER_APP_TOKEN", ""),
            pushover_user_key=_get("PUSHOVER_USER_KEY", ""),
            sendgrid_api_key=_get("SENDGRID_API_KEY", ""),
            sendgrid_from_email=_get("SENDGRID_FROM_EMAIL", ""),
            sendgrid_to_email=_get("SENDGRID_TO_EMAIL", ""),
            # Time & rate preferences
            preferred_time=_get("PREFERRED_TIME", "any"),
            max_requests_per_hour=max(0, _get_int("MAX_REQUESTS_PER_HOUR", 120)),
            max_api_requests_per_hour=max(
                0,
                _get_int(
                    "MAX_API_REQUESTS_PER_HOUR",
                    _get_int("MAX_REQUESTS_PER_HOUR", 120),
                ),
            ),
            max_ui_navigations_per_hour=max(0, _get_int("MAX_UI_NAVIGATIONS_PER_HOUR", 60)),
            slot_ttl_hours=max(1, _get_int("SLOT_TTL_HOURS", 24)),
            # Safety and advanced behaviors
            test_mode=_to_bool(_get("TEST_MODE", "False")),
            excluded_date_ranges=_get("EXCLUDED_DATE_RANGES", ""),
            safety_first_mode=_to_bool(_get("SAFETY_FIRST_MODE", "False")),
            safety_first_min_interval_minutes=max(1, _get_int("SAFETY_FIRST_MIN_INTERVAL_MINUTES", 10)),
            audio_alerts_enabled=_to_bool(_get("AUDIO_ALERTS_ENABLED", "False")),
            account_rotation_enabled=_to_bool(_get("ACCOUNT_ROTATION_ENABLED", "False")),
            rotation_accounts=_get("ROTATION_ACCOUNTS", ""),
            rotation_interval_checks=max(1, _get_int("ROTATION_INTERVAL_CHECKS", 1)),
            # VPN integration
            vpn_provider=_get("VPN_PROVIDER", "none"),
            vpn_cli_path=_get("VPN_CLI_PATH", "protonvpn"),
            vpn_server=_get("VPN_SERVER", ""),
            vpn_country=_get("VPN_COUNTRY", ""),
            vpn_require_connected=_to_bool(_get("VPN_REQUIRE_CONNECTED", "False")),
            vpn_rotate_on_captcha=_to_bool(_get("VPN_ROTATE_ON_CAPTCHA", "True")),
            vpn_reconnect_on_network_error=_to_bool(_get("VPN_RECONNECT_ON_NETWORK_ERROR", "True")),
            vpn_min_session_minutes=max(0, _get_int("VPN_MIN_SESSION_MINUTES", 10)),
        )

    def is_smtp_configured(self) -> bool:
        if not self.smtp_user or not self.smtp_pass:
            return False
        user = self.smtp_user.lower()
        password = self.smtp_pass.lower()
        if "your_email" in user or "your_app_password" in password:
            return False
        return True

    @staticmethod
    def _mask(value: str, *, keep: int = 2) -> str:
        if not value:
            return ""
        if len(value) <= keep * 2:
            return value[0] + "***" if len(value) > 1 else "*"
        return f"{value[:keep]}***{value[-keep:]}"

    def masked_summary(self) -> str:
        tg = "on" if self.telegram_bot_token and self.telegram_chat_id else "off"
        wh = "on" if self.webhook_url else "off"
        po = "on" if self.pushover_app_token and self.pushover_user_key else "off"
        sg = "on" if self.sendgrid_api_key and self.sendgrid_from_email and self.sendgrid_to_email else "off"
        vpn = self.vpn_provider.lower()
        vpn_state = vpn if vpn and vpn != "none" else "off"
        return (
            f"email={self._mask(self.email)} | location={self.location} | "
            f"notify={self._mask(self.notify_email)} | auto_book={self.auto_book} | "
            f"telegram={tg} | webhook={wh} | pushover={po} | sendgrid={sg} | "
            f"country={self.country_code} | "
            f"vpn={vpn_state} | abort_on_captcha={self.abort_on_captcha}"
        )


def send_notification(cfg: CheckerConfig, subject: str, message: str) -> bool:
    return send_all_notifications(cfg, subject, message)


class ProgressReporter:
    """
    Background thread that sends periodic progress reports via email.
    
    Reports include:
    - Check statistics (success/failure counts)
    - Recent log entries
    - System health status
    - Log file attachment
    """
    
    def __init__(self, cfg: CheckerConfig, interval_hours: float = 6.0):
        self.cfg = cfg
        self.interval_hours = interval_hours
        self.last_report_time = datetime.now()
        self.start_time = datetime.now()
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._check_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._captcha_count = 0
        self._lock = threading.Lock()
        # Backoff tracking for failed email sends
        self._consecutive_send_failures = 0
        self._next_send_attempt: Optional[datetime] = None
    
    def start(self) -> None:
        """Start the background reporter thread."""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._report_loop, daemon=True, name="ProgressReporter")
        self.thread.start()
        logging.info("📊 Progress reporter started (interval: %.1f hours)", self.interval_hours)
    
    def stop(self) -> None:
        """Stop the reporter thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
            logging.info("📊 Progress reporter stopped")
    
    def record_check(self, success: bool, captcha: bool = False) -> None:
        """Thread-safe method to record check results."""
        with self._lock:
            self._check_count += 1
            if success:
                self._success_count += 1
            else:
                self._failure_count += 1
            if captcha:
                self._captcha_count += 1
    
    def _report_loop(self) -> None:
        """Background loop that sends reports at configured intervals.

        Implements progressive back-off when email sends fail (e.g. SMTP
        unreachable) so the log file doesn't get flooded with errors.
        """
        while self.running:
            # Sleep in small increments to allow quick shutdown
            for _ in range(60):  # Check every second for 1 minute
                if not self.running:
                    return
                time.sleep(1)
            
            elapsed = datetime.now() - self.last_report_time
            if elapsed >= timedelta(hours=self.interval_hours):
                # Honour send-failure backoff
                if self._next_send_attempt and datetime.now() < self._next_send_attempt:
                    continue

                try:
                    self._send_progress_report()
                    self.last_report_time = datetime.now()
                    # Reset backoff on success
                    self._consecutive_send_failures = 0
                    self._next_send_attempt = None
                except Exception as exc:
                    self._consecutive_send_failures += 1
                    # Exponential back-off: 5 min → 10 → 20 → 40 → … capped at 2 hours
                    backoff_minutes = min(120, 5 * (2 ** (self._consecutive_send_failures - 1)))
                    self._next_send_attempt = datetime.now() + timedelta(minutes=backoff_minutes)
                    logging.warning(
                        "Failed to send progress report (attempt #%d): %s — "
                        "next retry in %d minutes",
                        self._consecutive_send_failures,
                        exc,
                        backoff_minutes,
                    )
    
    def _get_stats(self) -> Dict[str, Any]:
        """Get current statistics thread-safely."""
        with self._lock:
            total = max(1, self._check_count)
            return {
                'total_checks': self._check_count,
                'successful_checks': self._success_count,
                'failed_checks': self._failure_count,
                'captcha_count': self._captcha_count,
                'success_rate': self._success_count / total,
            }
    
    def _format_uptime(self) -> str:
        """Format uptime in human-readable form."""
        elapsed = datetime.now() - self.start_time
        days = elapsed.days
        hours, remainder = divmod(elapsed.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    
    def _read_recent_logs(self, num_lines: int = 100) -> str:
        """Read the last N lines from the log file."""
        log_path = LOG_PATH
        try:
            if log_path.exists():
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                    return ''.join(lines[-num_lines:])
        except Exception as exc:
            return f"Error reading log file: {exc}"
        return "No log file found"
    
    def _extract_key_events(self, log_tail: str) -> List[str]:
        """Extract key events from recent logs."""
        keywords = [
            'available', 'appointment found', 'earlier date',
            'captcha', 'error', 'failed', 'session expired',
            'login successful', 'notification sent'
        ]
        events = []
        for line in log_tail.split('\n'):
            line_lower = line.lower()
            if any(kw in line_lower for kw in keywords):
                # Clean up the line
                line = line.strip()
                if line and len(line) < 200:  # Skip very long lines
                    events.append(line)
        return events[-15:]  # Last 15 key events
    
    def _send_progress_report(self) -> None:
        """Send email with progress summary and log attachment."""
        if not self.cfg.is_smtp_configured():
            logging.debug("SMTP not configured; skipping progress report")
            return
        
        stats = self._get_stats()
        log_tail = self._read_recent_logs(200)
        key_events = self._extract_key_events(log_tail)
        
        # Determine status emoji
        success_rate = stats['success_rate']
        if success_rate >= 0.9:
            status_emoji = "✅"
            status_text = "Excellent - Running smoothly"
        elif success_rate >= 0.7:
            status_emoji = "⚠️"
            status_text = "Good - Some failures detected"
        elif success_rate >= 0.5:
            status_emoji = "🟡"
            status_text = "Fair - Investigate errors"
        else:
            status_emoji = "❌"
            status_text = "Poor - Requires attention"
        
        subject = f"🤖 Visa Checker Report - {datetime.now().strftime('%Y-%m-%d %H:%M')} [{status_emoji}]"
        
        body = f"""
US Visa Appointment Checker - Progress Report
{'=' * 50}

{status_emoji} Status: {status_text}
📅 Report Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
⏱️  Uptime: {self._format_uptime()}

📊 STATISTICS (Since Last Report)
{'-' * 40}
  Total Checks:      {stats['total_checks']:,}
  ✅ Successful:     {stats['successful_checks']:,} ({stats['success_rate']:.1%})
  ❌ Failed:         {stats['failed_checks']:,}
  🤖 Captcha Blocks: {stats['captcha_count']:,}

📍 CONFIGURATION
{'-' * 40}
  Location:          {self.cfg.location}
  Current Appt:      {self.cfg.current_appointment_date}
  Target Range:      {self.cfg.start_date} to {self.cfg.end_date}
  Check Frequency:   ~{self.cfg.check_frequency_minutes} minutes
  Auto-book:         {'Enabled' if self.cfg.auto_book else 'Disabled'}

🔍 KEY EVENTS (Recent Activity)
{'-' * 40}
"""
        if key_events:
            for event in key_events:
                # Truncate long events
                if len(event) > 100:
                    event = event[:100] + "..."
                body += f"  • {event}\n"
        else:
            body += "  No significant events detected\n"
        
        body += f"""
📋 NEXT ACTIONS
{'-' * 40}
  • Next report in: {self.interval_hours:.1f} hours
  • View full logs: Check attached file or logs/visa_checker.log

{'=' * 50}
🐳 Running in: {'Docker' if os.path.exists('/.dockerenv') else 'Native mode'}
📝 Full log file attached below
"""
        
        self._send_email_with_attachment(subject, body)
        logging.info("📧 Progress report sent successfully")
    
    def _send_email_with_attachment(self, subject: str, body: str) -> None:
        """Send email with log file attached."""
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = self.cfg.smtp_user
        msg['To'] = self.cfg.notify_email
        
        # Attach body text
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        # Attach log file if it exists
        log_path = LOG_PATH
        if log_path.exists() and log_path.stat().st_size > 0:
            try:
                with open(log_path, 'rb') as f:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(f.read())
                
                encoders.encode_base64(part)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename=visa_checker_{datetime.now().strftime("%Y%m%d_%H%M")}.log'
                )
                msg.attach(part)
            except Exception as exc:
                logging.warning("Failed to attach log file: %s", exc)
        
        # Send email
        with smtplib.SMTP(self.cfg.smtp_server, self.cfg.smtp_port) as server:
            server.starttls()
            server.login(self.cfg.smtp_user, self.cfg.smtp_pass)
            server.sendmail(self.cfg.smtp_user, self.cfg.notify_email, msg.as_string())


class VisaAppointmentChecker:
    EMAIL_SELECTORS: List[Selector] = [
        (By.ID, "user_email"),
        (By.NAME, "user[email]"),
        (By.CSS_SELECTOR, "form input[type='email']"),
        (By.CSS_SELECTOR, "input[name*='email']"),
        (By.CSS_SELECTOR, "input[autocomplete='email']"),
        (
            By.XPATH,
            "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'email')]",
        ),
        (
            By.XPATH,
            "//input[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'email')]",
        ),
    ]

    PASSWORD_SELECTORS: List[Selector] = [
        (By.ID, "user_password"),
        (By.NAME, "user[password]"),
        (By.CSS_SELECTOR, "form input[type='password']"),
        (
            By.XPATH,
            "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'password')]",
        ),
        (
            By.XPATH,
            "//input[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'password')]",
        ),
    ]

    SIGN_IN_SELECTORS: List[Selector] = [
        (By.NAME, "commit"),
        (By.CSS_SELECTOR, "input[type='submit']"),
        (By.CSS_SELECTOR, "button[type='submit']"),
        (
            By.XPATH,
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]",
        ),
        (By.XPATH, "//input[@value='Sign In']"),
    ]

    COOKIE_SELECTORS: List[Selector] = [
        (By.ID, "onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, "button[aria-label='Accept all']"),
        (
            By.XPATH,
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]",
        ),
        (By.CSS_SELECTOR, "button[data-cookiebanner='accept']"),
    ]

    ALERT_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, ".alert"),
        (By.CSS_SELECTOR, "[role='alert']"),
        (By.CSS_SELECTOR, ".flash"),
        (By.CSS_SELECTOR, ".error, .errors"),
    ]

    PRIVACY_CHECKBOX_SELECTORS: List[Selector] = [
        (By.ID, "policy_confirmed"),
        (By.NAME, "policy_confirmed"),
        (By.CSS_SELECTOR, "input[name='policy_confirmed']"),
        (By.CSS_SELECTOR, "input[id='policy_confirmed']"),
        (
            By.XPATH,
            "//input[@type='checkbox' and (contains(@name, 'policy') or contains(@id, 'policy'))]",
        ),
        (
            By.XPATH,
            "//input[@type='checkbox' and contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'privacy')]",
        ),
    ]

    PRIVACY_LABEL_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, "label[for='policy_confirmed']"),
        # iCheck styled checkbox wrapper - click the div.icheckbox
        (By.CSS_SELECTOR, "div.icheckbox"),
        (By.CSS_SELECTOR, ".icheck-item"),
        (
            By.XPATH,
            "//label[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'privacy policy')]",
        ),
    ]

    GROUP_CONTINUE_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, "a.button.primary[href*='continue_actions']"),
        (
            By.XPATH,
            "//a[contains(@class, 'button') and contains(@href, 'continue_actions') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
        ),
    ]

    RESCHEDULE_TOGGLE_SELECTORS: List[Selector] = [
        (
            By.XPATH,
            "//a[contains(@class, 'accordion-title') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reschedule appointment')]",
        ),
    ]

    RESCHEDULE_BUTTON_SELECTORS: List[Selector] = [
        (
            By.XPATH,
            "//a[contains(@href, '/appointment') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reschedule')]",
        ),
        (By.CSS_SELECTOR, "a[href*='/appointment']"),
    ]

    APPOINTMENT_FORM_SELECTORS: List[Selector] = [
        (By.ID, "appointment-form"),
        (By.CSS_SELECTOR, "form#appointment-form"),
        (By.CSS_SELECTOR, "form[action*='appointment']"),
        (By.CSS_SELECTOR, "fieldset.fieldset legend"),
        (By.XPATH, "//legend[contains(text(), 'Consular Section Appointment')]"),
        (By.CSS_SELECTOR, "#consulate-appointment-fields"),
        (By.ID, "appointments_consulate_appointment_facility_id"),
    ]

    LOCATION_SELECTORS: List[Selector] = [
        (By.ID, "appointments_consulate_appointment_facility_id"),
        (By.CSS_SELECTOR, "select#appointments_consulate_appointment_facility_id"),
        (By.CSS_SELECTOR, "select[id*='consulate_appointment_facility']"),
        (By.NAME, "appointments[consulate_appointment][facility_id]"),
        (By.ID, "location"),
        (By.NAME, "location"),
        (By.CSS_SELECTOR, "select[name*='location']"),
    ]

    CONSULATE_BUSY_SELECTORS: List[Selector] = [
        (By.ID, "consulate_date_time_not_available"),
        (By.CSS_SELECTOR, "#consulate_date_time_not_available small"),
        (By.CSS_SELECTOR, ".display-none.error small"),
        (By.XPATH, "//div[@id='consulate_date_time_not_available' and contains(@style, 'display: block')]"),
    ]

    CONSULATE_DATE_INPUT_SELECTORS: List[Selector] = [
        (By.ID, "appointments_consulate_appointment_date"),
        (By.CSS_SELECTOR, "input[id*='consulate_appointment_date']"),
        (By.CSS_SELECTOR, "input.hasDatepicker[readonly='readonly']"),
        (By.CSS_SELECTOR, "input[name='appointments[consulate_appointment][date]']"),
    ]

    CONSULATE_TIME_SELECTORS: List[Selector] = [
        (By.ID, "appointments_consulate_appointment_time"),
        (By.CSS_SELECTOR, "select[id*='consulate_appointment_time']"),
        (By.CSS_SELECTOR, "select[name='appointments[consulate_appointment][time]']"),
    ]

    DATEPICKER_CONTAINER_SELECTORS: List[Selector] = [
        (By.ID, "ui-datepicker-div"),
        (By.CSS_SELECTOR, "#ui-datepicker-div"),
        (By.CSS_SELECTOR, ".ui-datepicker"),
    ]

    CALENDAR_ICON_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, "a[href='#select'] img.calendar_icon"),
        (By.CSS_SELECTOR, "img.calendar_icon"),
        (By.CSS_SELECTOR, "a[href='#select']"),
    ]
    
    # Facility ID mapping based on AIS portal (for backup/debugging)
    FACILITY_ID_MAP: Dict[str, str] = {
        "Calgary": "89",
        "Halifax": "90",
        "Montreal": "91",
        "Ottawa": "92",
        "Quebec City": "93",
        "Toronto": "94",
        "Vancouver": "95",
    }

    # Maximum backoff (minutes) applied when the Scheduling Limit Warning page is hit repeatedly
    SCHEDULING_LIMIT_MAX_BACKOFF_MINUTES: int = 120
    EXCLUSION_WINDOW_LIMIT: int = 9

    # Selectors for the Continue acknowledgment button on the Scheduling Limit Warning page
    WARNING_CONTINUE_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, "a.button.primary[href*='appointment']"),
        (By.CSS_SELECTOR, "input[type='submit'][value='Continue']"),
        (By.XPATH, "//input[@type='submit' and contains(@value, 'Continue')]"),
        (
            By.XPATH,
            "//a[contains(@class, 'button') and contains("
            "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
        ),
    ]

    def __init__(
        self,
        cfg: CheckerConfig,
        *,
        headless: bool = True,
        selectors_path: str = "selectors.yml",
    ) -> None:
        self.cfg = cfg
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self.driver_path: Optional[str] = None
        # Try webdriver-manager first; if offline, fall back to Selenium Manager
        try:
            self.driver_path = ChromeDriverManager().install()
        except Exception as exc:
            logging.warning(
                "webdriver-manager could not resolve driver (%s) — "
                "falling back to Selenium Manager (built-in)",
                exc,
            )
        self._last_error_signature: Optional[str] = None
        self._last_notification_time: Optional[datetime] = None
        self._appointment_base_url: Optional[str] = None
        self._backoff_until: Optional[datetime] = None
        self._checks_since_restart = 0
        self._heartbeat_path: Optional[Path]
        
        # Performance optimizations
        self._last_busy_check: Optional[datetime] = None
        self._busy_streak_count = 0
        self._adaptive_frequency = cfg.check_frequency_minutes
        self._cached_elements = {}
        self._last_session_validation: Optional[datetime] = None
        self._recent_results: List[int] = []  # Track last 10 check results
        self._metrics: Dict[str, List[float]] = {}  # Performance metrics per operation
        
        # Strategic optimization properties
        self._availability_history: List[Dict[str, Any]] = []
        self._pattern_file = Path("appointment_patterns.json")
        self._prime_time_windows: List[Tuple[int, int]] = []
        self._excluded_date_windows: List[Tuple[datetime, datetime]] = []
        self._burst_mode_active = False
        self._slot_ledger = SlotLedger()
        self._rotation_accounts: List[Tuple[str, str]] = []
        self._rotation_index = 0
        self._vpn_manager: Optional[ProtonVpnManager] = None

        self._refresh_portal_urls()

        # API fast-path (P1.1 / P1.2)
        seeded_schedule_id = (cfg.schedule_id or "").strip()
        self._schedule_id: Optional[str] = seeded_schedule_id or None
        self._api_session: Optional[Any] = None
        self._api_session_cookies_hash: Optional[int] = None

        # Rate tracking (P2.5)
        self._request_timestamps: List[datetime] = []
        self._ui_nav_timestamps: List[datetime] = []

        # Config hot-reload (P3.2)
        self._config_mtime: Optional[float] = None
        try:
            self._config_mtime = os.path.getmtime("config.ini")
        except OSError:
            pass

        # Network health tracking
        self._consecutive_network_errors = 0
        self._last_successful_network_time: Optional[datetime] = None
        self._network_backoff_until: Optional[datetime] = None
        self._account_lockout_until: Optional[datetime] = None

        # Scheduling limit warning tracking
        self._scheduling_limit_count = 0
        # Observability counters (Phase 5)
        self._warning_page_hits = 0
        self._continue_success_count = 0
        self._api_check_count = 0
        self._ui_check_count = 0

        apply_selector_overrides(self.__class__, selectors_path)
        
        # Initialize strategic components
        self._parse_prime_time_windows()
        self._parse_excluded_windows()
        self._initialize_account_rotation()
        self._init_vpn_manager()
        if cfg.pattern_learning_enabled:
            self._load_patterns()

        if self._schedule_id:
            logging.info("Using configured schedule_id seed: %s", self._schedule_id)
        
        if cfg.heartbeat_path:
            heartbeat_path = Path(cfg.heartbeat_path).expanduser()
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path = heartbeat_path
        else:
            self._heartbeat_path = None

    # ------------------------------------------------------------------
    # Driver lifecycle helpers
    # ------------------------------------------------------------------
    def ensure_driver(self) -> webdriver.Chrome:
        if self.driver is not None:
            return self.driver

        options = self._build_options()
        service = Service(self.driver_path) if self.driver_path else Service()

        try:
            driver = webdriver.Chrome(service=service, options=options)
        except WebDriverException as exc:
            logging.error("Failed to start Chrome driver: %s", exc)
            raise

        driver.set_page_load_timeout(90)
        driver.implicitly_wait(0)

        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});",
                },
            )
        except Exception:  # noqa: BLE001
            logging.debug("Unable to tweak navigator.webdriver; continuing anyway.")

        try:
            browser_version = driver.capabilities.get("browserVersion")
            driver_version = driver.capabilities.get("chrome", {}).get("chromedriverVersion", "")
            logging.info(
                "Active Chrome session: browser=%s | chromedriver=%s",
                browser_version,
                driver_version.split(" ", 1)[0] if driver_version else "unknown",
            )
        except Exception:  # noqa: BLE001
            logging.debug("Unable to read driver capabilities for version logging")

        self.driver = driver
        logging.info("Chrome driver initialized (headless=%s)", self.headless)
        return driver

    def reset_driver(self) -> None:
        self.quit_driver()
        self.driver = None
        self._appointment_base_url = None
        self._checks_since_restart = 0

    def quit_driver(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:  # noqa: BLE001
            logging.debug("Driver quit raised; ignoring to continue cleanup.")
        finally:
            self.driver = None
            self._appointment_base_url = None

    def _build_options(self) -> Options:
        return build_chrome_options(headless=self.headless)

    # ------------------------------------------------------------------
    # Strategic optimization methods
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        """Return current wall-clock time in the configured timezone (naive)."""
        if ZoneInfo is not None:
            try:
                tz = ZoneInfo(self.cfg.timezone)
                return datetime.now(tz).replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                pass
        return datetime.now()

    def _portal_url(self) -> str:
        return getattr(self, "_portal_base", "https://ais.usvisa-info.com/en-ca/niv")

    def _login_target(self) -> str:
        return getattr(self, "_login_url", f"{self._portal_url()}/users/sign_in")

    def _reschedule_targets(self) -> List[str]:
        targets = getattr(self, "_reschedule_urls", None)
        if isinstance(targets, list) and targets:
            return targets
        base = self._portal_url()
        return [f"{base}/schedule/", f"{base}/appointment", f"{base}/"]

    def _refresh_portal_urls(self) -> None:
        country_code = (self.cfg.country_code or "en-ca").strip().lower()
        if not re.fullmatch(r"[a-z]{2}-[a-z]{2}", country_code):
            logging.warning("Invalid COUNTRY_CODE '%s'; defaulting to en-ca", country_code)
            country_code = "en-ca"
        self.cfg.country_code = country_code
        self._portal_base = f"https://ais.usvisa-info.com/{country_code}/niv"
        self._login_url = f"{self._portal_base}/users/sign_in"
        self._reschedule_urls = [
            f"{self._portal_base}/schedule/",
            f"{self._portal_base}/appointment",
            f"{self._portal_base}/",
        ]

    def _extract_schedule_id(self, url: str) -> Optional[str]:
        """Extract schedule_id from AIS URL pattern /schedule/{id}/"""
        match = re.search(r"/schedule/(\d+)", url)
        if match:
            sid = match.group(1)
            if self._schedule_id != sid:
                self._schedule_id = sid
                logging.info("Captured schedule_id: %s", sid)
            return sid
        return self._schedule_id

    def _parse_excluded_windows(self) -> None:
        """Parse excluded date windows from config as semicolon-separated ranges.

        Accepted entry formats:
        - YYYY-MM-DD:YYYY-MM-DD
        - YYYY-MM-DD to YYYY-MM-DD

        At most ``EXCLUSION_WINDOW_LIMIT`` windows are used to keep parsing
        predictable and align with common user scheduling use cases.
        """
        self._excluded_date_windows = []
        raw = (self.cfg.excluded_date_ranges or "").strip()
        if not raw:
            return

        chunks = [c.strip() for c in raw.replace("\n", ";").split(";") if c.strip()]
        if len(chunks) > self.EXCLUSION_WINDOW_LIMIT:
            dropped = chunks[self.EXCLUSION_WINDOW_LIMIT:]
            chunks = chunks[:self.EXCLUSION_WINDOW_LIMIT]
            logging.warning("Only first %d exclusion windows are used", self.EXCLUSION_WINDOW_LIMIT)
            logging.warning("Dropped exclusion windows: %s", dropped)

        for chunk in chunks:
            token = chunk.replace(" to ", ":")
            if ":" not in token:
                logging.warning("Skipping invalid exclusion window: %s", chunk)
                continue
            start_raw, end_raw = [part.strip() for part in token.split(":", 1)]
            try:
                start_dt = datetime.strptime(start_raw, "%Y-%m-%d")
                end_dt = datetime.strptime(end_raw, "%Y-%m-%d")
            except ValueError:
                logging.warning("Skipping invalid exclusion date format: %s", chunk)
                continue
            if start_dt > end_dt:
                start_dt, end_dt = end_dt, start_dt
            self._excluded_date_windows.append((start_dt, end_dt))

        if self._excluded_date_windows:
            logging.info("Configured %d exclusion window(s)", len(self._excluded_date_windows))

    def _is_excluded_date(self, date_value: datetime) -> bool:
        if not hasattr(self, "_excluded_date_windows"):
            self._excluded_date_windows = []
        for start_dt, end_dt in self._excluded_date_windows:
            if start_dt <= date_value <= end_dt:
                return True
        return False

    def _initialize_account_rotation(self) -> None:
        self._rotation_accounts = []
        primary = (self.cfg.email.strip(), self.cfg.password)
        if primary[0] and primary[1]:
            self._rotation_accounts.append(primary)

        raw = (self.cfg.rotation_accounts or "").strip()
        if raw:
            for entry in [e.strip() for e in raw.split(";") if e.strip()]:
                if "|" in entry:
                    email, pwd = entry.split("|", 1)
                elif ":" in entry:
                    email, pwd = entry.split(":", 1)
                else:
                    logging.warning("Skipping invalid rotation account entry: %s", entry)
                    continue
                pair = (email.strip(), pwd.strip())
                if pair[0] and pair[1] and pair not in self._rotation_accounts:
                    self._rotation_accounts.append(pair)

        self._rotation_index = 0
        if self.cfg.account_rotation_enabled and len(self._rotation_accounts) > 1:
            logging.info("Account rotation enabled with %d accounts", len(self._rotation_accounts))

    def _init_vpn_manager(self) -> None:
        provider = (self.cfg.vpn_provider or "").strip().lower()
        if provider == "protonvpn":
            self._vpn_manager = ProtonVpnManager(
                cli_path=self.cfg.vpn_cli_path,
                server=self.cfg.vpn_server,
                country=self.cfg.vpn_country,
                require_connected=self.cfg.vpn_require_connected,
                min_session_minutes=self.cfg.vpn_min_session_minutes,
                reconnect_on_network_error=self.cfg.vpn_reconnect_on_network_error,
                rotate_on_captcha=self.cfg.vpn_rotate_on_captcha,
            )
            logging.info(
                "Proton VPN control enabled (server=%s, country=%s, require_connected=%s)",
                self.cfg.vpn_server or "fastest",
                self.cfg.vpn_country or "auto",
                self.cfg.vpn_require_connected,
            )
        else:
            self._vpn_manager = None

    def _rotate_account_if_needed(self, check_count: int) -> None:
        if not self.cfg.account_rotation_enabled:
            return
        if len(self._rotation_accounts) < 2:
            return
        if check_count % max(1, self.cfg.rotation_interval_checks) != 0:
            return

        self._rotation_index = (self._rotation_index + 1) % len(self._rotation_accounts)
        next_email, next_password = self._rotation_accounts[self._rotation_index]
        self.cfg.email = next_email
        self.cfg.password = next_password
        self._api_session = None
        self._api_session_cookies_hash = None
        self.reset_driver()
        logging.info("Rotated active AIS account to %s", self.cfg._mask(next_email))

    def _audio_alert(self, reason: str) -> None:
        if not self.cfg.audio_alerts_enabled:
            return
        try:
            if winsound is not None:
                winsound.MessageBeep(0x30)
            elif sys.stdout.isatty():
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        logging.info("Audio alert triggered: %s", reason)

    def _parse_prime_time_windows(self) -> None:
        """Parse prime time configuration into time windows"""
        try:
            start_hours = [int(h.strip()) for h in self.cfg.prime_hours_start.split(',')]
            end_hours = [int(h.strip()) for h in self.cfg.prime_hours_end.split(',')]
            
            self._prime_time_windows = list(zip(start_hours, end_hours))
            logging.info("Prime time windows configured: %s", self._prime_time_windows)
        except Exception as exc:
            logging.warning("Invalid prime time configuration, using defaults: %s", exc)
            self._prime_time_windows = [(6, 9), (12, 14), (17, 19), (22, 1)]

    def _is_prime_time(self) -> bool:
        """Check if current time falls in optimal checking windows"""
        now = self._now()
        current_hour = now.hour
        
        for start, end in self._prime_time_windows:
            if start <= end:
                if start <= current_hour < end:
                    return True
            else:  # Crosses midnight (e.g., 22-1)
                if current_hour >= start or current_hour < end:
                    return True
        return False

    def _calculate_optimal_frequency(self) -> float:
        """Adjust checking frequency based on likelihood of appointments.
        
        Uses the configurable prime_time_backoff_multiplier (default 0.5 = 2x faster),
        weekend multiplier, and pattern-derived weight when available.
        """
        base_freq = self.cfg.check_frequency_minutes
        
        # Apply pattern weight if pattern learning is active
        pattern_weight = self._calculate_pattern_weight()
        
        if self._is_prime_time():
            # More frequent during optimal windows
            freq = max(1.0, base_freq * self.cfg.prime_time_backoff_multiplier)
            # Pattern weight further adjusts prime-time frequency
            freq = max(1.0, freq * pattern_weight)
            return freq
        elif 2 <= self._now().hour <= 6:
            # Less frequent during low-activity hours
            return base_freq * 2.0
        elif self._now().weekday() in [5, 6]:  # Weekend
            return base_freq * self.cfg.weekend_frequency_multiplier
        else:
            return base_freq * pattern_weight

    def _should_use_burst_mode(self) -> bool:
        """Enable burst mode during high-probability windows"""
        if self.cfg.safety_first_mode:
            return False
        if not self.cfg.burst_mode_enabled:
            return False
            
        now = self._now()
        
        # Business hours start (6-9 AM)
        if 6 <= now.hour <= 9:
            return True
        
        # Lunch hour (12-2 PM) 
        if 12 <= now.hour <= 14:
            return True
            
        # If we haven't seen "busy" in last 30 minutes (possible opening)
        if self._last_busy_check and (self._now() - self._last_busy_check) > timedelta(minutes=30):
            return True
            
        return False

    def _check_all_locations(self) -> Optional[str]:
        """Check availability across multiple consulates"""
        if not self.cfg.multi_location_check:
            return None
            
        backup_locations = [loc.strip() for loc in self.cfg.backup_locations.split(',') if loc.strip()]
        locations = [self.cfg.location] + backup_locations
        
        for location in locations:
            try:
                if self._check_location_availability(location):
                    logging.info("🎉 Found availability at %s!", location)
                    return location
            except Exception as exc:
                logging.debug("Failed to check %s: %s", location, exc)
                
        return None

    def _check_location_availability(self, location: str) -> bool:
        """Quick availability check for specific location.
        
        IMPORTANT: This must only be called AFTER navigating to the appointment page.
        Returns False if we're not on the right page.
        """
        try:
            driver = self.ensure_driver()
            current_url = driver.current_url.lower()
            
            # Safety check: verify we're on the appointment page
            if not any(token in current_url for token in ("appointment", "schedule")):
                logging.debug("Not on appointment page, skipping location availability check")
                return False
            
            # Also verify login is complete (not on sign_in page)
            if "sign_in" in current_url:
                logging.debug("Still on login page, skipping location availability check")
                return False
            
            # Switch to location
            location_select = self._find_element(self.LOCATION_SELECTORS, wait_time=5)
            if location_select:
                # Temporarily change the target location for this check
                original_location = self.cfg.location
                self.cfg.location = location
                self._ensure_location_selected(location_select)
                self.cfg.location = original_location  # Restore original
                time.sleep(1)
            else:
                # If no location selector, we can't switch locations
                logging.debug("No location selector found for multi-location check")
                return False
                
            # Quick busy check
            return not self._is_calendar_busy()
        except Exception:
            return False

    def _load_patterns(self) -> None:
        """Load historical availability patterns"""
        if self._pattern_file.exists():
            try:
                with open(self._pattern_file) as f:
                    self._availability_history = json.load(f)
                logging.info("Loaded %d historical availability events", len(self._availability_history))
            except Exception as exc:
                logging.debug("Failed to load patterns: %s", exc)
                self._availability_history = []

    def _save_patterns(self) -> None:
        """Save availability patterns for future optimization"""
        try:
            with open(self._pattern_file, 'w') as f:
                json.dump(self._availability_history[-100:], f, indent=2)  # Keep last 100 events
        except Exception as exc:
            logging.debug("Failed to save patterns: %s", exc)

    def _record_availability_event(self, event_type: str) -> None:
        """Record when calendar becomes available or busy"""
        if not self.cfg.pattern_learning_enabled:
            return
            
        now = self._now()
        event = {
            'timestamp': now.isoformat(),
            'hour': now.hour,
            'day_of_week': now.weekday(),
            'event': event_type
        }
        self._availability_history.append(event)
        self._save_patterns()

    def _calculate_pattern_weight(self) -> float:
        """Derive a frequency multiplier from historical availability patterns.

        Returns a value between 0.3 (very high historical availability → check
        more often) and 1.5 (very low historical availability → slow down).
        Falls back to 1.0 (neutral) when there is insufficient data.
        """
        if not self.cfg.pattern_learning_enabled or len(self._availability_history) < 10:
            return 1.0

        current_hour = self._now().hour
        current_dow = self._now().weekday()

        # Count successes (accessible / earlier_date_found / available_in_burst)
        # vs total events for the current hour ±1 and same day-of-week.
        relevant_success = 0
        relevant_total = 0

        for ev in self._availability_history:
            ev_hour = ev.get("hour")
            ev_dow = ev.get("day_of_week")
            if ev_hour is None or ev_dow is None:
                continue
            hour_match = abs(ev_hour - current_hour) <= 1 or abs(ev_hour - current_hour) >= 23
            dow_match = ev_dow == current_dow
            if hour_match or dow_match:
                relevant_total += 1
                if ev.get("event") in ("accessible", "earlier_date_found", "available_in_burst"):
                    relevant_success += 1

        if relevant_total < 5:
            return 1.0

        success_rate = relevant_success / relevant_total
        # Map success_rate 0→1.5, 1→0.3 (linear interpolation)
        weight = 1.5 - 1.2 * success_rate
        return round(max(0.3, min(1.5, weight)), 2)

    def _perform_burst_checks(self) -> bool:
        """Perform rapid-fire checks during burst mode.

        Uses API fast-path when available, falls back to browser calendar.
        When availability is detected, fully evaluates and acts on dates.
        """
        logging.info("🚀 Entering burst mode - rapid checking for 10 minutes")
        self._burst_mode_active = True
        
        try:
            for i in range(20):  # 20 checks x 30 seconds = 10 minutes
                # Try API first (much faster)
                if self._schedule_id:
                    primary_fid = self._resolve_facility_id(self.cfg.location)
                    if primary_fid:
                        api_dates = self._api_check_dates(primary_fid)
                        if api_dates:
                            logging.info("🎉 BURST API: %d dates found after %d attempts", len(api_dates), i + 1)
                            self._record_availability_event("available_in_burst")
                            self._evaluate_api_results({self.cfg.location: api_dates})
                            return True

                # Fall back to browser check
                if not self._is_calendar_busy():
                    logging.info("🎉 CALENDAR AVAILABLE! Collecting dates after %d attempts", i + 1)
                    self._record_availability_event("available_in_burst")
                    available_slots = self._collect_available_dates(max_months=3)
                    if available_slots:
                        self._evaluate_available_dates(available_slots)
                    return True
                    
                if i < 19:  # Don't sleep after last check
                    time.sleep(30)
                    
            logging.info("Burst mode completed - no availability found")
            return False
        finally:
            self._burst_mode_active = False

    # ------------------------------------------------------------------
    # API fast-path methods (P1.1)
    # ------------------------------------------------------------------
    def _resolve_facility_id(self, location: str) -> Optional[str]:
        """Resolve a location name to a facility ID."""
        override = (self.cfg.facility_id or "").strip()
        if override.isdigit():
            return override
        normalized = location.strip().lower()
        for name, fid in self.FACILITY_ID_MAP.items():
            if name.lower() in normalized or normalized in name.lower():
                return fid
        return None

    def _get_api_session(self):
        """Create/reuse a requests.Session with cookies from the Selenium driver."""
        try:
            import requests as _requests  # noqa: F811
        except ImportError:
            return None

        driver = self.driver
        if driver is None:
            return None

        try:
            cookies = driver.get_cookies()
        except WebDriverException:
            return None

        cookie_hash = hash(tuple(sorted((c["name"], c["value"]) for c in cookies)))

        if self._api_session is not None and self._api_session_cookies_hash == cookie_hash:
            return self._api_session

        session = _requests.Session()
        session.headers.update({
            "User-Agent": driver.execute_script("return navigator.userAgent"),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": driver.current_url,
        })
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))

        self._api_session = session
        self._api_session_cookies_hash = cookie_hash
        return session

    def _api_check_dates(self, facility_id: str) -> Optional[List[str]]:
        """Check available dates via the JSON API (no browser interaction).

        Returns list of date strings (YYYY-MM-DD) or None on failure.
        """
        if not self._schedule_id:
            return None

        if self._should_throttle():
            logging.debug("API rate-throttled, skipping")
            return None

        session = self._get_api_session()
        if session is None:
            return None

        url = (
            f"{self._portal_url()}/schedule/"
            f"{self._schedule_id}/appointment/days/{facility_id}.json"
            f"?appointments[expedite]=false"
        )

        def _extract_dates(response):
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    dates = [entry["date"] for entry in data if "date" in entry]
                    logging.info("API: %d dates available at facility %s", len(dates), facility_id)
                    return dates
            return None

        try:
            self._record_api_request()
            resp = session.get(url, timeout=10)
            dates = _extract_dates(resp)
            if dates is not None:
                return dates
            if resp.status_code == 401:
                logging.debug("API returned 401 — refreshing session and retrying once")
                self._api_session = None
                retry_session = self._get_api_session()
                if retry_session is not None:
                    self._record_api_request()
                    retry_resp = retry_session.get(url, timeout=10)
                    retry_dates = _extract_dates(retry_resp)
                    if retry_dates is not None:
                        return retry_dates
                    logging.debug(
                        "API retry returned status %d for facility %s",
                        retry_resp.status_code,
                        facility_id,
                    )
            else:
                logging.debug("API returned status %d for facility %s", resp.status_code, facility_id)
        except Exception as exc:  # noqa: BLE001
            logging.debug("API date check failed for facility %s: %s", facility_id, exc)

        return None

    def _api_check_times(self, facility_id: str, date: str) -> Optional[List[str]]:
        """Fetch available times for a specific date via JSON API."""
        if not self._schedule_id:
            return None

        session = self._get_api_session()
        if session is None:
            return None

        url = (
            f"{self._portal_url()}/schedule/"
            f"{self._schedule_id}/appointment/times/{facility_id}.json"
            f"?date={date}&appointments[expedite]=false"
        )

        def _extract_times(response):
            if response.status_code == 200:
                data = response.json()
                return data.get("available_times", [])
            return None

        try:
            self._record_api_request()
            resp = session.get(url, timeout=10)
            times = _extract_times(resp)
            if times is not None:
                return times
            if resp.status_code == 401:
                logging.debug("API times returned 401 — refreshing session and retrying once")
                self._api_session = None
                retry_session = self._get_api_session()
                if retry_session is not None:
                    self._record_api_request()
                    retry_resp = retry_session.get(url, timeout=10)
                    retry_times = _extract_times(retry_resp)
                    if retry_times is not None:
                        return retry_times
        except Exception as exc:  # noqa: BLE001
            logging.debug("API time check failed: %s", exc)

        return None

    def _api_check_all_locations(self) -> Dict[str, List[str]]:
        """Check all facility locations via API in parallel.

        Locations are ordered by historical slot volume (slot_ledger priority
        scoring) so that the most productive locations are checked first.

        Returns dict of {location_name: [date_strings]} for locations with
        available dates.
        """
        if not self._schedule_id:
            return {}

        # Pre-compute scores to avoid repeated DB calls during sort.
        scores: Dict[str, float] = {
            name: self._slot_ledger.location_score(name)
            for name in self.FACILITY_ID_MAP
        }
        # Sort facilities by location_score descending so high-yield locations
        # are submitted to the thread pool first.
        facilities_sorted = sorted(
            self.FACILITY_ID_MAP.items(),
            key=lambda item: scores[item[0]],
            reverse=True,
        )
        results: Dict[str, List[str]] = {}

        def _check_one(name_id_pair):
            name, fid = name_id_pair
            dates = self._api_check_dates(fid)
            return name, dates

        with ThreadPoolExecutor(max_workers=min(4, len(facilities_sorted))) as pool:
            futures = {pool.submit(_check_one, item): item for item in facilities_sorted}
            for future in as_completed(futures, timeout=30):
                try:
                    name, dates = future.result()
                    if dates:
                        results[name] = dates
                except Exception as exc:  # noqa: BLE001
                    logging.debug("Parallel API check failed: %s", exc)

        if results:
            total = sum(len(d) for d in results.values())
            logging.info("API scan: %d locations with availability (%d total dates)", len(results), total)

        return results

    def _evaluate_api_results(self, results: Dict[str, List[str]]) -> None:
        """Evaluate dates obtained from the API fast-path."""
        try:
            current_date = datetime.strptime(self.cfg.current_appointment_date, "%Y-%m-%d")
            start_date = datetime.strptime(self.cfg.start_date, "%Y-%m-%d")
            end_date = datetime.strptime(self.cfg.end_date, "%Y-%m-%d")
        except ValueError:
            return

        best_by_location: Dict[str, datetime] = {}

        for location, dates in results.items():
            for date_str in dates:
                try:
                    parsed = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    continue

                self._slot_ledger.record_slot(date_str, location)

                if self._is_excluded_date(parsed):
                    logging.debug("Skipping excluded API date %s at %s", date_str, location)
                    continue

                if start_date <= parsed <= end_date and parsed < current_date:
                    days_earlier = (current_date - parsed).days
                    if days_earlier >= self.cfg.min_improvement_days:
                        if location not in best_by_location or parsed < best_by_location[location]:
                            best_by_location[location] = parsed

        if not best_by_location:
            return

        best_location = min(best_by_location, key=lambda loc: best_by_location[loc])
        best_date = best_by_location[best_location]
        days_earlier = (current_date - best_date).days
        slot_key = best_date.strftime("%Y-%m-%d")

        # Suppress duplicate notifications for the same slot (Phase 4)
        if self._slot_ledger.is_notified(slot_key, best_location, ttl_hours=self.cfg.slot_ttl_hours):
            logging.debug(
                "Skipping duplicate notification for %s @ %s (already notified within %dh TTL)",
                slot_key, best_location, self.cfg.slot_ttl_hours,
            )
            return

        logging.info("🎉 API SCAN: Earlier appointment at %s: %s (%d days earlier)",
                     best_location, best_date.strftime("%Y-%m-%d"), days_earlier)
        self._audio_alert("earlier appointment found via API")

        self._record_availability_event("earlier_date_found")

        location_details = []
        for loc, dt in sorted(best_by_location.items(), key=lambda x: x[1]):
            de = (current_date - dt).days
            location_details.append(f"  📍 {loc}: {dt.strftime('%B %d, %Y')} ({de} days earlier)")

        subject = f"🎉 Earlier Visa Appointment! {best_date.strftime('%B %d, %Y')} at {best_location} ({days_earlier}d earlier)"
        message = (
            f"Earlier visa appointments found via fast API scan!\n\n"
            f"Best option:\n"
            f"📅 {best_date.strftime('%B %d, %Y')} at {best_location}\n"
            f"⏰ {days_earlier} days earlier than current ({self.cfg.current_appointment_date})\n\n"
            f"All available locations:\n"
            + "\n".join(location_details) + "\n\n"
            f"Current: {self.cfg.current_appointment_date}\n"
            f"Range: {self.cfg.start_date} to {self.cfg.end_date}\n\n"
            + ("🤖 Auto-book ENABLED" if self.cfg.auto_book else "⚠️ Book manually: https://ais.usvisa-info.com")
        )
        send_notification(self.cfg, subject, message)

        self._slot_ledger.mark_notified(slot_key, best_location)

    def _try_api_fast_check(self) -> bool:
        """Attempt fast API-based checking.

        Returns True if availability was found and handled (notification sent
        etc.), False if the caller should fall back to browser-based checking.
        """
        if not self._schedule_id or self._should_throttle():
            return False

        api_results = self._api_check_all_locations()
        if not api_results:
            return False

        self._evaluate_api_results(api_results)
        return True

    # ------------------------------------------------------------------
    # Rate tracking (P2.5)
    # ------------------------------------------------------------------
    def _record_api_request(self) -> None:
        """Record an API request timestamp for rate limiting."""
        now = self._now()
        self._request_timestamps.append(now)
        cutoff = now - timedelta(hours=1)
        self._request_timestamps = [t for t in self._request_timestamps if t > cutoff]
        self._api_check_count += 1

    def _record_ui_navigation(self) -> None:
        """Record a UI navigation for rate limiting and observability."""
        now = self._now()
        self._ui_nav_timestamps.append(now)
        cutoff = now - timedelta(hours=1)
        self._ui_nav_timestamps = [t for t in self._ui_nav_timestamps if t > cutoff]
        self._ui_check_count += 1

    def _ui_cooldown_seconds(self) -> int:
        """Return seconds until UI navigation budget resets (0 if under limit)."""
        limit = self.cfg.max_ui_navigations_per_hour
        if limit <= 0:
            return 0

        now = self._now()
        cutoff = now - timedelta(hours=1)
        recent = [t for t in self._ui_nav_timestamps if t > cutoff]
        if len(recent) < limit:
            return 0

        oldest = min(recent)
        remaining = int((oldest + timedelta(hours=1) - now).total_seconds())
        return max(1, remaining)

    def _should_throttle(self) -> bool:
        """Check if we should throttle API requests (uses API-specific budget)."""
        limit = self.cfg.max_api_requests_per_hour
        if limit <= 0:
            return False
        now = self._now()
        cutoff = now - timedelta(hours=1)
        recent = sum(1 for t in self._request_timestamps if t > cutoff)
        if recent >= limit:
            logging.debug("API rate limit: %d requests in the last hour (max: %d)",
                          recent, limit)
            return True
        return False

    def _should_throttle_ui(self) -> bool:
        """Check if we should throttle UI navigations."""
        limit = self.cfg.max_ui_navigations_per_hour
        if limit <= 0:
            return False
        now = self._now()
        cutoff = now - timedelta(hours=1)
        self._ui_nav_timestamps = [t for t in self._ui_nav_timestamps if t > cutoff]
        if len(self._ui_nav_timestamps) >= limit:
            logging.debug(
                "UI rate limit: %d navigations in the last hour (max: %d)",
                len(self._ui_nav_timestamps),
                limit,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Config hot-reload (P3.2)
    # ------------------------------------------------------------------
    def _check_config_reload(self) -> bool:
        """Check if config.ini has changed and reload if so."""
        try:
            mtime = os.path.getmtime("config.ini")
        except OSError:
            return False

        if self._config_mtime is not None and mtime > self._config_mtime:
            logging.info("🔄 config.ini changed, reloading...")
            try:
                new_cfg = CheckerConfig.load()
                old_auto_book = self.cfg.auto_book
                old_country = self.cfg.country_code
                old_schedule_seed = (self.cfg.schedule_id or "").strip()
                self.cfg = new_cfg
                self._refresh_portal_urls()

                new_schedule_seed = (new_cfg.schedule_id or "").strip()
                if new_schedule_seed and new_schedule_seed != self._schedule_id:
                    self._schedule_id = new_schedule_seed
                    self._api_session = None
                    self._api_session_cookies_hash = None
                    logging.info("Schedule ID seed updated from config reload: %s", new_schedule_seed)

                if old_country != new_cfg.country_code:
                    logging.info("Country code changed: %s → %s", old_country, new_cfg.country_code)
                    self._api_session = None
                    self._api_session_cookies_hash = None
                    self.reset_driver()
                elif old_schedule_seed != new_schedule_seed and new_schedule_seed:
                    self._api_session = None
                    self._api_session_cookies_hash = None

                self._config_mtime = mtime
                self._parse_prime_time_windows()
                self._parse_excluded_windows()
                self._initialize_account_rotation()
                self._init_vpn_manager()
                logging.info("✅ Configuration reloaded successfully")
                if new_cfg.auto_book != old_auto_book:
                    logging.info("Auto-book changed: %s → %s", old_auto_book, new_cfg.auto_book)
                return True
            except Exception as exc:  # noqa: BLE001
                logging.warning("Config reload failed: %s", exc)

        self._config_mtime = mtime
        return False

    # ------------------------------------------------------------------
    # High level flow
    # ------------------------------------------------------------------
    def _get_page_state(self) -> str:
        """Detect current page type to optimize navigation"""
        if not self.driver:
            return "no_driver"
            
        try:
            url = self.driver.current_url.lower()
            page_source = self.driver.page_source.lower()
            
            if "appointment" in url and ("form" in page_source or "appointments_consulate_appointment" in page_source):
                return "appointment_form"
            elif "schedule" in url:
                return "schedule_page" 
            elif "groups" in url:
                return "dashboard"
            elif "sign_in" in url:
                return "login_required"
            return "unknown"
        except Exception:
            return "unknown"

    def _run_test_mode_check(self) -> None:
        """Validate authenticated flow without probing/booking appointment slots."""
        on_form = self._ensure_on_appointment_form(max_wait=5)
        if on_form:
            logging.info("🧪 Test mode: authenticated and appointment form detected")
        else:
            logging.warning("🧪 Test mode: authenticated but appointment form not detected")
        send_notification(
            self.cfg,
            "🧪 Visa Checker Test Mode Report",
            (
                "Test mode check completed.\n"
                f"Authenticated account: {self.cfg._mask(self.cfg.email)}\n"
                f"Appointment form detected: {'Yes' if on_form else 'No'}\n"
                "No availability probing or booking actions were executed."
            ),
        )

    def perform_check(self) -> None:
        start_time = datetime.now()
        
        # --- API fast-path (P1.1): skip browser if we have schedule_id ---
        if self._schedule_id and self.driver is not None:
            logging.info("Attempting API fast-path check (%d locations)...", len(self.FACILITY_ID_MAP))
            if self._try_api_fast_check():
                logging.info("✅ API fast-path found availability; browser interaction skipped")
                # If auto-book is needed, fall through to browser flow below
                if not self.cfg.auto_book:
                    return
            else:
                logging.debug("API fast-path found nothing; falling back to browser")

        # Respect UI navigation budgets before launching a browser cycle
        if self._should_throttle_ui():
            cooldown = self._ui_cooldown_seconds()
            if cooldown <= 0:
                cooldown = max(30, int(self.cfg.check_frequency_minutes * 60))
            self._backoff_until = max(
                self._backoff_until or datetime.now(),
                datetime.now() + timedelta(seconds=cooldown),
            )
            logging.warning(
                "UI navigation limit reached (%d/hour). Backing off for %d seconds before next browser cycle.",
                self.cfg.max_ui_navigations_per_hour,
                cooldown,
            )
            raise UiRateLimitError(
                f"UI navigation budget reached ({self.cfg.max_ui_navigations_per_hour}/hr); "
                f"cooling down for {cooldown} seconds"
            )

        # Record this as a UI navigation cycle
        self._record_ui_navigation()

        driver = self.ensure_driver()
        
        try:
            # Smart navigation based on current page state
            page_state = self._get_page_state()
            logging.debug("Current page state: %s", page_state)
            
            if page_state == "appointment_form":
                # Already on appointment page, skip navigation
                logging.info("Already on appointment form, skipping navigation")
            elif page_state in ["dashboard", "schedule_page"]:
                # Navigate directly to scheduling
                self._navigate_to_schedule(driver)
            else:
                # Full login flow required
                self._navigate_to_login(driver)
                if "sign_in" in driver.current_url.lower():
                    self._complete_login(driver)
                else:
                    logging.info("Session already authenticated; skipping login form.")
                self._navigate_to_schedule(driver)

            # Extract schedule_id from current URL for future API fast-path
            self._extract_schedule_id(driver.current_url)

            if self.cfg.test_mode:
                self._run_test_mode_check()
                return

            # Now check availability after we're properly navigated
            self._check_consulate_availability()

            # --- Multi-location scan when primary is busy ---
            if self.cfg.multi_location_check and self._busy_streak_count > 0:
                alt = self._check_all_locations()
                if alt:
                    logging.info("Appointment availability detected at alternate location: %s", alt)
                    send_notification(
                        self.cfg,
                        f"📍 Appointment available at {alt}!",
                        f"Calendar is open at {alt} while {self.cfg.location} is busy."
                        f"\n\nLogin to book: https://ais.usvisa-info.com",
                    )

            # --- Burst mode trigger ---
            if self._should_use_burst_mode() and self._busy_streak_count == 0:
                logging.info("Burst-mode conditions met — starting rapid checks")
                self._perform_burst_checks()

            logging.info("Appointment check completed - reached scheduling section.")

        except CaptchaDetectedError as exc:
            logging.warning("Captcha blocked automation: %s", exc)
            self.reset_driver()
            raise
        except Exception as exc:  # noqa: BLE001
            logging.exception("Error during appointment check: %s", exc)
            self._handle_error(exc)
            self.reset_driver()
            raise
        finally:
            # Track performance metrics
            duration = (datetime.now() - start_time).total_seconds()
            self._track_performance('check_duration', duration)

    # ------------------------------------------------------------------
    # Core automation steps
    # ------------------------------------------------------------------
    def _validate_existing_session(self, driver: webdriver.Chrome) -> bool:
        """Check if current session is still valid without full login"""
        if not self._last_session_validation:
            return False
            
        # Only validate every 5 minutes to avoid overhead
        time_since_validation = datetime.now() - self._last_session_validation
        if time_since_validation < timedelta(minutes=5):
            return True
            
        try:
            # Try accessing a known authenticated endpoint
            driver.get(f"{self._portal_url()}/groups")
            is_valid = "sign_in" not in driver.current_url.lower()
            if is_valid:
                self._last_session_validation = datetime.now()
                logging.info("Existing session validated successfully")
            return is_valid
        except Exception:
            return False

    def _navigate_to_login(self, driver: webdriver.Chrome) -> None:
        # Check for existing valid session first
        if self._validate_existing_session(driver):
            logging.info("Valid session detected, skipping login workflow")
            return
            
        login_url = self._login_target()
        logging.info("Navigating to login page: %s", login_url)
        self._safe_get(login_url, detect_captcha=True)
        self._dismiss_overlays()
        
        # Check if we're already authenticated by looking for dashboard/groups/schedule URLs
        current_url = driver.current_url.lower()
        if any(indicator in current_url for indicator in ["dashboard", "groups", "schedule"]) and "sign_in" not in current_url:
            logging.info("Already authenticated, skipping login form")
            self._last_session_validation = datetime.now()

    def _complete_login(self, driver: webdriver.Chrome) -> None:
        logging.info("Attempting to complete login workflow")

        email_field = self._find_or_raise(self.EMAIL_SELECTORS, "email field", wait_time=20)
        self._enter_text(email_field, self.cfg.email)
        logging.info("Entered email address")

        password_field = self._find_or_raise(self.PASSWORD_SELECTORS, "password field", wait_time=15)
        self._enter_text(password_field, self.cfg.password)
        logging.info("Entered password")

        sign_in_button = self._find_or_raise(
            self.SIGN_IN_SELECTORS,
            "sign in button",
            wait_time=15,
            clickable=True,
        )
        self._accept_privacy_policy()
        logging.info("Clicking sign in button")
        sign_in_button.click()

        self._detect_captcha()

        self._await_login_transition(driver)

    def _navigate_to_schedule(self, driver: webdriver.Chrome) -> None:
        self._handle_group_continue()

        schedule_found = False
        for url in self._reschedule_targets():
            try:
                logging.info("Navigating to scheduling page candidate: %s", url)
                self._safe_get(url)
                self._dismiss_overlays()
                if "sign_in" in driver.current_url.lower():
                    logging.info("Session expired while navigating; re-authenticating")
                    self._complete_login(driver)
                    continue
                self._handle_group_continue()
                current = driver.current_url.lower()
                if any(token in current for token in ("schedule", "appointment")):
                    logging.info("Reached scheduling page: %s", driver.current_url)
                    self._open_reschedule_flow()
                    schedule_found = True
                    break
            except TimeoutException:
                logging.warning("Timeout while loading %s; trying next", url)
            except WebDriverException as exc:
                logging.warning("Browser navigation error for %s: %s", url, exc)
        
        if not schedule_found:
            logging.error("Failed to reach scheduling page. Current URL: %s", driver.current_url)
            self._capture_debug_state("scheduling_navigation_failed")
            raise RuntimeError("Failed to reach scheduling page")

        # Wait for page to fully load after navigation
        time.sleep(2)
        self._wait_for_page_ready(driver)
        
        # Cache form elements when we reach the appointment page for future use
        self._cache_form_elements()
        
        # Try to find location selector with SHORTER wait to fail fast
        location_select = self._find_element(self.LOCATION_SELECTORS, wait_time=10, use_cache=True)
        if location_select:
            self._ensure_location_selected(location_select)
            logging.info("Location selector found and configured")
        else:
            # Try alternative: check if we're on the right page by looking for the date input
            date_input = self._find_element(self.CONSULATE_DATE_INPUT_SELECTORS, wait_time=5)
            if date_input:
                logging.info("Date picker found, location may be pre-selected or single location")
            else:
                logging.warning(
                    "Location selector not found; page layout may have changed or location already locked."
                )
                # Capture comprehensive debug info when location selector is missing
                self._capture_debug_state("missing_location_selector")

                # If this is a Scheduling Limit Warning page, raise immediately so we
                # avoid the 20-second widget wait in _check_consulate_availability and
                # apply an appropriately long backoff.
                try:
                    title = (driver.title or "").lower()
                    source = (driver.page_source or "").lower()
                except Exception:  # noqa: BLE001
                    title = ""
                    source = ""
                if "scheduling limit warning" in title or "scheduling limit warning" in source:
                    self._scheduling_limit_count += 1
                    self._handle_scheduling_limit_warning(driver)

    def _handle_group_continue(self) -> None:
        driver = self.ensure_driver()

        button = self._find_element(self.GROUP_CONTINUE_SELECTORS, wait_time=5, clickable=True)
        if not button:
            return

        # Capture href BEFORE clicking to avoid stale element reference
        continue_href = ""
        try:
            continue_href = button.get_attribute("href") or ""
        except (WebDriverException, StaleElementReferenceException):
            logging.debug("Could not retrieve href from button; continuing without base URL capture")

        self._scroll_into_view(button)
        try:
            button.click()
        except (WebDriverException, ElementClickInterceptedException, ElementNotInteractableException):
            logging.debug("Direct continue click failed; attempting scripted click")
            try:
                driver.execute_script("arguments[0].click();", button)
            except (WebDriverException, StaleElementReferenceException):
                logging.debug("Scripted click also failed; page may have already navigated")

        logging.info("Clicked group continue button")

        if continue_href:
            absolute_continue = urljoin(driver.current_url, continue_href)
            base = absolute_continue.rstrip("/")
            if base.endswith("continue_actions"):
                base = base[: -len("continue_actions")].rstrip("/")
            if base and not base.endswith("/"):
                base = f"{base}/"
            if base:
                self._appointment_base_url = base
                logging.debug("Captured appointment base URL: %s", self._appointment_base_url)
                # Extract schedule_id for API fast-path
                self._extract_schedule_id(base)

        self._wait_for_page_ready(driver)
        self._dismiss_overlays()

    def _open_reschedule_flow(self) -> None:
        driver = self.ensure_driver()
        current = driver.current_url.lower()
        
        # Check if we're already on the appointment form by looking for key elements
        if self._ensure_on_appointment_form():
            logging.info("Already on appointment form")
            return

        # If URL contains /appointment, we're likely on the right page but need to wait for it to load
        if "/appointment" in current:
            logging.info("On appointment URL, waiting for form elements to load...")
            time.sleep(2)
            self._wait_for_page_ready(driver)
            if self._ensure_on_appointment_form():
                return

        if "continue_actions" in current:
            logging.info("On continue actions page; expanding reschedule section")
        else:
            toggler = self._find_element(self.RESCHEDULE_TOGGLE_SELECTORS, wait_time=10, clickable=True)
            if toggler:
                self._scroll_into_view(toggler)
                try:
                    toggler.click()
                except (WebDriverException, ElementClickInterceptedException):
                    logging.debug("Accordion toggle click failed; attempting scripted click")
                    driver.execute_script("arguments[0].click();", toggler)
                time.sleep(1)
                if self._ensure_on_appointment_form():
                    return
            else:
                logging.debug("Reschedule accordion toggle not found; attempting to locate button directly")

        if self._appointment_base_url:
            appointment_url = urljoin(self._appointment_base_url, "appointment")
            if not driver.current_url.startswith(appointment_url):
                logging.info("Loading appointment page directly via stored URL: %s", appointment_url)
                self._safe_get(appointment_url)
                # Wait longer for page to fully render - the AIS site can be slow
                time.sleep(5)
                self._wait_for_page_ready(driver)
                self._dismiss_overlays()
            if self._ensure_on_appointment_form():
                return

        reschedule_button = self._find_element(self.RESCHEDULE_BUTTON_SELECTORS, wait_time=10, clickable=False)
        if reschedule_button:
            href = ""
            try:
                href = reschedule_button.get_attribute("href") or ""
            except (WebDriverException, StaleElementReferenceException):
                logging.debug("Could not retrieve href from reschedule button")
            
            if href:
                logging.info("Navigating directly to reschedule link: %s", href)
                self._safe_get(href)
                time.sleep(2)
                self._dismiss_overlays()
                if self._ensure_on_appointment_form():
                    return
            else:
                logging.debug("Reschedule anchor missing href attribute; attempting click")
                try:
                    self._scroll_into_view(reschedule_button)
                    reschedule_button.click()
                except (ElementClickInterceptedException, ElementNotInteractableException, WebDriverException, StaleElementReferenceException):
                    logging.debug("Reschedule button click failed; attempting scripted click")
                    try:
                        driver.execute_script("arguments[0].click();", reschedule_button)
                    except (WebDriverException, StaleElementReferenceException):
                        logging.debug("Scripted click on reschedule button also failed")
                time.sleep(2)
                self._wait_for_page_ready(driver)
                self._dismiss_overlays()
                if self._ensure_on_appointment_form():
                    return

        # Final attempt: Check if form elements exist even if form itself wasn't detected
        location_or_date = self._find_element(self.LOCATION_SELECTORS, wait_time=5) or \
                          self._find_element(self.CONSULATE_DATE_INPUT_SELECTORS, wait_time=5)
        if location_or_date:
            logging.info("Found form elements directly, proceeding with check")
            return

        logging.warning(
            "Unable to open reschedule appointment workflow automatically; remaining on %s",
            driver.current_url,
        )
        # Capture comprehensive debug information when reschedule fails
        self._capture_debug_state("reschedule_navigation_failed")

    def _ensure_on_appointment_form(self, max_wait: int = 15) -> bool:
        """Check if we're on the appointment form page.
        
        Uses multiple strategies with SHORT waits to quickly detect the form.
        Returns False quickly if form elements are not found.
        
        Args:
            max_wait: Maximum seconds to spend looking for form elements (default 15)
        """
        driver = self.ensure_driver()
        current_url = driver.current_url.lower()
        
        # Quick URL check - if we're still on login, definitely not on form
        if "sign_in" in current_url:
            logging.debug("Still on sign_in page, not on appointment form")
            return False
        
        # URL must contain appointment or schedule keywords
        if not any(token in current_url for token in ("appointment", "schedule")):
            logging.debug("URL doesn't contain appointment/schedule: %s", driver.current_url)
            return False

        # Detect Scheduling Limit Warning page — this is NOT a valid appointment form.
        # AIS shows this page when too many scheduling attempts have been made; it requires
        # human CAPTCHA intervention to proceed and cannot be automated past.
        try:
            title = (driver.title or "").lower()
            if "scheduling limit warning" in title:
                logging.warning(
                    "Scheduling Limit Warning page detected at %s — not a valid appointment form",
                    driver.current_url,
                )
                return False
        except Exception:  # noqa: BLE001
            pass
        
        start_time = datetime.now()
        
        # Quick check using short waits (1-2 seconds per selector group)
        # This prevents the 70+ second waits we were seeing
        form = self._find_element(self.APPOINTMENT_FORM_SELECTORS, wait_time=2)
        if form:
            logging.info("Appointment form detected at %s", driver.current_url)
            return True
        
        # Check if we've spent too long already
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed > max_wait:
            logging.debug("Exceeded max wait time (%.1fs) looking for form", elapsed)
            return False
        
        # Quick check for key elements that indicate we're on the right page
        location = self._find_element(self.LOCATION_SELECTORS, wait_time=2)
        if location:
            logging.info("Location selector detected at %s", driver.current_url)
            return True
            
        date_input = self._find_element(self.CONSULATE_DATE_INPUT_SELECTORS, wait_time=2)
        if date_input:
            logging.info("Date input detected at %s", driver.current_url)
            return True
            
        busy_element = self._find_element(self.CONSULATE_BUSY_SELECTORS, wait_time=2)
        if busy_element:
            logging.info("Busy indicator detected at %s", driver.current_url)
            return True
        
        # Last resort: check for fieldset typical of appointment forms
        try:
            fieldset = driver.find_element(By.CSS_SELECTOR, "fieldset.fieldset")
            if fieldset:
                logging.info("Appointment fieldset detected at %s", driver.current_url)
                return True
        except NoSuchElementException:
            pass
        
        logging.debug("No appointment form elements found at %s", driver.current_url)
        return False

    def _ensure_location_selected(self, element) -> None:
        # Check if it's a standard <select> element or a custom dropdown
        if element.tag_name.lower() == 'select':
            self._handle_standard_location_select(element)
        else:
            self._handle_custom_location_dropdown(element)

    def _handle_standard_location_select(self, element) -> None:
        """Handle standard HTML <select> elements for location selection"""
        try:
            select = Select(element)
            selected_text = select.first_selected_option.text.strip() if select.options else ""

            normalized_target = self.cfg.location.strip().lower()
            normalized_selected = selected_text.lower()

            if normalized_target == normalized_selected:
                logging.info("Target consular location already selected: %s", selected_text)
                return

            # Try exact match first
            try:
                select.select_by_visible_text(self.cfg.location)
                logging.info("Selected consular location by exact match: %s", self.cfg.location)
                time.sleep(0.5)  # Allow UI to update
                return
            except NoSuchElementException:
                pass

            # Try matching by facility ID if location name matches our map
            for location_name, facility_id in self.FACILITY_ID_MAP.items():
                if normalized_target in location_name.lower() or location_name.lower() in normalized_target:
                    try:
                        select.select_by_value(facility_id)
                        logging.info("Selected consular location by facility ID %s: %s", facility_id, location_name)
                        time.sleep(0.5)  # Allow UI to update
                        return
                    except NoSuchElementException:
                        pass

            # Try fuzzy matching on visible text
            for option in select.options:
                option_text = option.text.strip()
                if not option_text:
                    continue
                if normalized_target in option_text.lower() or option_text.lower() in normalized_target:
                    select.select_by_visible_text(option_text)
                    logging.info("Selected consular location using fuzzy match: %s", option_text)
                    time.sleep(0.5)  # Allow UI to update
                    return

            logging.warning(
                "Unable to match configured location '%s' to the available dropdown options (currently '%s').",
                self.cfg.location,
                selected_text,
            )
        except Exception as exc:
            logging.debug("Error handling standard location select: %s", exc)

    def _handle_custom_location_dropdown(self, element) -> None:
        """Handle custom dropdown elements (div, etc.) for location selection"""
        try:
            # Check if location is already selected by looking at element text
            current_text = element.text.strip()
            normalized_target = self.cfg.location.strip().lower()
            
            if normalized_target in current_text.lower():
                logging.info("Target consular location already selected: %s", current_text)
                return

            # Try to find and click the dropdown to open it
            try:
                element.click()
                time.sleep(1)  # Wait for dropdown to open
                
                # Look for dropdown options - try multiple selectors
                option_selectors = [
                    (By.CSS_SELECTOR, f"[data-value*='{self.cfg.location}']"),
                    (By.CSS_SELECTOR, f"[title*='{self.cfg.location}']"),
                    (By.XPATH, f"//div[contains(text(), '{self.cfg.location}')]"),
                    (By.XPATH, f"//li[contains(text(), '{self.cfg.location}')]"),
                    (By.XPATH, f"//option[contains(text(), '{self.cfg.location}')]"),
                    (By.CSS_SELECTOR, ".dropdown-item, .option, .select-option"),
                ]
                
                # Try to find and click the correct option
                for selector_type, selector_value in option_selectors:
                    try:
                        options = self.driver.find_elements(selector_type, selector_value)
                        for option in options:
                            option_text = option.text.strip()
                            if option_text and normalized_target in option_text.lower():
                                option.click()
                                logging.info("Selected consular location from custom dropdown: %s", option_text)
                                time.sleep(1)  # Wait for selection to register
                                return
                    except Exception:
                        continue
                        
                logging.info("Location dropdown detected but target location not found in options")
                
            except Exception as exc:
                logging.debug("Could not interact with custom dropdown: %s", exc)
                
        except Exception as exc:
            logging.debug("Error handling custom location dropdown: %s", exc)

    def _is_calendar_busy(self) -> bool:
        """Check if calendar shows busy status.
        
        Returns True if:
        - Busy element is found and visible
        - OR we're not on the appointment page (safe default)
        """
        driver = self.ensure_driver()
        current_url = driver.current_url.lower()
        
        # Safety: if we're not on the appointment page, assume busy to prevent false positives
        if "sign_in" in current_url or not any(token in current_url for token in ("appointment", "schedule")):
            logging.debug("Not on appointment page, assuming calendar busy for safety")
            return True
            
        busy_element = self._is_selector_visible(self.CONSULATE_BUSY_SELECTORS)
        return busy_element is not None

    def _check_consulate_availability(self) -> None:
        driver = self.ensure_driver()
        check_start = datetime.now()
        
        logging.debug("Starting consulate availability check at %s", driver.current_url)

        try:
            WebDriverWait(driver, 20).until(
                lambda d: (
                    self._is_selector_visible(self.CONSULATE_BUSY_SELECTORS)
                    or self._is_selector_visible(self.CONSULATE_DATE_INPUT_SELECTORS)
                )
            )
        except TimeoutException:
            logging.warning("Consular appointment widgets did not load within the expected time window")
            # Capture comprehensive debug info when widgets don't load
            self._capture_debug_state("widgets_not_loaded")

            try:
                self._detect_account_lockout()
            except AccountLockedError:
                raise

            # If anti-bot controls or scheduling-limit gates are shown, treat this
            # as a blocking condition (not a successful availability check).
            self._detect_captcha()

            try:
                title = (driver.title or "").lower()
                source = (driver.page_source or "").lower()
            except Exception:  # noqa: BLE001
                title = ""
                source = ""

            scheduling_limit_markers = (
                "scheduling limit warning",
                "error message",
                "please try again later",
                "too many requests",
            )
            if any(marker in title or marker in source for marker in scheduling_limit_markers):
                self._scheduling_limit_count += 1
                self._handle_scheduling_limit_warning(driver)
            return

        self._detect_account_lockout()

        # Widgets loaded successfully — clear any previous scheduling-limit streak
        if self._scheduling_limit_count > 0:
            logging.info(
                "Scheduling Limit Warning cleared after %d consecutive occurrence(s); resuming normal checks",
                self._scheduling_limit_count,
            )
            self._scheduling_limit_count = 0

        # Intelligent calendar polling with adaptive frequency
        if self._is_calendar_busy():
            self._busy_streak_count += 1
            self._last_busy_check = datetime.now()
            message = "System is busy. Please try again later."
            
            busy_element = self._is_selector_visible(self.CONSULATE_BUSY_SELECTORS)
            if busy_element:
                message = busy_element.text.strip() or message
            
            # Check for "display: block" to confirm it's actually showing
            busy_visible = False
            try:
                busy_div = driver.find_element(By.ID, "consulate_date_time_not_available")
                display_style = busy_div.get_attribute("style") or ""
                busy_visible = "display: none" not in display_style.lower()
            except Exception:
                busy_visible = True  # Assume visible if we can't check
            
            if busy_visible:
                logging.info("⚠️ Consular calendar busy: %s", message)
                
                # Record busy event for pattern learning
                self._record_availability_event("busy")
                
                # Adaptive frequency adjustment for repeated busy responses
                if self._busy_streak_count >= 3:
                    old_freq = self._adaptive_frequency
                    self._adaptive_frequency = min(60, self._adaptive_frequency * 1.2)
                    if self._adaptive_frequency != old_freq:
                        logging.info("Adaptive frequency increased to %.1f minutes due to persistent busy status", 
                                   self._adaptive_frequency)
                
                self._schedule_backoff()
                self._capture_artifact("consulate_busy")
                return
            else:
                logging.debug("Busy element exists but is hidden, proceeding with calendar check")
                # Reset busy streak as it's not actually busy
                self._busy_streak_count = 0
        else:
            # Calendar is accessible!
            if self._busy_streak_count > 5:  # Only alert if we've been seeing busy for a while
                send_notification(
                    self.cfg,
                    "🚨 URGENT: Calendar Accessible!", 
                    f"Calendar is no longer busy after {self._busy_streak_count} attempts. Checking appointments NOW!"
                )
            
            # Record availability event for pattern learning
            self._record_availability_event("accessible")
            
            # Reset busy streak on successful calendar access
            if self._busy_streak_count > 0:
                logging.info("Calendar accessible again after %d busy attempts", self._busy_streak_count)
                self._busy_streak_count = 0
                self._adaptive_frequency = self.cfg.check_frequency_minutes

        date_input = self._find_element(self.CONSULATE_DATE_INPUT_SELECTORS, wait_time=5)
        if not date_input:
            logging.info("Consular date input field not found; cannot probe availability")
            return

        self._scroll_into_view(date_input)

        current_value = (date_input.get_attribute("value") or "").strip()
        if current_value:
            logging.info("Current appointment date pre-filled on form: %s", current_value)

        # Try multiple methods to open the calendar
        calendar_opened = False
        try:
            date_input.click()
            time.sleep(0.3)
            calendar_opened = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS) is not None
        except (WebDriverException, ElementNotInteractableException):
            logging.debug("Direct click on date input failed, trying alternatives")

        # If direct click didn't work, try clicking the calendar icon
        if not calendar_opened:
            try:
                calendar_icon = self._find_element(self.CALENDAR_ICON_SELECTORS, wait_time=2)
                if calendar_icon:
                    logging.debug("Attempting to open calendar via calendar icon")
                    calendar_icon.click()
                    time.sleep(0.3)
                    calendar_opened = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS) is not None
            except (WebDriverException, ElementNotInteractableException):
                pass

        # Last resort: JavaScript click
        if not calendar_opened:
            try:
                logging.debug("Using JavaScript to trigger calendar")
                driver.execute_script("arguments[0].focus(); arguments[0].click();", date_input)
                time.sleep(0.3)
            except Exception as exc:
                logging.warning("Failed to open calendar with all methods: %s", exc)

        time.sleep(0.5)  # Allow calendar to fully render

        available_slots = self._collect_available_dates(max_months=3)
        if available_slots:
            logging.info("Discovered available appointment dates: %s", ", ".join(available_slots))
            # Check if any available slots are better than current appointment
            self._evaluate_available_dates(available_slots)
        else:
            logging.info("No selectable appointment dates found in the scanned calendar window")

        time_select = self._find_element(self.CONSULATE_TIME_SELECTORS, wait_time=3)
        if time_select:
            times = [
                option.text.strip()
                for option in Select(time_select).options
                if option.get_attribute("value")
            ]
            if times:
                logging.info("Available appointment times for selected date: %s", ", ".join(times))
            else:
                logging.info("No appointment times loaded yet (date may not be selected or availability is empty)")
        
        # Track navigation performance
        nav_time = (datetime.now() - check_start).total_seconds()
        self._track_performance('availability_check', nav_time)

    def _collect_available_dates(self, max_months: int = 3) -> List[str]:
        available: List[str] = []

        calendar = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS)
        if not calendar:
            logging.info("Calendar widget did not open; assuming no selectable dates available")
            return available

        for month_index in range(max_months):
            if calendar is None or not calendar.is_displayed():
                break

            title_elements = calendar.find_elements(By.CSS_SELECTOR, ".ui-datepicker-title")
            month_label = title_elements[0].text.strip() if title_elements else f"Month {month_index + 1}"

            day_links = calendar.find_elements(By.CSS_SELECTOR, "table.ui-datepicker-calendar td:not(.ui-state-disabled) a")
            for link in day_links:
                day_text = link.text.strip()
                if not day_text:
                    continue
                available.append(f"{month_label} {day_text}")

            next_buttons = calendar.find_elements(By.CSS_SELECTOR, ".ui-datepicker-next:not(.ui-state-disabled)")
            if month_index == max_months - 1 or not next_buttons:
                break

            try:
                next_buttons[0].click()
            except WebDriverException:
                logging.debug("Failed to advance to next month in calendar")
                break

            time.sleep(0.5)
            calendar = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS)
            if not calendar:
                break

        return available

    def _evaluate_available_dates(self, available_slots: List[str]) -> None:
        """Evaluate discovered dates against current appointment and user preferences.
        
        Sends notifications when better appointments are found.
        """
        try:
            current_date = datetime.strptime(self.cfg.current_appointment_date, "%Y-%m-%d")
            start_date = datetime.strptime(self.cfg.start_date, "%Y-%m-%d")
            end_date = datetime.strptime(self.cfg.end_date, "%Y-%m-%d")
        except ValueError as exc:
            logging.warning("Invalid date format in configuration: %s", exc)
            return

        earlier_dates = []
        dates_in_range = []
        
        for slot in available_slots:
            # Parse the slot format "Month Year Day" (e.g., "January 2025 15")
            parsed_date = self._parse_calendar_date(slot)
            if not parsed_date:
                continue

            # Record every discovered slot in the ledger
            slot_key = parsed_date.strftime("%Y-%m-%d")
            self._slot_ledger.record_slot(slot_key, self.cfg.location)

            if self._is_excluded_date(parsed_date):
                logging.debug("Skipping excluded slot date %s", parsed_date.strftime("%Y-%m-%d"))
                continue

            # Check if date is within user's preferred range
            if start_date <= parsed_date <= end_date:
                dates_in_range.append(parsed_date)
                
                # Check if date is earlier than current appointment
                if parsed_date < current_date:
                    earlier_dates.append(parsed_date)

        if earlier_dates:
            earliest = min(earlier_dates)
            days_earlier = (current_date - earliest).days

            # ---- Min-improvement gate ----
            if days_earlier < self.cfg.min_improvement_days:
                logging.info(
                    "Earlier date %s is only %d days sooner (threshold: %d) — skipping",
                    earliest.strftime("%Y-%m-%d"), days_earlier, self.cfg.min_improvement_days,
                )
                return

            # ---- Dedup via slot ledger ----
            slot_key = earliest.strftime("%Y-%m-%d")
            
            logging.info("🎉 EARLIER APPOINTMENT FOUND! %s (%.0f days earlier than current)", 
                        earliest.strftime("%Y-%m-%d"), days_earlier)
            self._audio_alert("earlier appointment found")
            
            # Record this availability event
            self._record_availability_event("earlier_date_found")
            
            # Send notification
            subject = f"🎉 Earlier Visa Appointment Available! ({days_earlier} days earlier)"
            message = (
                f"An earlier visa appointment has been found!\n\n"
                f"📅 Available Date: {earliest.strftime('%B %d, %Y')}\n"
                f"📍 Location: {self.cfg.location}\n"
                f"⏰ Days Earlier: {days_earlier} days\n\n"
                f"Current Appointment: {self.cfg.current_appointment_date}\n"
                f"Target Range: {self.cfg.start_date} to {self.cfg.end_date}\n\n"
                f"All earlier dates found: {', '.join(d.strftime('%Y-%m-%d') for d in sorted(earlier_dates))}\n\n"
                f"{'🤖 Auto-book is ENABLED — attempting to book...' if self.cfg.auto_book else '⚠️ Login to book manually: https://ais.usvisa-info.com'}"
            )
            send_notification(self.cfg, subject, message)
            self._slot_ledger.mark_notified(slot_key, self.cfg.location)
            
            # ---- Auto-book pipeline (cascade) ----
            if self.cfg.auto_book:
                self._attempt_auto_book_cascade(sorted(earlier_dates), current_date)
                
        elif dates_in_range:
            logging.info("Found %d dates in target range, but none earlier than current appointment (%s)", 
                        len(dates_in_range), self.cfg.current_appointment_date)
        else:
            logging.debug("No available dates fall within target range %s to %s", 
                         self.cfg.start_date, self.cfg.end_date)

    def _parse_calendar_date(self, slot: str) -> Optional[datetime]:
        """Parse calendar date string like 'January 2025 15' into datetime."""
        try:
            # Try common formats
            # Format: "Month Year Day" (e.g., "January 2025 15")
            parts = slot.split()
            if len(parts) >= 3:
                month_str = parts[0]
                year_str = parts[1]
                day_str = parts[-1]
                
                # Handle cases where year might be part of month string
                date_str = f"{month_str} {day_str}, {year_str}"
                return datetime.strptime(date_str, "%B %d, %Y")
        except (ValueError, IndexError):
            pass
        
        # Try other formats
        for fmt in ["%B %Y %d", "%B %d, %Y", "%Y-%m-%d", "%d %B %Y"]:
            try:
                return datetime.strptime(slot.strip(), fmt)
            except ValueError:
                continue
        
        logging.debug("Could not parse date from calendar slot: %s", slot)
        return None

    # ------------------------------------------------------------------
    # Auto-book pipeline
    # ------------------------------------------------------------------
    RESCHEDULE_SUBMIT_SELECTORS: List[Selector] = [
        (By.ID, "appointments_submit"),
        (By.CSS_SELECTOR, "input[name='commit'][type='submit']"),
        (By.CSS_SELECTOR, "#appointments_submit"),
        (By.XPATH, "//input[@type='submit' and contains(@value, 'Reschedule')]"),
        (By.XPATH, "//input[@type='submit' and contains(@value, 'Schedule')]"),
    ]

    CONFIRM_YES_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, "a.button.alert[href*='confirm']"),
        (By.LINK_TEXT, "Confirm"),
        (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm')]"),
    ]

    def _attempt_auto_book_cascade(self, candidates: List[datetime], current_date: datetime) -> None:
        """Try to auto-book from a sorted list of candidate dates (earliest first)."""
        for target_date in candidates:
            days_earlier = (current_date - target_date).days
            if days_earlier < self.cfg.min_improvement_days:
                continue
            try:
                if self._attempt_auto_book(target_date, days_earlier):
                    return  # Success!
            except Exception as exc:  # noqa: BLE001
                logging.warning("Auto-book failed for %s, trying next: %s",
                               target_date.strftime("%Y-%m-%d"), exc)
        logging.warning("Auto-book cascade exhausted all %d candidates", len(candidates))

    def _pick_preferred_time(self, options) -> Any:
        """Choose the best time slot based on user preference (P2.3)."""
        pref = self.cfg.preferred_time.lower()
        if pref == "any" or len(options) <= 1:
            return options[0]

        def _time_score(option):
            text = option.text.strip()
            try:
                hour = int(text.split(":")[0])
            except (ValueError, IndexError):
                return 99
            if pref == "morning":
                return abs(hour - 9)
            elif pref == "afternoon":
                return abs(hour - 14)
            elif pref == "evening":
                return abs(hour - 17)
            return 0

        return min(options, key=_time_score)

    def _attempt_auto_book(self, target_date: datetime, days_earlier: int) -> bool:
        """Safely attempt to book the given date.

        Guardrails
        ----------
        * ``auto_book_dry_run`` — log every step but click nothing destructive.
        * ``auto_book_confirmation_wait_seconds`` — pause before final confirm,
          giving the user time to intervene.
        * ``min_improvement_days`` — already enforced by caller.
        """
        dry = self.cfg.auto_book_dry_run
        tag = "[DRY-RUN] " if dry else ""

        logging.info("%s🤖 Auto-book: targeting %s (%d days earlier)",
                     tag, target_date.strftime("%Y-%m-%d"), days_earlier)

        try:
            # Step 1 — Click the target day in the open datepicker
            target_day = str(target_date.day)
            target_month = target_date.strftime("%B")
            target_year = str(target_date.year)

            calendar = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS)
            if not calendar:
                # Re-open the calendar
                date_input = self._find_element(self.CONSULATE_DATE_INPUT_SELECTORS, wait_time=5)
                if date_input:
                    date_input.click()
                    time.sleep(0.5)
                    calendar = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS)

            if not calendar:
                logging.warning("%sAuto-book aborted: calendar not visible", tag)
                return False

            # Navigate calendar to the right month
            for _ in range(12):  # Max 12 months forward
                title_els = calendar.find_elements(By.CSS_SELECTOR, ".ui-datepicker-title")
                if title_els:
                    cal_title = title_els[0].text.strip()
                    if target_month in cal_title and target_year in cal_title:
                        break
                next_btn = calendar.find_elements(
                    By.CSS_SELECTOR, ".ui-datepicker-next:not(.ui-state-disabled)"
                )
                if not next_btn:
                    logging.warning("%sAuto-book aborted: cannot navigate to %s %s",
                                    tag, target_month, target_year)
                    return False
                if dry:
                    logging.info("%sWould click next-month to reach %s %s", tag, target_month, target_year)
                    return True  # Dry-run success
                next_btn[0].click()
                time.sleep(0.5)
                calendar = self._is_selector_visible(self.DATEPICKER_CONTAINER_SELECTORS)
                if not calendar:
                    return

            # Click the target day
            day_links = calendar.find_elements(
                By.CSS_SELECTOR,
                "table.ui-datepicker-calendar td:not(.ui-state-disabled) a",
            )
            clicked = False
            for link in day_links:
                if link.text.strip() == target_day:
                    if dry:
                        logging.info("%sWould click day %s", tag, target_day)
                    else:
                        link.click()
                        logging.info("Clicked day %s in datepicker", target_day)
                    clicked = True
                    break

            if not clicked:
                logging.warning("%sAuto-book aborted: day %s not found in calendar", tag, target_day)
                return False

            if dry:
                logging.info("%sAuto-book DRY-RUN complete — no changes made", tag)
                send_notification(
                    self.cfg,
                    f"🧪 Auto-book DRY-RUN: {target_date.strftime('%B %d, %Y')}",
                    f"Dry-run completed successfully for {target_date.strftime('%B %d, %Y')}.\n"
                    f"Set AUTO_BOOK_DRY_RUN = False to enable real booking.",
                )
                return True

            time.sleep(1)

            # Step 2 — Select preferred time slot
            time_select = self._find_element(self.CONSULATE_TIME_SELECTORS, wait_time=5)
            if time_select:
                sel = Select(time_select)
                options = [o for o in sel.options if o.get_attribute("value")]
                if options:
                    chosen_option = self._pick_preferred_time(options)
                    sel.select_by_value(chosen_option.get_attribute("value"))
                    chosen_time = chosen_option.text.strip()
                    logging.info("Selected time slot: %s", chosen_time)
                else:
                    logging.warning("Auto-book aborted: no time slots available")
                    return False
            else:
                logging.warning("Auto-book aborted: time selector not found")
                return False

            # Step 3 — Wait for confirmation window (user can intervene)
            wait_secs = self.cfg.auto_book_confirmation_wait_seconds
            if wait_secs > 0:
                logging.info("⏳ Waiting %ds before submitting (Ctrl+C to abort)...", wait_secs)
                time.sleep(wait_secs)

            # Step 4 — Click submit / reschedule
            submit_btn = self._find_element(self.RESCHEDULE_SUBMIT_SELECTORS, wait_time=5)
            if submit_btn:
                submit_btn.click()
                logging.info("Clicked submit/reschedule button")
            else:
                logging.warning("Auto-book aborted: submit button not found")
                return False

            time.sleep(2)

            # Step 5 — Handle confirmation dialog if present
            confirm_btn = self._find_element(self.CONFIRM_YES_SELECTORS, wait_time=5)
            if confirm_btn:
                confirm_btn.click()
                logging.info("Confirmed reschedule")

            time.sleep(2)

            # Step 6 — Verify and notify
            send_notification(
                self.cfg,
                f"✅ AUTO-BOOKED: {target_date.strftime('%B %d, %Y')} at {chosen_time}",
                f"Your visa appointment has been automatically rescheduled!\n\n"
                f"📅 New Date: {target_date.strftime('%B %d, %Y')}\n"
                f"🕐 Time: {chosen_time}\n"
                f"📍 Location: {self.cfg.location}\n"
                f"⏰ {days_earlier} days earlier than previous\n\n"
                f"⚠️ Please verify at https://ais.usvisa-info.com",
            )
            logging.info("✅ Auto-book completed for %s at %s",
                         target_date.strftime("%Y-%m-%d"), chosen_time)
            self._slot_ledger.mark_booked(target_date.strftime("%Y-%m-%d"), self.cfg.location)
            return True

        except Exception as exc:  # noqa: BLE001
            logging.exception("Auto-book failed: %s", exc)
            send_notification(
                self.cfg,
                "❌ Auto-book FAILED",
                f"Auto-book attempted for {target_date.strftime('%B %d, %Y')} but failed.\n"
                f"Error: {exc}\n\nPlease book manually: https://ais.usvisa-info.com",
            )
            return False

    def _is_selector_visible(self, selectors: List[Selector]):
        driver = self.ensure_driver()
        for by, value in selectors:
            elements = driver.find_elements(by, value)
            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except StaleElementReferenceException:
                    continue
        return None

    # ------------------------------------------------------------------
    # Supporting helpers
    # ------------------------------------------------------------------
    def _wait_for_page_ready(self, driver: webdriver.Chrome, timeout: int = 30) -> None:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def _dismiss_overlays(self) -> None:
        driver = self.ensure_driver()
        for by, value in self.COOKIE_SELECTORS:
            elements = driver.find_elements(by, value)
            for element in elements:
                if not element.is_enabled() or not element.is_displayed():
                    continue
                try:
                    self._scroll_into_view(element)
                    element.click()
                    logging.info("Dismissed overlay via selector %s=%s", by, value)
                    time.sleep(0.5)
                    return
                except (WebDriverException, StaleElementReferenceException):
                    logging.debug("Failed to dismiss overlay with %s=%s", by, value)
                    continue

    def _await_login_transition(self, driver: webdriver.Chrome) -> None:
        def login_state(drv: webdriver.Chrome) -> bool:
            url = drv.current_url.lower()
            if "dashboard" in url or "schedule" in url or "groups" in url:
                return True
            if "sign_in" not in url:
                return True
            for by, value in self.ALERT_SELECTORS:
                for alert in drv.find_elements(by, value):
                    if alert.is_displayed() and alert.text.strip():
                        return True
            return False

        try:
            WebDriverWait(driver, 40).until(login_state)
        except TimeoutException:
            logging.warning("Login transition timed out; continuing with best effort.")

        if "sign_in" in driver.current_url.lower():
            self._log_alerts()
            # Check for specific error patterns that might indicate temporary issues
            alerts = []
            for by, value in self.ALERT_SELECTORS:
                for alert in driver.find_elements(by, value):
                    if alert.is_displayed() and alert.text.strip():
                        alert_text = alert.text.strip().lower()
                        alerts.append(alert_text)
                        
            # If we see "sign in or sign up" message, it might be a rate limit or session issue
            if any("sign in or sign up" in alert for alert in alerts):
                logging.warning("Detected 'sign in or sign up' message - possible rate limiting or session conflict")
                # Add a delay and reset driver to clear any session conflicts
                time.sleep(5)
                self.reset_driver()
                raise RuntimeError("Login blocked - possible rate limiting, retrying with fresh session")
            else:
                raise RuntimeError("Login failed - check credentials, CAPTCHA, or website changes")

        logging.info("Login flow appears successful; current URL: %s", driver.current_url)

    def _log_alerts(self) -> None:
        driver = self.ensure_driver()
        for by, value in self.ALERT_SELECTORS:
            for alert in driver.find_elements(by, value):
                if alert.text.strip():
                    logging.error("Page alert: %s", alert.text.strip())

    def _accept_privacy_policy(self) -> None:
        """Ensure the privacy policy confirmation checkbox is checked before submitting.
        
        The AIS website uses iCheck library which hides the actual checkbox input
        and replaces it with a styled div. We need to click the label or wrapper div.
        """
        driver = self.ensure_driver()

        # First, try to find and click the label or iCheck wrapper (preferred method)
        # The actual checkbox is hidden by iCheck, so clicking the label/wrapper is more reliable
        label = self._find_element(self.PRIVACY_LABEL_SELECTORS, wait_time=5)
        if label:
            self._scroll_into_view(label)
            try:
                label.click()
                logging.info("Accepted privacy policy via label/wrapper click")
                time.sleep(0.5)  # Give iCheck time to update the checkbox state
                return
            except WebDriverException:
                logging.debug("Label click failed; attempting scripted click")
                try:
                    driver.execute_script("arguments[0].click();", label)
                    logging.info("Accepted privacy policy via scripted label click")
                    time.sleep(0.5)
                    return
                except WebDriverException:
                    logging.debug("Scripted label click also failed")

        # Fallback: try to find the hidden checkbox and use JavaScript to check it
        checkbox = self._find_element_raw(self.PRIVACY_CHECKBOX_SELECTORS, wait_time=3)
        if checkbox:
            try:
                # Check if already selected
                if checkbox.is_selected():
                    logging.debug("Privacy policy checkbox already selected")
                    return
                
                # Use JavaScript to check the hidden checkbox directly
                driver.execute_script("arguments[0].checked = true; arguments[0].click();", checkbox)
                logging.info("Privacy policy checkbox selected via JavaScript")
                time.sleep(0.5)
                return
            except WebDriverException as exc:
                logging.debug("JavaScript checkbox selection failed: %s", exc)

        logging.warning("Privacy policy checkbox/label not found; attempting to continue anyway")

    def _enter_text(self, element, value: str) -> None:
        try:
            element.clear()
        except Exception:  # noqa: BLE001
            pass
        element.send_keys(value)

    def _scroll_into_view(self, element) -> None:
        driver = self.ensure_driver()
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                element,
            )
        except WebDriverException:
            logging.debug("Unable to scroll element into view; continuing anyway.")

    def _find_or_raise(
        self,
        selectors: List[Selector],
        description: str,
        *,
        wait_time: int = 10,
        clickable: bool = False,
    ):
        element = self._find_element(selectors, wait_time=wait_time, clickable=clickable)
        if element is None:
            raise RuntimeError(
                f"{description.capitalize()} not found - the AIS website layout may have changed"
            )
        self._scroll_into_view(element)
        return element

    def _cache_form_elements(self, force_refresh: bool = False):
        """Cache commonly used form elements to avoid repeated searches"""
        if force_refresh or not hasattr(self, '_cached_elements'):
            self._cached_elements = {}
        
        # Cache location selector if not already cached or if refresh forced
        if force_refresh or 'location_select' not in self._cached_elements:
            location_element = self._find_element_raw(self.LOCATION_SELECTORS, wait_time=5)
            if location_element:
                self._cached_elements['location_select'] = location_element
                logging.debug("Cached location selector element")

    def _find_element_raw(
        self,
        selectors: List[Selector],
        *,
        wait_time: int = 10,
        clickable: bool = False,
    ):
        """Raw element finding without caching"""
        driver = self.ensure_driver()
        driver.switch_to.default_content()
        contexts = [None] + driver.find_elements(By.TAG_NAME, "iframe")

        for frame in contexts:
            try:
                driver.switch_to.default_content()
                if frame is not None:
                    WebDriverWait(driver, wait_time).until(EC.frame_to_be_available_and_switch_to_it(frame))
                for by, value in selectors:
                    try:
                        wait = WebDriverWait(driver, wait_time)
                        if clickable:
                            element = wait.until(EC.element_to_be_clickable((by, value)))
                        else:
                            element = wait.until(EC.visibility_of_element_located((by, value)))
                        if element:
                            return element
                    except TimeoutException:
                        continue
            except TimeoutException:
                continue
            finally:
                driver.switch_to.default_content()

        return None

    def _find_element(
        self,
        selectors: List[Selector],
        *,
        wait_time: int = 10,
        clickable: bool = False,
        use_cache: bool = True,
    ):
        """Enhanced element finding with optional caching"""
        # For commonly used elements, try cache first
        if use_cache and hasattr(self, '_cached_elements'):
            if selectors == self.LOCATION_SELECTORS and 'location_select' in self._cached_elements:
                try:
                    element = self._cached_elements['location_select']
                    # Verify element is still valid
                    element.is_enabled()  # This will throw if element is stale
                    return element
                except (StaleElementReferenceException, WebDriverException):
                    # Remove stale element from cache
                    del self._cached_elements['location_select']
        
        return self._find_element_raw(selectors, wait_time=wait_time, clickable=clickable)

    # ------------------------------------------------------------------
    # Network health utilities
    # ------------------------------------------------------------------
    _NETWORK_ERROR_PATTERNS = (
        "err_name_not_resolved",
        "name resolution",
        "dns_probe",
        "err_connection_refused",
        "err_connection_timed_out",
        "err_internet_disconnected",
        "err_network_changed",
        "err_address_unreachable",
        "temporary failure in name resolution",
        "network is unreachable",
        "no address associated",
        "could not resolve host",
        "connection timed out",
    )

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        """Return True if *exc* looks like a DNS / connectivity failure."""
        msg = str(exc).lower()
        return any(p in msg for p in VisaAppointmentChecker._NETWORK_ERROR_PATTERNS)

    def _check_internet_connectivity(self, timeout: float = 5.0) -> bool:
        """Quick TCP connectivity probe (no HTTP overhead).

        Tries connecting to well-known DNS resolvers on port 53 as a
        lightweight 'can we reach the internet?' check.
        """
        targets = [("8.8.8.8", 53), ("1.1.1.1", 53), ("208.67.222.222", 53)]
        for host, port in targets:
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    return True
            except OSError:
                continue
        return False

    def _record_network_success(self) -> None:
        """Reset network error counters after a successful operation."""
        if self._consecutive_network_errors > 0:
            logging.info(
                "🌐 Network recovered after %d consecutive failures",
                self._consecutive_network_errors,
            )
        self._consecutive_network_errors = 0
        self._last_successful_network_time = datetime.now()
        self._network_backoff_until = None

    def _record_network_failure(self) -> int:
        """Increment network error counter and compute backoff.

        Returns the recommended sleep time in seconds before the next
        attempt so that callers can implement exponential back-off.
        """
        self._consecutive_network_errors += 1
        n = self._consecutive_network_errors

        # Exponential back-off capped at 15 minutes
        backoff_seconds = min(60 * 15, 30 * (2 ** min(n - 1, 5)))
        # Add some jitter
        backoff_seconds += random.randint(0, min(30, backoff_seconds // 4))

        self._network_backoff_until = datetime.now() + timedelta(seconds=backoff_seconds)
        logging.warning(
            "🌐 Network failure #%d — backing off %d s (until %s)",
            n,
            backoff_seconds,
            self._network_backoff_until.strftime("%H:%M:%S"),
        )
        return backoff_seconds

    def _ensure_vpn_ready(self, reason: str) -> bool:
        if not self._vpn_manager:
            return True
        return self._vpn_manager.ensure_connected(reason=reason)

    def _handle_vpn_network_issue(self) -> None:
        if not self._vpn_manager:
            return
        self._vpn_manager.handle_network_issue(reason="network error")

    def _handle_vpn_captcha(self) -> None:
        if not self._vpn_manager:
            return
        self._vpn_manager.handle_captcha_block()

    def _parse_lockout_until(self, text: str) -> Optional[datetime]:
        if not text:
            return None

        normalized = re.sub(r"\s+", " ", text.strip())
        pattern = re.compile(
            r"(?:your account is locked until|account locked until|locked until)\s+"
            r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+),\s+(?P<year>\d{4}),\s+"
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+(?P<tz>[A-Z]{2,5})",
            re.IGNORECASE,
        )
        match = pattern.search(normalized)
        if not match:
            return None

        try:
            naive = datetime.strptime(
                f"{match.group('day')} {match.group('month')} {match.group('year')}, "
                f"{match.group('hour')}:{match.group('minute')}:{match.group('second')}",
                "%d %B %Y, %H:%M:%S",
            )
        except ValueError:
            return None

        tz_name = match.group("tz").upper()
        tz_map = {
            "CST": -6,
            "CDT": -5,
            "EST": -5,
            "EDT": -4,
            "MST": -7,
            "MDT": -6,
            "PST": -8,
            "PDT": -7,
            "UTC": 0,
            "GMT": 0,
        }

        aware = None
        if ZoneInfo is not None:
            try:
                if tz_name in {"CST", "CDT"}:
                    aware = naive.replace(tzinfo=ZoneInfo("America/Chicago"))
                elif tz_name in {"EST", "EDT"}:
                    aware = naive.replace(tzinfo=ZoneInfo("America/New_York"))
                elif tz_name in {"MST", "MDT"}:
                    aware = naive.replace(tzinfo=ZoneInfo("America/Denver"))
                elif tz_name in {"PST", "PDT"}:
                    aware = naive.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
                elif tz_name in {"UTC", "GMT"}:
                    aware = naive.replace(tzinfo=ZoneInfo("UTC"))
            except Exception:  # noqa: BLE001
                aware = None

        if aware is None and tz_name in tz_map:
            aware = naive.replace(tzinfo=timezone(timedelta(hours=tz_map[tz_name])))

        if aware is None:
            return naive

        if ZoneInfo is not None:
            try:
                local_tz = ZoneInfo(self.cfg.timezone)
                return aware.astimezone(local_tz).replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                pass

        return aware.replace(tzinfo=None)

    def _raise_account_lockout(self, unlock_at: Optional[datetime], driver: Optional[webdriver.Chrome] = None) -> None:
        if unlock_at is None:
            unlock_at = self._now() + timedelta(hours=6)
        self._account_lockout_until = unlock_at
        logging.warning(
            "Account lockout detected; pausing checks until %s",
            unlock_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._audio_alert("account locked")
        send_notification(
            self.cfg,
            "🔒 Account Locked - Bot Paused",
            (
                "The AIS account appears locked, so the checker will pause automatically.\n\n"
                f"Unlock time: {unlock_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "The bot will not keep retrying while the lock is active."
            ),
        )
        raise AccountLockedError(
            f"AIS account locked until {unlock_at.strftime('%Y-%m-%d %H:%M:%S')}",
            unlock_at=unlock_at,
        )

    def _detect_account_lockout(self) -> Optional[datetime]:
        driver = self.ensure_driver()
        try:
            title = driver.title or ""
        except WebDriverException:
            title = ""
        try:
            source = driver.page_source or ""
        except WebDriverException:
            source = ""

        combined = f"{title}\n{source}"
        markers = (
            "your account is locked until",
            "account locked until",
            "locked until",
        )
        if any(marker in combined.lower() for marker in markers):
            unlock_at = self._parse_lockout_until(combined)
            self._raise_account_lockout(unlock_at, driver)
        return None

    def _safe_get(self, url: str, *, attempts: Optional[int] = None, detect_captcha: bool = False) -> None:
        driver = self.ensure_driver()
        total_attempts = attempts or self.cfg.max_retry_attempts
        last_exc: Optional[Exception] = None

        for attempt in range(1, total_attempts + 1):
            try:
                driver.get(url)
                self._wait_for_page_ready(driver)
                self._detect_account_lockout()
                if detect_captcha:
                    self._detect_captcha()
                self._record_network_success()
                return
            except AccountLockedError as exc:
                last_exc = exc
                logging.warning("Account lockout encountered while loading %s: %s", url, exc)
                self._record_network_success()
                break
            except CaptchaDetectedError as exc:
                last_exc = exc
                logging.warning("Captcha encountered while loading %s: %s", url, exc)
                self._record_network_success()  # reached server
                break
            except (WebDriverException, TimeoutException) as exc:
                last_exc = exc
                # For network errors, fail fast — retrying immediately
                # won't help.  The main loop handles the backoff.
                if self._is_network_error(exc):
                    logging.warning(
                        "Network error loading %s (attempt %s/%s): %s",
                        url, attempt, total_attempts, exc,
                    )
                    break
                if attempt == total_attempts:
                    break
                sleep_seconds = self.cfg.retry_backoff_seconds * attempt
                logging.warning(
                    "Navigation error for %s (attempt %s/%s): %s; retrying in %s seconds",
                    url,
                    attempt,
                    total_attempts,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)

        if last_exc and not isinstance(last_exc, (CaptchaDetectedError, AccountLockedError)) and not self._is_network_error(last_exc):
            self._capture_artifact("navigation_failure")
        if last_exc:
            raise last_exc

    def _detect_captcha(self) -> bool:
        driver = self.ensure_driver()
        try:
            iframe_candidates = driver.find_elements(By.TAG_NAME, "iframe")
        except WebDriverException:
            return False

        captcha_iframes = []
        for frame in iframe_candidates:
            try:
                src = (frame.get_attribute("src") or "").lower()
                title = (frame.get_attribute("title") or "").lower()
                if not src and not title:
                    continue
                if any(keyword in src for keyword in ("hcaptcha.com", "recaptcha", "turnstile")) or "captcha" in title:
                    if frame.is_displayed():
                        captcha_iframes.append(frame)
            except WebDriverException:
                continue

        widget_selectors = [
            ".g-recaptcha",
            ".grecaptcha-badge",
            ".h-captcha",
            ".cf-turnstile",
            "[data-sitekey][class*='captcha']",
            "div[aria-label*='captcha']",
        ]
        captcha_widgets = []
        for selector in widget_selectors:
            try:
                captcha_widgets.extend(
                    [element for element in driver.find_elements(By.CSS_SELECTOR, selector) if element.is_displayed()]
                )
            except WebDriverException:
                continue

        challenge_keywords = (
            "verify you are human",
            "i am not a robot",
            "please select all",
            "captcha challenge",
            "complete the security check",
        )
        page_source = ""
        try:
            page_source = driver.page_source.lower()
        except WebDriverException:
            page_source = ""

        page_mentions_challenge = any(keyword in page_source for keyword in challenge_keywords)

        if captcha_iframes or captcha_widgets or page_mentions_challenge:
            logging.warning("Captcha challenge detected on page; automation paused.")
            self._audio_alert("captcha/manual intervention required")
            self._capture_artifact("captcha_detected")
            self._schedule_backoff()
            message = (
                "Captcha detected - manual solve required"
                if self.cfg.abort_on_captcha
                else "Captcha detected - retry will be attempted after backoff"
            )
            raise CaptchaDetectedError(message)

        return False

    def _try_warning_continue(self, driver: webdriver.Chrome) -> bool:
        """Attempt to click the Continue acknowledgment button on the Scheduling Limit Warning page.

        Returns True if the button was found and clicked successfully.
        """
        for selector_by, selector_val in self.WARNING_CONTINUE_SELECTORS:
            try:
                el = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((selector_by, selector_val))
                )
                el.click()
                logging.info("Scheduling Limit Warning: clicked Continue acknowledgment button")
                try:
                    self._wait_for_page_ready(driver)
                except Exception as exc:  # noqa: BLE001
                    logging.debug("_wait_for_page_ready after Continue click raised: %s", exc)
                return True
            except (TimeoutException, NoSuchElementException, ElementClickInterceptedException,
                    ElementNotInteractableException, StaleElementReferenceException, WebDriverException) as exc:
                logging.debug(
                    "WARNING_CONTINUE selector (%s, %r) not usable: %s",
                    selector_by, selector_val, exc,
                )
                continue
        logging.debug("No Continue button found on Scheduling Limit Warning page")
        return False

    def _handle_scheduling_limit_warning(self, driver: webdriver.Chrome) -> None:
        """Handle the AIS 'Scheduling Limit Warning' page.

        First attempts the official Continue acknowledgment path.  If Continue
        is available and clickable, we acknowledge the warning and let the
        normal flow resume (no raise).  Only when Continue is unavailable or
        the warning persists in a loop do we apply escalating backoff and raise
        CaptchaDetectedError to require human intervention.
        """
        self._warning_page_hits += 1

        # --- Phase 1: try the official Continue acknowledgment path first ---
        if self._try_warning_continue(driver):
            self._continue_success_count += 1
            logging.info(
                "Scheduling Limit Warning acknowledged via Continue (hit #%d, "
                "success #%d); resuming normal flow.",
                self._warning_page_hits,
                self._continue_success_count,
            )
            return  # Do NOT raise; let the caller proceed normally.

        # Continue was not available — escalate with backoff.
        self._scheduling_limit_count += 1
        consecutive = self._scheduling_limit_count

        # Escalating backoff: 30 min base, doubled for each additional hit,
        # capped at SCHEDULING_LIMIT_MAX_BACKOFF_MINUTES.
        base_minutes = 30
        backoff_minutes = min(
            base_minutes * (2 ** (consecutive - 1)),
            self.SCHEDULING_LIMIT_MAX_BACKOFF_MINUTES,
        )
        self._backoff_until = datetime.now() + timedelta(minutes=backoff_minutes)

        logging.warning(
            "Scheduling Limit Warning detected (consecutive count: %d); "
            "Continue unavailable — human CAPTCHA intervention required. Next retry in %d minutes.",
            consecutive,
            backoff_minutes,
        )
        self._audio_alert("scheduling limit warning")

        # Notify the user on the first occurrence (and every 3rd thereafter)
        if consecutive == 1 or consecutive % 3 == 0:
            send_notification(
                self.cfg,
                "⚠️ Scheduling Limit Warning — Manual Action Required",
                (
                    "The AIS portal is showing a 'Scheduling Limit Warning' page that "
                    "requires you to solve a CAPTCHA before the bot can continue.\n\n"
                    f"Please log in manually at {self._login_target()} and complete "
                    "the CAPTCHA challenge on the scheduling page.\n\n"
                    f"The bot will automatically retry in {backoff_minutes} minutes. "
                    f"(This is consecutive occurrence #{consecutive}.)"
                ),
            )

        raise CaptchaDetectedError(
            f"Scheduling limit warning (#{consecutive}) — human CAPTCHA required; "
            f"retry in {backoff_minutes} minutes"
        )

    def _schedule_backoff(self) -> None:
        # Use adaptive frequency if available, otherwise fall back to configured frequency
        user_frequency = getattr(self, '_adaptive_frequency', self.cfg.check_frequency_minutes)
        
        # Strategic optimization: Reduce backoff during prime time
        prime_time_multiplier = 1.0
        if self._is_prime_time():
            prime_time_multiplier = self.cfg.prime_time_backoff_multiplier
            logging.debug("Applying prime time backoff reduction: %.1fx", prime_time_multiplier)
        
        # Factor in busy streak for more intelligent backoff
        busy_multiplier = 1.0 + (self._busy_streak_count * 0.2)  # Increase by 20% per busy streak
        
        # Set backoff range based on frequency: 
        # - For frequent checks (< 5 min): use longer backoff to avoid overloading
        # - For moderate checks (5-30 min): use moderate backoff 
        # - For infrequent checks (> 30 min): use shorter backoff
        if user_frequency < 5:
            min_backoff = max(10, user_frequency * 3 * busy_multiplier * prime_time_multiplier)
            max_backoff = max(20, user_frequency * 5 * busy_multiplier * prime_time_multiplier)
        elif user_frequency <= 30:
            min_backoff = max(user_frequency, 8 * busy_multiplier * prime_time_multiplier)
            max_backoff = max(user_frequency * 2, 15 * busy_multiplier * prime_time_multiplier)
        else:
            min_backoff = max(5, user_frequency // 2 * busy_multiplier * prime_time_multiplier)
            max_backoff = max(10, user_frequency * busy_multiplier * prime_time_multiplier)
        
        # Ensure minimum backoff of 2 minutes during prime time
        if self._is_prime_time():
            min_backoff = max(2, min_backoff)
            max_backoff = max(min_backoff + 1, max_backoff)
        
        delay_minutes = random.randint(int(min_backoff), int(max_backoff))
        self._backoff_until = datetime.now() + timedelta(minutes=delay_minutes)
        
        # Dynamic messaging based on user frequency and busy streak
        if self._busy_streak_count > 0:
            reason = f"persistent busy status (streak: {self._busy_streak_count}, freq: {user_frequency:.1f}m)"
        elif user_frequency < 5:
            reason = f"frequent checking (every {user_frequency:.1f}m)"
        elif user_frequency <= 15:
            reason = f"moderate checking frequency ({user_frequency:.1f}m)"
        else:
            reason = f"current check interval ({user_frequency:.1f}m)"
        
        if self._is_prime_time():
            reason += " [PRIME TIME - reduced backoff]"
            
        logging.info("Backoff scheduled for %s minutes due to busy calendar response (adjusted for %s)", 
                    delay_minutes, reason)

    def _capture_artifact(self, label: str) -> None:
        driver = self.driver
        if driver is None:
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_label = label.replace(" ", "_")
        base = ARTIFACTS_DIR / f"{timestamp}_{safe_label}"

        try:
            base.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
            logging.info("📄 Saved HTML artifact: %s", base.with_suffix(".html"))
        except Exception as exc:  # noqa: BLE001
            logging.debug("Failed to persist page source artifact: %s", exc)

        try:
            driver.save_screenshot(str(base.with_suffix(".png")))
            logging.info("📸 Saved screenshot: %s", base.with_suffix(".png"))
        except Exception as exc:  # noqa: BLE001
            logging.debug("Failed to capture screenshot artifact: %s", exc)

    def _capture_debug_state(self, label: str) -> None:
        """Capture comprehensive debug information about current page state.
        
        This captures:
        - Current URL
        - Page title
        - Key element visibility
        - Screenshot and HTML
        - Console logs (if available)
        """
        driver = self.driver
        if driver is None:
            logging.debug("Cannot capture debug state: driver is None")
            return

        logging.info("=" * 60)
        logging.info("🔍 DEBUG STATE CAPTURE: %s", label)
        logging.info("=" * 60)
        
        # Basic page info
        try:
            logging.info("📍 Current URL: %s", driver.current_url)
            logging.info("📄 Page Title: %s", driver.title)
        except Exception as exc:
            logging.warning("Failed to get basic page info: %s", exc)

        # Check for key elements
        element_checks = [
            ("Location Selector", self.LOCATION_SELECTORS),
            ("Date Input", self.CONSULATE_DATE_INPUT_SELECTORS),
            ("Busy Message", self.CONSULATE_BUSY_SELECTORS),
            ("Appointment Form", self.APPOINTMENT_FORM_SELECTORS),
            ("Reschedule Button", self.RESCHEDULE_BUTTON_SELECTORS),
            ("Sign In Button", self.SIGN_IN_SELECTORS),
        ]
        
        logging.info("🔎 Element Visibility Check:")
        for name, selectors in element_checks:
            found = self._find_element(selectors, wait_time=2)
            status = "✅ FOUND" if found else "❌ NOT FOUND"
            if found:
                try:
                    tag = found.tag_name
                    visible = found.is_displayed()
                    enabled = found.is_enabled()
                    logging.info("   %s: %s (tag=%s, visible=%s, enabled=%s)", 
                               name, status, tag, visible, enabled)
                except Exception:
                    logging.info("   %s: %s (could not get details)", name, status)
            else:
                logging.info("   %s: %s", name, status)

        # Check for common page indicators
        try:
            page_source = driver.page_source.lower()
            indicators = [
                ("Login Form", "user[email]" in page_source or "sign_in" in page_source),
                ("Appointment Form", "consulate_appointment" in page_source),
                ("Calendar Busy", "not_available" in page_source or "no appointments" in page_source.lower()),
                ("CAPTCHA", "captcha" in page_source or "recaptcha" in page_source),
                ("Error Message", "error" in page_source and "alert" in page_source),
            ]
            logging.info("🔎 Page Content Indicators:")
            for name, present in indicators:
                status = "✅ PRESENT" if present else "❌ NOT PRESENT"
                logging.info("   %s: %s", name, status)
        except Exception as exc:
            logging.warning("Failed to check page content indicators: %s", exc)

        # Try to get console logs
        try:
            logs = driver.get_log('browser')
            if logs:
                logging.info("🖥️ Browser Console Logs (last 5):")
                for log in logs[-5:]:
                    logging.info("   [%s] %s", log.get('level', 'UNKNOWN'), log.get('message', '')[:200])
        except Exception:
            pass  # Not all browsers support this

        # Capture artifacts
        self._capture_artifact(f"debug_{label}")
        
        logging.info("=" * 60)

    def _update_heartbeat(self, status: str) -> None:
        if not self._heartbeat_path:
            return

        api_total = self._api_check_count
        ui_total = self._ui_check_count
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "warning_page_hits": self._warning_page_hits,
            "continue_success_count": self._continue_success_count,
            "continue_success_rate": (
                round(self._continue_success_count / self._warning_page_hits, 3)
                if self._warning_page_hits > 0
                else None
            ),
            "api_checks": api_total,
            "ui_checks": ui_total,
            "api_vs_ui_ratio": (
                round(api_total / (api_total + ui_total), 3)
                if (api_total + ui_total) > 0
                else None
            ),
        }

        try:
            self._heartbeat_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logging.debug("Failed to write heartbeat file: %s", exc)

    def _calculate_dynamic_backoff(self) -> int:
        """Calculate backoff based on error patterns and success rate"""
        # Use adaptive frequency if available
        base_minutes = getattr(self, '_adaptive_frequency', self.cfg.check_frequency_minutes)
        
        # Track success rate over last 10 attempts
        if len(self._recent_results) >= 3:
            success_rate = sum(self._recent_results[-10:]) / len(self._recent_results[-10:])
        else:
            success_rate = 1.0  # Assume good performance initially
        
        # Increase backoff if success rate is low
        if success_rate < 0.5:
            multiplier = 3  # Triple backoff for low success
        elif success_rate < 0.8:
            multiplier = 2  # Double for moderate success
        else:
            multiplier = 1  # Normal backoff for good success
        
        dynamic_minutes = base_minutes * multiplier
        logging.debug("Dynamic backoff: base=%.1fm, success_rate=%.2f, multiplier=%.1f, result=%.1fm", 
                     base_minutes, success_rate, multiplier, dynamic_minutes)
        
        return int(dynamic_minutes)

    def compute_sleep_seconds(self, base_minutes: int) -> int:
        sleep_seconds, self._backoff_until = compute_sleep_seconds_external(
            base_minutes=base_minutes,
            optimal_minutes=self._calculate_optimal_frequency(),
            dynamic_backoff_minutes=self._calculate_dynamic_backoff(),
            sleep_jitter_seconds=self.cfg.sleep_jitter_seconds,
            is_prime_time=self._is_prime_time(),
            backoff_until=self._backoff_until,
        )
        if self.cfg.safety_first_mode:
            min_interval = max(1, self.cfg.safety_first_min_interval_minutes) * 60
            sleep_seconds = max(sleep_seconds, min_interval)
        return sleep_seconds

    def _track_performance(self, operation: str, duration: float):
        """Track performance metrics for various operations"""
        if operation not in self._metrics:
            self._metrics[operation] = []
        
        self._metrics[operation].append(duration)
        
        # Keep only last 20 measurements to avoid memory bloat
        if len(self._metrics[operation]) > 20:
            self._metrics[operation] = self._metrics[operation][-20:]
        
        # Log performance stats every 10 measurements
        if len(self._metrics[operation]) % 10 == 0:
            recent_measurements = self._metrics[operation][-10:]
            avg_time = sum(recent_measurements) / len(recent_measurements)
            max_time = max(recent_measurements)
            min_time = min(recent_measurements)
            logging.info("Performance stats [%s]: avg=%.2fs, min=%.2fs, max=%.2fs", 
                        operation, avg_time, min_time, max_time)

    def _cleanup_artifacts(self):
        """Remove old artifacts to prevent disk bloat"""
        try:
            html_files = list(ARTIFACTS_DIR.glob("*.html"))
            if len(html_files) > 50:
                # Sort by creation time and remove oldest files
                html_files.sort(key=lambda x: x.stat().st_ctime)
                old_files = html_files[:30]
                for file_path in old_files:
                    try:
                        file_path.unlink()
                        # Also remove corresponding PNG file
                        png_path = file_path.with_suffix('.png')
                        if png_path.exists():
                            png_path.unlink()
                    except Exception:
                        pass
                logging.debug("Cleaned up %d old artifact files", len(old_files))
        except Exception as exc:
            logging.debug("Artifact cleanup failed: %s", exc)

    def post_check(self, *, success: bool) -> None:
        self._checks_since_restart += 1
        self._update_heartbeat("success" if success else "failure")
        
        # Track success rate
        self._recent_results.append(1 if success else 0)
        if len(self._recent_results) > 10:
            self._recent_results = self._recent_results[-10:]

        # Periodic cleanup every 10 checks
        if self._checks_since_restart % 10 == 0:
            self._cleanup_artifacts()
            # Purge expired slot ledger entries (P2.2)
            self._slot_ledger.purge_expired(self.cfg.slot_ttl_hours)

        # Restart driver with increased threshold for better performance
        restart_threshold = max(50, self.cfg.driver_restart_checks)
        if self._checks_since_restart >= restart_threshold:
            logging.info(
                "Restarting browser driver after %s checks to mitigate resource usage",
                restart_threshold,
            )
            self.reset_driver()
            self._checks_since_restart = 0

    def _handle_error(self, exc: Exception) -> None:
        signature = f"{type(exc).__name__}:{str(exc)}"
        now = datetime.now(timezone.utc)

        # Skip heavy artifact capture for network errors (nothing useful to capture)
        if not self._is_network_error(exc):
            self._capture_artifact(f"error_{type(exc).__name__.lower()}")

        # If this is a rate limiting or login issue, schedule a longer backoff
        error_message = str(exc).lower()
        if any(keyword in error_message for keyword in ["rate limiting", "sign in or sign up", "login blocked"]):
            # Schedule a longer backoff for login issues
            backoff_minutes = max(15, self.cfg.check_frequency_minutes * 3)
            self._backoff_until = datetime.now() + timedelta(minutes=backoff_minutes)
            logging.warning("Login rate limiting detected, scheduling %s minute backoff", backoff_minutes)

        # Don't attempt to send email notifications for network errors —
        # the email would fail too and just add noise.
        if self._is_network_error(exc):
            logging.info("Skipping error notification — network is unreachable")
            return

        notify = True
        if self._last_error_signature == signature and self._last_notification_time:
            elapsed = now - self._last_notification_time
            if elapsed < timedelta(minutes=30):
                logging.info(
                    "Skipping duplicate notification (last sent %.1f minutes ago)",
                    elapsed.total_seconds() / 60,
                )
                notify = False

        if notify:
            if send_notification(self.cfg, "Visa Appointment Checker Error", str(exc)):
                self._last_error_signature = signature
                self._last_notification_time = now


def main() -> None:
    parser = argparse.ArgumentParser(
        description="US Visa Appointment Checker",
        epilog=(
            "Examples:\n"
            "  python visa_appointment_checker.py --setup\n"
            "  python visa_appointment_checker.py --frequency 5\n"
            "  python visa_appointment_checker.py --no-headless --report-interval 3"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Launch guided CLI setup wizard to create/update config.ini.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="Emit structured JSON logs to console and log file.",
    )
    parser.add_argument(
        "--selectors-file",
        default="selectors.yml",
        help="Path to YAML selector registry (default: selectors.yml).",
    )
    parser.add_argument(
        "--frequency",
        type=int,
        default=None,
        help="Check frequency in minutes (overrides config.ini value)",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run Chrome in visible mode (useful for debugging or solving CAPTCHA).",
    )
    parser.add_argument(
        "--report-interval",
        type=float,
        default=6.0,
        help="Hours between progress report emails (default: 6). Set to 0 to disable.",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run exactly one check cycle and exit (scheduler/task-friendly mode).",
    )
    parser.set_defaults(headless=True)
    args = parser.parse_args()
    configure_logging(debug=args.debug, json_logs=args.json_logs)
    logging.info("Visa checker logs will rotate under %s", LOG_PATH.resolve())

    if args.setup:
        run_cli_setup_wizard()
        return

    try:
        cfg = CheckerConfig.load()
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logging.error("Configuration error: %s", exc)
        logging.error("Run 'python visa_appointment_checker.py --setup' to create config.ini")
        raise SystemExit(1) from exc

    frequency = max(1, args.frequency if args.frequency is not None else cfg.check_frequency_minutes)
    headless = args.headless
    report_interval = max(0, args.report_interval)

    print("\n🚀 US Visa Appointment Checker Started - OPTIMIZED")
    print("=" * 55)
    print(f"📅 Current appointment date: {cfg.current_appointment_date}")
    print(f"📍 Location: {cfg.location}")
    if cfg.multi_location_check and cfg.backup_locations:
        backup_locs = [loc.strip() for loc in cfg.backup_locations.split(',') if loc.strip()]
        print(f"🌎 Backup locations: {', '.join(backup_locs[:2])}{'...' if len(backup_locs) > 2 else ''}")
    print(f"⏱️  Base frequency: {frequency} minutes")
    print(f"📊 Progress reports: Every {report_interval:.1f} hours" if report_interval > 0 else "📊 Progress reports: Disabled")
    print(f"🎯 Strategic optimization: {'Enabled' if cfg.burst_mode_enabled else 'Disabled'}")
    print(f"🕐 Prime time optimization: {'Enabled' if cfg.prime_hours_start else 'Disabled'}")
    print(f"📧 Email notifications: {'Enabled' if cfg.is_smtp_configured() else 'Disabled'}")
    tg_on = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
    print(f"📨 Telegram notifications: {'Enabled' if tg_on else 'Disabled'}")
    wh_on = bool(cfg.webhook_url)
    print(f"🔗 Webhook notifications: {'Enabled' if wh_on else 'Disabled'}")
    po_on = bool(cfg.pushover_app_token and cfg.pushover_user_key)
    print(f"📱 Pushover notifications: {'Enabled' if po_on else 'Disabled'}")
    vpn_on = cfg.vpn_provider.lower() == "protonvpn"
    if vpn_on:
        vpn_target = cfg.vpn_server or (cfg.vpn_country or "fastest")
        print(
            f"🛡️ VPN: Proton VPN -> {vpn_target} "
            f"(require_connected={cfg.vpn_require_connected})"
        )
    else:
        print("🛡️ VPN: Disabled")
    print(f"🤖 Auto-book: {'Enabled' if cfg.auto_book else 'Disabled'}")
    if cfg.auto_book:
        print(f"   Dry-run: {'Yes' if cfg.auto_book_dry_run else 'No'} | Preferred time: {cfg.preferred_time}")
    print(f"🧪 Test mode: {'Enabled' if cfg.test_mode else 'Disabled'}")
    print(f"🛡️ Safety-first mode: {'Enabled' if cfg.safety_first_mode else 'Disabled'}")
    print(f"🔊 Audio alerts: {'Enabled' if cfg.audio_alerts_enabled else 'Disabled'}")
    print(f"⏰ Timezone: {cfg.timezone}")
    print(f"🕶️ Headless mode: {'On' if headless else 'Off'}")
    print("🔄 Config hot-reload: Enabled")
    print("=" * 55)

    logging.info("Configuration summary: %s", cfg.masked_summary())
    if cfg.heartbeat_path:
        logging.info("Heartbeat file: %s", cfg.heartbeat_path)

    checker = VisaAppointmentChecker(cfg, headless=headless, selectors_path=args.selectors_file)
    
    # Start progress reporter if interval > 0 and SMTP is configured
    reporter: Optional[ProgressReporter] = None
    if report_interval > 0 and cfg.is_smtp_configured():
        reporter = ProgressReporter(cfg, interval_hours=report_interval)
        reporter.start()

    check_count = 0
    try:
        while True:
            check_count += 1
            start_time = datetime.now()

            # ---- Config hot-reload (P3.2) ----
            checker._check_config_reload()
            checker._rotate_account_if_needed(check_count)

            # ---- Network pre-check ----
            if checker._account_lockout_until and datetime.now() < checker._account_lockout_until:
                wait_secs = (checker._account_lockout_until - datetime.now()).total_seconds()
                print(
                    f"🔒 Account locked — sleeping {int(wait_secs)}s "
                    f"(until {checker._account_lockout_until.strftime('%Y-%m-%d %H:%M:%S')})"
                )
                time.sleep(max(1, wait_secs))
                continue

            # If we are in a network-backoff window, wait it out before
            # burning a Chrome launch + page load cycle.
            if checker._network_backoff_until and datetime.now() < checker._network_backoff_until:
                wait_secs = (checker._network_backoff_until - datetime.now()).total_seconds()
                print(
                    f"🌐 Network backoff active — sleeping {int(wait_secs)}s "
                    f"(until {checker._network_backoff_until.strftime('%H:%M:%S')})"
                )
                time.sleep(max(1, wait_secs))
                # Quick connectivity probe before proceeding
                if not checker._check_internet_connectivity():
                    backoff = checker._record_network_failure()
                    print(f"🌐 Still offline — extending backoff by {backoff}s")
                    continue

            if not checker._ensure_vpn_ready(reason="pre-check"):
                retry_secs = max(30, checker.cfg.retry_backoff_seconds)
                print(
                    f"🛡️ Proton VPN required but not connected — retrying in {retry_secs}s"
                )
                time.sleep(retry_secs)
                continue

            print(f"\n🔄 Starting check #{check_count} at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 30)

            success = False
            captcha_detected = False
            network_error = False
            try:
                checker.perform_check()
                print(f"✅ Check #{check_count} completed successfully")
                success = True
                checker._record_network_success()
            except AccountLockedError as exc:
                print(f"🔒 Check #{check_count} paused due to account lock: {exc}")
            except UiRateLimitError as exc:
                print(f"⏸️ Check #{check_count} skipped due to UI rate limit: {exc}")
            except CaptchaDetectedError as exc:
                print(f"🤖 Check #{check_count} blocked by captcha: {exc}")
                captcha_detected = True
                # Captcha means we reached the server — network is fine
                checker._record_network_success()
                checker._handle_vpn_captcha()
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Check #{check_count} failed: {exc}")
                if checker._is_network_error(exc):
                    network_error = True
                    backoff = checker._record_network_failure()
                    print(f"🌐 Network error detected — will retry in {backoff}s")
                    checker._handle_vpn_network_issue()

            # Record stats for progress reporter
            if reporter:
                reporter.record_check(success=success, captcha=captcha_detected)

            checker.post_check(success=success)

            # If we just hit a network error, use the network backoff instead
            # of the normal sleep interval (which would be too short).
            if network_error and checker._network_backoff_until:
                remaining = (checker._network_backoff_until - datetime.now()).total_seconds()
                if remaining > 0:
                    print(
                        f"⏰ Network backoff until "
                        f"{checker._network_backoff_until.strftime('%H:%M:%S')}"
                    )
                    print("💤 Sleeping (network recovery)...")
                    time.sleep(remaining)
                    if args.run_once:
                        break
                    continue

            if args.run_once:
                print("✅ --run-once mode complete. Exiting after a single cycle.")
                break

            sleep_seconds = checker.compute_sleep_seconds(frequency)
            next_check = datetime.now() + timedelta(seconds=sleep_seconds)
            minutes, seconds = divmod(sleep_seconds, 60)
            print(
                f"⏰ Next check at: {next_check.strftime('%H:%M:%S')} (in {minutes}m {seconds}s)"
            )
            print("💤 Sleeping...")

            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("\n🛑 Stopping visa appointment checker (KeyboardInterrupt)")
    finally:
        if reporter:
            reporter.stop()
        checker.quit_driver()
        print("🧹 Browser session closed")


if __name__ == "__main__":
    main()
