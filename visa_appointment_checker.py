import argparse
import configparser
import json
import logging
import os
import random
import smtplib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin

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

LOGIN_URL = "https://ais.usvisa-info.com/en-ca/niv/users/sign_in"
RESCHEDULE_URLS = [
    "https://ais.usvisa-info.com/en-ca/niv/schedule/",
    "https://ais.usvisa-info.com/en-ca/niv/appointment",
    "https://ais.usvisa-info.com/en-ca/niv/",
]

# Keep webdriver-manager quiet unless user overrides
os.environ.setdefault("WDM_LOG_LEVEL", "0")

# Enable debug mode via environment variable
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "visa_checker.log"

ARTIFACTS_DIR = Path("artifacts")
ARTIFACTS_DIR.mkdir(exist_ok=True)

# Set log level based on debug mode
log_level = logging.DEBUG if DEBUG_MODE else logging.INFO

logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(),
    ],
)

# Suppress verbose third-party library logging to avoid log spam
# These libraries are extremely chatty at DEBUG level
for noisy_logger in [
    "selenium",
    "selenium.webdriver.remote.remote_connection",
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "PIL",
    "chardet",
]:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

if DEBUG_MODE:
    logging.info("🔍 DEBUG MODE ENABLED - Verbose logging active")

logging.info("Visa checker logs will rotate under %s", LOG_PATH.resolve())

Selector = Tuple[str, str]


class CaptchaDetectedError(RuntimeError):
    """Raised when the AIS site presents a CAPTCHA that blocks automation."""


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

    @classmethod
    def load(cls, path: str = "config.ini") -> "CheckerConfig":
        parser = configparser.ConfigParser()
        parser.optionxform = str

        if not parser.read(path):
            raise FileNotFoundError(
                f"Unable to load configuration. Expected file at '{path}'. "
                "Run configure.sh, the installer, or the web UI to create one."
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

        start_date = _get("START_DATE")
        end_date = _get("END_DATE")

        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError as exc:  # noqa: B904
            raise ValueError("START_DATE and END_DATE must be formatted as YYYY-MM-DD") from exc

        if start_dt > end_dt:
            raise ValueError("START_DATE must be earlier than or equal to END_DATE")

        def _get_float(key: str, fallback: Optional[float] = None) -> float:
            try:
                return float(_get(key, str(fallback) if fallback is not None else None))
            except ValueError as exc:  # noqa: B904
                raise ValueError(f"{key} must be a float") from exc

        return cls(
            email=_get("EMAIL"),
            password=_get("PASSWORD"),
            current_appointment_date=_get("CURRENT_APPOINTMENT_DATE"),
            location=_get("LOCATION"),
            start_date=start_date,
            end_date=end_date,
            check_frequency_minutes=frequency,
            smtp_server=_get("SMTP_SERVER", "smtp.gmail.com"),
            smtp_port=smtp_port,
            smtp_user=_get("SMTP_USER"),
            smtp_pass=_get("SMTP_PASS"),
            notify_email=_get("NOTIFY_EMAIL"),
            auto_book=_to_bool(_get("AUTO_BOOK", "False")),
            driver_restart_checks=max(1, _get_int("DRIVER_RESTART_CHECKS", 50)),  # Increased default
            heartbeat_path=os.getenv("HEARTBEAT_PATH", raw_defaults.get("HEARTBEAT_PATH")),
            max_retry_attempts=max(1, _get_int("MAX_RETRY_ATTEMPTS", 2)),  # Reduced default
            retry_backoff_seconds=max(1, _get_int("RETRY_BACKOFF_SECONDS", 5)),
            sleep_jitter_seconds=max(0, _get_int("SLEEP_JITTER_SECONDS", 60)),  # Increased default
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
        return (
            f"email={self._mask(self.email)} | location={self.location} | "
            f"notify={self._mask(self.notify_email)} | auto_book={self.auto_book} | "
            f"abort_on_captcha={self.abort_on_captcha}"
        )


def send_notification(cfg: CheckerConfig, subject: str, message: str) -> bool:
    if not cfg.is_smtp_configured():
        logging.info("Skipping email notification - SMTP not fully configured.")
        return False

    try:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = cfg.smtp_user
        msg["To"] = cfg.notify_email

        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port) as server:
            server.starttls()
            server.login(cfg.smtp_user, cfg.smtp_pass)
            server.sendmail(cfg.smtp_user, cfg.notify_email, msg.as_string())

        logging.info("Email notification sent successfully")
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logging.error("SMTP authentication failed: %s", exc)
        logging.error(
            "Please verify your Gmail username and app password. "
            "App passwords require 2FA to be enabled."
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to send email notification: %s", exc)

    return False


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
        """Background loop that sends reports at configured intervals."""
        while self.running:
            # Sleep in small increments to allow quick shutdown
            for _ in range(60):  # Check every second for 1 minute
                if not self.running:
                    return
                time.sleep(1)
            
            elapsed = datetime.now() - self.last_report_time
            if elapsed >= timedelta(hours=self.interval_hours):
                try:
                    self._send_progress_report()
                    self.last_report_time = datetime.now()
                except Exception as exc:
                    logging.error("Failed to send progress report: %s", exc)
    
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

    def __init__(self, cfg: CheckerConfig, *, headless: bool = True) -> None:
        self.cfg = cfg
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self.driver_path = ChromeDriverManager().install()
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
        self._burst_mode_active = False
        
        # Initialize strategic components
        self._parse_prime_time_windows()
        if cfg.pattern_learning_enabled:
            self._load_patterns()
        
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
        service = Service(self.driver_path)

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
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        
        # Basic security and compatibility options
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--log-level=3")
        
        # Performance optimizations
        minimal_browser = os.getenv("MINIMAL_BROWSER", "true").lower() == "true"
        if minimal_browser:
            options.add_argument("--disable-images")
            options.add_argument("--disable-plugins")
            options.add_argument("--disable-java")
            options.add_argument("--disable-web-security")
            options.add_argument("--no-proxy-server")
            options.add_argument("--disable-background-timer-throttling")
            options.add_argument("--disable-renderer-backgrounding")
            options.add_argument("--disable-backgrounding-occluded-windows")
            
        # Memory optimizations
        options.add_argument("--memory-pressure-off")
        options.add_argument("--max_old_space_size=4096")
        
        # Disable unnecessary features
        prefs = {
            "profile.default_content_setting_values": {
                "images": 2 if minimal_browser else 0,
                "plugins": 2,
                "popups": 2,
                "geolocation": 2,
                "notifications": 2,
                "media_stream": 2,
            }
        }
        options.add_experimental_option("prefs", prefs)
        
        user_agent = os.getenv(
            "CHECKER_USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        options.add_argument(f"--user-agent={user_agent}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        return options

    # ------------------------------------------------------------------
    # Strategic optimization methods
    # ------------------------------------------------------------------
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
        now = datetime.now()
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
        """Adjust checking frequency based on likelihood of appointments"""
        base_freq = self.cfg.check_frequency_minutes
        
        if self._is_prime_time():
            # More frequent during optimal windows
            return max(1.0, base_freq * 0.5)  # 2x more frequent (half the interval)
        elif 2 <= datetime.now().hour <= 6:
            # Less frequent during low-activity hours
            return base_freq * 2.0  # 2x less frequent (double the interval)
        elif datetime.now().weekday() in [5, 6]:  # Weekend
            return base_freq * self.cfg.weekend_frequency_multiplier
        else:
            return base_freq

    def _should_use_burst_mode(self) -> bool:
        """Enable burst mode during high-probability windows"""
        if not self.cfg.burst_mode_enabled:
            return False
            
        now = datetime.now()
        
        # Business hours start (6-9 AM)
        if 6 <= now.hour <= 9:
            return True
        
        # Lunch hour (12-2 PM) 
        if 12 <= now.hour <= 14:
            return True
            
        # If we haven't seen "busy" in last 30 minutes (possible opening)
        if self._last_busy_check and (datetime.now() - self._last_busy_check) > timedelta(minutes=30):
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
            
        event = {
            'timestamp': datetime.now().isoformat(),
            'hour': datetime.now().hour,
            'day_of_week': datetime.now().weekday(),
            'event': event_type
        }
        self._availability_history.append(event)
        self._save_patterns()

    def _perform_burst_checks(self) -> bool:
        """Perform rapid-fire checks during burst mode"""
        logging.info("🚀 Entering burst mode - rapid checking for 10 minutes")
        self._burst_mode_active = True
        
        try:
            for i in range(20):  # 20 checks x 30 seconds = 10 minutes
                if not self._is_calendar_busy():
                    logging.info("🎉 CALENDAR AVAILABLE! Breaking burst mode after %d attempts", i + 1)
                    self._record_availability_event("available_in_burst")
                    return True
                    
                if i < 19:  # Don't sleep after last check
                    time.sleep(30)
                    
            logging.info("Burst mode completed - no availability found")
            return False
        finally:
            self._burst_mode_active = False

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

    def perform_check(self) -> None:
        start_time = datetime.now()
        
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

            # Now check availability after we're properly navigated
            self._check_consulate_availability()
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
            driver.get("https://ais.usvisa-info.com/en-ca/niv/groups")
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
            
        logging.info("Navigating to login page: %s", LOGIN_URL)
        self._safe_get(LOGIN_URL, detect_captcha=True)
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
        for url in RESCHEDULE_URLS:
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
            return

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
        driver = self.ensure_driver()
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
                
            # Check if date is within user's preferred range
            if start_date <= parsed_date <= end_date:
                dates_in_range.append(parsed_date)
                
                # Check if date is earlier than current appointment
                if parsed_date < current_date:
                    earlier_dates.append(parsed_date)

        if earlier_dates:
            earliest = min(earlier_dates)
            days_earlier = (current_date - earliest).days
            
            logging.info("🎉 EARLIER APPOINTMENT FOUND! %s (%.0f days earlier than current)", 
                        earliest.strftime("%Y-%m-%d"), days_earlier)
            
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
                f"{'🤖 Auto-book is ENABLED - attempting to book...' if self.cfg.auto_book else '⚠️ Login to book manually: https://ais.usvisa-info.com'}"
            )
            send_notification(self.cfg, subject, message)
            
            # If auto-book is enabled, we would implement booking here
            # TODO: Implement auto-booking functionality
            if self.cfg.auto_book:
                logging.warning("Auto-book is enabled but not yet implemented. Please book manually.")
                
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

    def _safe_get(self, url: str, *, attempts: Optional[int] = None, detect_captcha: bool = False) -> None:
        driver = self.ensure_driver()
        total_attempts = attempts or self.cfg.max_retry_attempts
        last_exc: Optional[Exception] = None

        for attempt in range(1, total_attempts + 1):
            try:
                driver.get(url)
                self._wait_for_page_ready(driver)
                if detect_captcha:
                    self._detect_captcha()
                return
            except CaptchaDetectedError as exc:
                last_exc = exc
                logging.warning("Captcha encountered while loading %s: %s", url, exc)
                break
            except (WebDriverException, TimeoutException) as exc:
                last_exc = exc
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

        if not isinstance(last_exc, CaptchaDetectedError):
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
            self._capture_artifact("captcha_detected")
            self._schedule_backoff()
            message = (
                "Captcha detected - manual solve required"
                if self.cfg.abort_on_captcha
                else "Captcha detected - retry will be attempted after backoff"
            )
            raise CaptchaDetectedError(message)

        return False

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

        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
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
        # Strategic optimization: Use optimal frequency calculation
        optimal_minutes = self._calculate_optimal_frequency()
        
        # Use the more conservative of base_minutes and optimal calculation
        if optimal_minutes < base_minutes:
            adjusted_minutes = optimal_minutes
            logging.debug("Using optimized frequency: %.1f minutes (prime time: %s)", 
                        optimal_minutes, self._is_prime_time())
        else:
            adjusted_minutes = self._calculate_dynamic_backoff()
        
        base_seconds = max(1, adjusted_minutes) * 60
        
        jitter = 0
        if self.cfg.sleep_jitter_seconds:
            jitter = random.randint(-self.cfg.sleep_jitter_seconds, self.cfg.sleep_jitter_seconds)

        # Reduce minimum sleep time during prime hours
        min_sleep = 30
        if self._is_prime_time():
            min_sleep = 15  # Faster response during prime time
        
        sleep_seconds = max(min_sleep, base_seconds + jitter)

        if self._backoff_until:
            now = datetime.now()
            if now < self._backoff_until:
                backoff_seconds = int((self._backoff_until - now).total_seconds())
                sleep_seconds = max(sleep_seconds, backoff_seconds)
                logging.debug("Applying scheduled backoff: %s seconds remaining", backoff_seconds)
            else:
                logging.debug("Backoff period expired, resuming normal schedule")
                self._backoff_until = None

        return int(sleep_seconds)

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

        if success:
            self._backoff_until = None

        # Periodic cleanup every 10 checks
        if self._checks_since_restart % 10 == 0:
            self._cleanup_artifacts()

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

        self._capture_artifact(f"error_{type(exc).__name__.lower()}")

        # If this is a rate limiting or login issue, schedule a longer backoff
        error_message = str(exc).lower()
        if any(keyword in error_message for keyword in ["rate limiting", "sign in or sign up", "login blocked"]):
            # Schedule a longer backoff for login issues
            backoff_minutes = max(15, self.cfg.check_frequency_minutes * 3)
            self._backoff_until = datetime.now() + timedelta(minutes=backoff_minutes)
            logging.warning("Login rate limiting detected, scheduling %s minute backoff", backoff_minutes)

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
    try:
        cfg = CheckerConfig.load()
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logging.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    parser = argparse.ArgumentParser(description="US Visa Appointment Checker")
    parser.add_argument(
        "--frequency",
        type=int,
        default=cfg.check_frequency_minutes,
        help="Check frequency in minutes (default from config.ini)",
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
    parser.set_defaults(headless=True)
    args = parser.parse_args()

    frequency = max(1, args.frequency)
    headless = args.headless
    report_interval = max(0, args.report_interval)

    print("🚀 US Visa Appointment Checker Started - OPTIMIZED")
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
    print(f"📧 Notifications: {'Enabled' if cfg.is_smtp_configured() else 'Disabled (configure SMTP)'}")
    print(f"🤖 Auto-book: {'Enabled' if cfg.auto_book else 'Disabled'}")
    print(f"🕶️ Headless mode: {'On' if headless else 'Off'}")
    print("=" * 55)

    logging.info("Configuration summary: %s", cfg.masked_summary())
    if cfg.heartbeat_path:
        logging.info("Heartbeat file: %s", cfg.heartbeat_path)

    checker = VisaAppointmentChecker(cfg, headless=headless)
    
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
            print(f"\n🔄 Starting check #{check_count} at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 30)

            success = False
            captcha_detected = False
            try:
                checker.perform_check()
                print(f"✅ Check #{check_count} completed successfully")
                success = True
            except CaptchaDetectedError as exc:
                print(f"🤖 Check #{check_count} blocked by captcha: {exc}")
                captcha_detected = True
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Check #{check_count} failed: {exc}")

            # Record stats for progress reporter
            if reporter:
                reporter.record_check(success=success, captcha=captcha_detected)

            checker.post_check(success=success)

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