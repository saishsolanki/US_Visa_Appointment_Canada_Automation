import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("logs")
LOG_PATH = LOG_DIR / "visa_checker.log"

ARTIFACTS_DIR = Path("artifacts")
FALLBACK_BASE_DIR = Path.home() / ".local" / "state" / "visa_checker"

NOISY_LOGGERS = [
    "selenium",
    "selenium.webdriver.remote.remote_connection",
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "PIL",
    "chardet",
]


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, debug: bool = False, json_logs: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    stream_handler = logging.StreamHandler()
    file_handler: logging.Handler
    log_path = LOG_PATH

    try:
        LOG_DIR.mkdir(exist_ok=True)
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5)
    except OSError:
        fallback_log_dir = FALLBACK_BASE_DIR / "logs"
        fallback_artifacts_dir = FALLBACK_BASE_DIR / "artifacts"
        fallback_log_dir.mkdir(parents=True, exist_ok=True)
        fallback_artifacts_dir.mkdir(parents=True, exist_ok=True)
        globals()["LOG_DIR"] = fallback_log_dir
        globals()["LOG_PATH"] = fallback_log_dir / "visa_checker.log"
        globals()["ARTIFACTS_DIR"] = fallback_artifacts_dir
        log_path = globals()["LOG_PATH"]
        file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5)

    handlers = [file_handler, stream_handler]

    if json_logs:
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=handlers, force=True)

    for noisy_logger in NOISY_LOGGERS:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
