from __future__ import annotations

import os
import tempfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
PYTEST_TMP_DIR = ROOT_DIR / ".pytest_tmp"
PYTEST_CACHE_DIR = ROOT_DIR / ".pytest_cache" / "v" / "cache"


def _prepare_pytest_dirs() -> None:
    global PYTEST_TMP_DIR, PYTEST_CACHE_DIR

    try:
        PYTEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
        PYTEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback_root = Path(tempfile.gettempdir()) / "us_visa_checker_pytest"
        PYTEST_TMP_DIR = fallback_root / ".pytest_tmp"
        PYTEST_CACHE_DIR = fallback_root / ".pytest_cache" / "v" / "cache"
        PYTEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
        PYTEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    for env_name in ("TMPDIR", "TEMP", "TMP"):
        os.environ.setdefault(env_name, str(PYTEST_TMP_DIR))


_prepare_pytest_dirs()
