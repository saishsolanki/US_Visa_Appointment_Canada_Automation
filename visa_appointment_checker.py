import argparse
import configparser
import json
import logging
import os
import random
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from email.mime.text import MIMEText

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

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "visa_checker.log"

ARTIFACTS_DIR = Path("artifacts")
ARTIFACTS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(),
    ],
)

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
    ]

    CONSULATE_DATE_INPUT_SELECTORS: List[Selector] = [
        (By.ID, "appointments_consulate_appointment_date"),
        (By.CSS_SELECTOR, "input[id*='consulate_appointment_date']"),
    ]

    CONSULATE_TIME_SELECTORS: List[Selector] = [
        (By.ID, "appointments_consulate_appointment_time"),
        (By.CSS_SELECTOR, "select[id*='consulate_appointment_time']"),
    ]

    DATEPICKER_CONTAINER_SELECTORS: List[Selector] = [
        (By.ID, "ui-datepicker-div"),
        (By.CSS_SELECTOR, "#ui-datepicker-div"),
    ]

    DATEPICKER_AVAILABLE_DAY_SELECTORS: List[Selector] = [
        (By.CSS_SELECTOR, "#ui-datepicker-div td:not(.ui-state-disabled) a"),
    ]

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
        self._session_cookies: Optional[dict] = None
        self._last_session_validation: Optional[datetime] = None
        self._recent_results = []  # Track last 10 check results
        self._metrics = {
            'check_durations': [],
            'navigation_times': [],
            'success_rate': 0.0,
            'avg_response_time': 0.0
        }
        
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
                    break
            except TimeoutException:
                logging.warning("Timeout while loading %s; trying next", url)
            except WebDriverException as exc:
                logging.warning("Browser navigation error for %s: %s", url, exc)
        else:
            raise RuntimeError("Failed to reach scheduling page")

        # Cache form elements when we reach the appointment page for future use
        self._cache_form_elements()
        
        location_select = self._find_element(self.LOCATION_SELECTORS, wait_time=20, use_cache=True)
        if location_select:
            self._ensure_location_selected(location_select)
        else:
            logging.info(
                "Location selector not found; page layout may have changed or location already locked."
            )
            self._capture_artifact("missing_location_selector")

        self._check_consulate_availability()

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
        if "/appointment" in current and "reschedule" not in current:
            # Already on appointment page or similar.
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
                time.sleep(0.5)
            else:
                logging.debug("Reschedule accordion toggle not found; attempting to locate button directly")

        if self._ensure_on_appointment_form():
            return

        if self._appointment_base_url:
            appointment_url = urljoin(self._appointment_base_url, "appointment")
            if not driver.current_url.startswith(appointment_url):
                logging.info("Loading appointment page directly via stored URL: %s", appointment_url)
                self._safe_get(appointment_url)
                self._dismiss_overlays()
            if self._ensure_on_appointment_form():
                return

        reschedule_button = self._find_element(self.RESCHEDULE_BUTTON_SELECTORS, wait_time=20, clickable=False)
        if reschedule_button:
            href = ""
            try:
                href = reschedule_button.get_attribute("href") or ""
            except (WebDriverException, StaleElementReferenceException):
                logging.debug("Could not retrieve href from reschedule button")
            
            if href:
                logging.info("Navigating directly to reschedule link: %s", href)
                self._safe_get(href)
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
                self._wait_for_page_ready(driver)
                self._dismiss_overlays()
                if self._ensure_on_appointment_form():
                    return

        logging.warning(
            "Unable to open reschedule appointment workflow automatically; remaining on %s",
            driver.current_url,
        )
        self._capture_artifact("reschedule_navigation_failed")

    def _ensure_on_appointment_form(self) -> bool:
        driver = self.ensure_driver()
        form = self._find_element(self.APPOINTMENT_FORM_SELECTORS, wait_time=12)
        if not form:
            return False

        logging.info("Appointment form detected at %s", driver.current_url)
        return True

    def _ensure_location_selected(self, element) -> None:
        select = Select(element)
        selected_text = select.first_selected_option.text.strip() if select.options else ""

        normalized_target = self.cfg.location.strip().lower()
        normalized_selected = selected_text.lower()

        if normalized_target == normalized_selected:
            logging.info("Target consular location already selected: %s", selected_text)
            return

        try:
            select.select_by_visible_text(self.cfg.location)
            logging.info("Selected consular location by exact match: %s", self.cfg.location)
            return
        except NoSuchElementException:
            pass

        for option in select.options:
            option_text = option.text.strip()
            if not option_text:
                continue
            if normalized_target in option_text.lower() or option_text.lower() in normalized_target:
                select.select_by_visible_text(option_text)
                logging.info("Selected consular location using fuzzy match: %s", option_text)
                return

        logging.warning(
            "Unable to match configured location '%s' to the available dropdown options (currently '%s').",
            self.cfg.location,
            selected_text,
        )

    def _is_calendar_busy(self) -> bool:
        """Check if calendar shows busy status"""
        busy_element = self._is_selector_visible(self.CONSULATE_BUSY_SELECTORS)
        return busy_element is not None

    def _check_consulate_availability(self) -> None:
        driver = self.ensure_driver()
        check_start = datetime.now()

        try:
            WebDriverWait(driver, 20).until(
                lambda d: (
                    self._is_selector_visible(self.CONSULATE_BUSY_SELECTORS)
                    or self._is_selector_visible(self.CONSULATE_DATE_INPUT_SELECTORS)
                )
            )
        except TimeoutException:
            logging.warning("Consular appointment widgets did not load within the expected time window")
            return

        # Intelligent calendar polling with adaptive frequency
        if self._is_calendar_busy():
            self._busy_streak_count += 1
            message = "System is busy. Please try again later."
            
            if busy_element := self._is_selector_visible(self.CONSULATE_BUSY_SELECTORS):
                message = busy_element.text.strip() or message
                
            logging.info("Consular calendar message: %s", message)
            
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

        try:
            date_input.click()
        except (WebDriverException, ElementNotInteractableException):
            driver.execute_script("arguments[0].click();", date_input)

        time.sleep(0.5)  # Reduced sleep time

        available_slots = self._collect_available_dates(max_months=3)
        if available_slots:
            logging.info("Discovered available appointment dates: %s", ", ".join(available_slots))
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
            if not calendar.is_displayed():
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
        """Ensure the privacy policy confirmation checkbox is checked before submitting."""
        driver = self.ensure_driver()

        checkbox = self._find_element(self.PRIVACY_CHECKBOX_SELECTORS, wait_time=5)
        if checkbox is None:
            label = self._find_element(self.PRIVACY_LABEL_SELECTORS, wait_time=3)
            if label is None:
                logging.debug("Privacy policy checkbox not found; continuing without explicit confirmation.")
                return

            self._scroll_into_view(label)
            try:
                label.click()
                logging.info("Accepted privacy policy via label click")
            except WebDriverException:
                logging.debug("Label click failed; attempting scripted click")
                driver.execute_script("arguments[0].click();", label)
                logging.info("Accepted privacy policy via scripted label click")
            return

        if checkbox.is_selected():
            logging.debug("Privacy policy checkbox already selected")
            return

        self._scroll_into_view(checkbox)
        try:
            checkbox.click()
            logging.info("Privacy policy checkbox selected")
            return
        except WebDriverException:
            logging.debug("Direct checkbox click failed; attempting scripted click")

        driver.execute_script("arguments[0].click();", checkbox)
        logging.info("Privacy policy checkbox selected via scripted click")

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
        
        # Factor in busy streak for more intelligent backoff
        busy_multiplier = 1.0 + (self._busy_streak_count * 0.2)  # Increase by 20% per busy streak
        
        # Set backoff range based on frequency: 
        # - For frequent checks (< 5 min): use longer backoff to avoid overloading
        # - For moderate checks (5-30 min): use moderate backoff 
        # - For infrequent checks (> 30 min): use shorter backoff
        if user_frequency < 5:
            min_backoff = max(10, user_frequency * 3 * busy_multiplier)
            max_backoff = max(20, user_frequency * 5 * busy_multiplier)
        elif user_frequency <= 30:
            min_backoff = max(user_frequency, 8 * busy_multiplier)
            max_backoff = max(user_frequency * 2, 15 * busy_multiplier)
        else:
            min_backoff = max(5, user_frequency // 2 * busy_multiplier)
            max_backoff = max(10, user_frequency * busy_multiplier)
        
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
        except Exception as exc:  # noqa: BLE001
            logging.debug("Failed to persist page source artifact: %s", exc)

        try:
            driver.save_screenshot(str(base.with_suffix(".png")))
        except Exception as exc:  # noqa: BLE001
            logging.debug("Failed to capture screenshot artifact: %s", exc)

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
        # Use dynamic calculation
        adjusted_minutes = self._calculate_dynamic_backoff()
        base_seconds = max(1, adjusted_minutes) * 60
        
        jitter = 0
        if self.cfg.sleep_jitter_seconds:
            jitter = random.randint(-self.cfg.sleep_jitter_seconds, self.cfg.sleep_jitter_seconds)

        sleep_seconds = max(30, base_seconds + jitter)

        if self._backoff_until:
            now = datetime.now()
            if now < self._backoff_until:
                backoff_seconds = int((self._backoff_until - now).total_seconds())
                sleep_seconds = max(sleep_seconds, backoff_seconds)
                logging.debug("Applying scheduled backoff: %s seconds remaining", backoff_seconds)
            else:
                logging.debug("Backoff period expired, resuming normal schedule")
                self._backoff_until = None

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
    parser.set_defaults(headless=True)
    args = parser.parse_args()

    frequency = max(1, args.frequency)
    headless = args.headless

    print("🚀 US Visa Appointment Checker Started")
    print("=" * 50)
    print(f"📅 Current appointment date: {cfg.current_appointment_date}")
    print(f"📍 Location: {cfg.location}")
    print(f"⏱️  Check frequency: {frequency} minutes")
    print(f"📧 Notifications: {'Enabled' if cfg.is_smtp_configured() else 'Disabled (configure SMTP)'}")
    print(f"🤖 Auto-book: {'Enabled' if cfg.auto_book else 'Disabled'}")
    print(f"🕶️ Headless mode: {'On' if headless else 'Off'}")
    print("=" * 50)

    logging.info("Configuration summary: %s", cfg.masked_summary())
    if cfg.heartbeat_path:
        logging.info("Heartbeat file: %s", cfg.heartbeat_path)

    checker = VisaAppointmentChecker(cfg, headless=headless)

    check_count = 0
    try:
        while True:
            check_count += 1
            start_time = datetime.now()
            print(f"\n🔄 Starting check #{check_count} at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 30)

            success = False
            try:
                checker.perform_check()
                print(f"✅ Check #{check_count} completed successfully")
                success = True
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Check #{check_count} failed: {exc}")

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
        checker.quit_driver()
        print("🧹 Browser session closed")


if __name__ == "__main__":
    main()