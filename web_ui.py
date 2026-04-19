from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    flash,
    jsonify,
    Response,
    stream_with_context,
    session,
    send_from_directory,
    abort,
)
import argparse
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from config_manager import BOOLEAN_KEYS, CONFIG_KEYS, ConfigManager

app = Flask(__name__)
app.secret_key = os.urandom(32)

_ACCESS_TOKEN = (os.getenv("WEB_UI_TOKEN") or "").strip()

# Path to the log file produced by logging_utils.py
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_BASE_DIR, "logs", "visa_checker.log")
_ARTIFACTS_PATH = os.path.join(_BASE_DIR, "artifacts")

# In-memory ring-buffer for update output (shared between threads)
_update_output: list[str] = []
_update_lock = threading.Lock()
_update_running = False

CONFIG_MANAGER = ConfigManager()

# Boolean keys that are actually represented as checkboxes on the current form.
# Any boolean key not listed here should preserve its existing value on POST.
_FORM_BOOLEAN_KEYS = {
    "AUTO_BOOK",
    "BURST_MODE_ENABLED",
    "MULTI_LOCATION_CHECK",
    "PATTERN_LEARNING_ENABLED",
    "AUTO_BOOK_DRY_RUN",
    "TEST_MODE",
    "SAFETY_FIRST_MODE",
    "AUDIO_ALERTS_ENABLED",
    "ACCOUNT_ROTATION_ENABLED",
}

_INT_KEYS = {
    "CHECK_FREQUENCY_MINUTES",
    "SMTP_PORT",
    "DRIVER_RESTART_CHECKS",
    "MAX_RETRY_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "SLEEP_JITTER_SECONDS",
    "BUSY_BACKOFF_MIN_MINUTES",
    "BUSY_BACKOFF_MAX_MINUTES",
    "MIN_IMPROVEMENT_DAYS",
    "AUTO_BOOK_CONFIRMATION_WAIT_SECONDS",
    "MAX_REQUESTS_PER_HOUR",
    "MAX_API_REQUESTS_PER_HOUR",
    "MAX_UI_NAVIGATIONS_PER_HOUR",
    "SLOT_TTL_HOURS",
    "SAFETY_FIRST_MIN_INTERVAL_MINUTES",
    "ROTATION_INTERVAL_CHECKS",
    "VPN_MIN_SESSION_MINUTES",
}

_FLOAT_KEYS = {
    "PRIME_TIME_BACKOFF_MULTIPLIER",
    "WEEKEND_FREQUENCY_MULTIPLIER",
}

_SERVICE_NAME = (os.getenv("CHECKER_SERVICE_NAME") or "visa-checker.service").strip() or "visa-checker.service"

_WARNING_SEVERITY_ORDER = ("rate-limit", "captcha", "network", "config", "other")
_WARNING_SEVERITY_KEYWORDS = {
    "rate-limit": (
        "rate limit",
        "throttle",
        "forbidden",
        "429",
        "busy",
        "backoff",
        "too many requests",
        "scheduling limit",
    ),
    "captcha": (
        "captcha",
        "hcaptcha",
        "challenge",
        "verify you are human",
        "recaptcha",
    ),
    "network": (
        "network",
        "connection",
        "timeout",
        "timed out",
        "dns",
        "proxy",
        "vpn",
        "unreachable",
        "connection reset",
    ),
    "config": (
        "config",
        "configuration",
        "missing",
        "invalid",
        "parse",
        "selector",
        "schedule id",
        "facility id",
        "country code",
    ),
}

_STRATEGY_PRESETS: dict[str, dict[str, object]] = {
    "balanced": {
        "label": "Balanced",
        "description": "Default mixed API/UI cadence.",
        "updates": {
            "CHECK_FREQUENCY_MINUTES": "3",
            "BURST_MODE_ENABLED": "True",
            "MULTI_LOCATION_CHECK": "True",
            "MAX_API_REQUESTS_PER_HOUR": "140",
            "MAX_UI_NAVIGATIONS_PER_HOUR": "60",
        },
    },
    "api-recovery": {
        "label": "API Recovery",
        "description": "Use API-heavy mode during warning-gate cooldowns.",
        "updates": {
            "CHECK_FREQUENCY_MINUTES": "4",
            "BURST_MODE_ENABLED": "False",
            "MULTI_LOCATION_CHECK": "True",
            "MAX_API_REQUESTS_PER_HOUR": "180",
            "MAX_UI_NAVIGATIONS_PER_HOUR": "25",
        },
    },
    "aggressive-hunt": {
        "label": "Aggressive Hunt",
        "description": "Higher cadence with wider UI probing.",
        "updates": {
            "CHECK_FREQUENCY_MINUTES": "2",
            "BURST_MODE_ENABLED": "True",
            "MULTI_LOCATION_CHECK": "True",
            "MAX_API_REQUESTS_PER_HOUR": "220",
            "MAX_UI_NAVIGATIONS_PER_HOUR": "90",
        },
    },
}

_INTERVENTION_ACTIONS: dict[str, dict[str, object]] = {
    "recover-gate": {
        "preset": "api-recovery",
        "restart": True,
        "label": "Gate recovery",
        "description": "Switch to API recovery strategy and restart checker.",
    },
    "resume-balanced": {
        "preset": "balanced",
        "restart": True,
        "label": "Resume balanced",
        "description": "Return to balanced strategy and restart checker.",
    },
    "push-aggressive": {
        "preset": "aggressive-hunt",
        "restart": True,
        "label": "Push aggressive",
        "description": "Enable aggressive strategy and restart checker.",
    },
}


def _parse_bool_flag(raw: str, default: bool = True) -> bool:
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_heartbeat_path() -> str:
    env_path = (os.getenv("HEARTBEAT_PATH") or "").strip()
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))

    try:
        parser = CONFIG_MANAGER.load_parser()
        config_path = CONFIG_MANAGER.get_case_insensitive(parser, "HEARTBEAT_PATH", "")
        if config_path.strip():
            return os.path.abspath(os.path.expanduser(config_path.strip()))
    except Exception:  # noqa: BLE001
        pass

    fallback = os.path.join(_BASE_DIR, "logs", "runtime_heartbeat.json")
    if os.path.exists(fallback):
        return fallback
    return ""


def _load_heartbeat_payload() -> dict[str, object]:
    gate_status: dict[str, object] = {
        "warning_gate_streak": 0,
        "warning_seen_this_cycle": False,
        "breaker_active": False,
        "breaker_remaining_seconds": 0,
        "breaker_until": None,
        "breaker_last_trip_at": None,
        "ui_skipped_due_breaker": False,
        "last_real_slot_eval_at": None,
        "api_checks": 0,
        "ui_checks": 0,
        "api_vs_ui_ratio": None,
        "warning_page_hits": 0,
        "continue_success_count": 0,
    }

    payload: dict[str, object] = {
        "available": False,
        "path": "",
        "timestamp": "",
        "status": "",
        "gate_status": gate_status,
        "facility_priority": [],
    }

    heartbeat_path = _resolve_heartbeat_path()
    payload["path"] = heartbeat_path
    if not heartbeat_path or not os.path.exists(heartbeat_path):
        return payload

    try:
        with open(heartbeat_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:  # noqa: BLE001
        return payload

    raw_gate = raw.get("gate_status") if isinstance(raw, dict) else {}
    if isinstance(raw_gate, dict):
        gate_status.update(raw_gate)

    payload.update(
        {
            "available": True,
            "timestamp": str(raw.get("timestamp", "") or ""),
            "status": str(raw.get("status", "") or ""),
            "gate_status": gate_status,
            "facility_priority": raw.get("facility_priority", []) if isinstance(raw, dict) else [],
        }
    )
    return payload


def _strategy_snapshot() -> dict[str, object]:
    current = CONFIG_MANAGER.ui_values()
    presets: list[dict[str, object]] = []
    active_preset = ""

    for name, spec in _STRATEGY_PRESETS.items():
        updates = spec.get("updates", {})
        assert isinstance(updates, dict)
        matches = all(str(current.get(key, "")).strip() == str(value).strip() for key, value in updates.items())
        if matches and not active_preset:
            active_preset = name

        presets.append(
            {
                "name": name,
                "label": str(spec.get("label", name) or name),
                "description": str(spec.get("description", "") or ""),
                "updates": updates,
                "active": matches,
            }
        )

    return {
        "active_preset": active_preset,
        "presets": presets,
        "current": {
            "CHECK_FREQUENCY_MINUTES": current.get("CHECK_FREQUENCY_MINUTES", ""),
            "BURST_MODE_ENABLED": current.get("BURST_MODE_ENABLED", ""),
            "MULTI_LOCATION_CHECK": current.get("MULTI_LOCATION_CHECK", ""),
            "MAX_API_REQUESTS_PER_HOUR": current.get("MAX_API_REQUESTS_PER_HOUR", ""),
            "MAX_UI_NAVIGATIONS_PER_HOUR": current.get("MAX_UI_NAVIGATIONS_PER_HOUR", ""),
        },
    }


def _apply_strategy_preset(preset_name: str, *, restart_service: bool) -> tuple[bool, str, dict[str, str]]:
    preset_key = preset_name.strip().lower()
    preset = _STRATEGY_PRESETS.get(preset_key)
    if preset is None:
        return False, f"Unknown strategy preset: {preset_name}", {}

    updates = {
        str(key): str(value)
        for key, value in dict(preset.get("updates", {})).items()
    }
    try:
        CONFIG_MANAGER.save_updates(updates)
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to save strategy preset: {exc}", updates

    if restart_service:
        ok, message = _service_action("restart")
        if ok:
            return True, f"Applied preset '{preset_key}' and restarted service", updates
        return False, f"Applied preset but restart failed: {message}", updates

    return True, f"Applied preset '{preset_key}'", updates


def _opportunity_heatmap(history: list[dict[str, str]]) -> dict[str, object]:
    if not history:
        return {
            "hours": [],
            "locations": [],
            "matrix": [],
            "max_count": 0,
            "sample_count": 0,
        }

    hour_labels = [f"{hour:02d}:00" for hour in range(24)]
    location_map: dict[str, list[int]] = {}
    sample_count = 0

    for row in history:
        discovered_raw = str(row.get("discovered", "") or "").strip()
        location = str(row.get("location", "") or "Unknown").strip() or "Unknown"
        if not discovered_raw:
            continue

        normalized = discovered_raw.replace("Z", "+00:00")
        try:
            discovered = datetime.fromisoformat(normalized)
        except ValueError:
            continue

        bucket = int(discovered.hour)
        bins = location_map.setdefault(location, [0 for _ in range(24)])
        bins[bucket] += 1
        sample_count += 1

    if not location_map:
        return {
            "hours": hour_labels,
            "locations": [],
            "matrix": [],
            "max_count": 0,
            "sample_count": sample_count,
        }

    sorted_locations = sorted(
        location_map.keys(),
        key=lambda loc: sum(location_map[loc]),
        reverse=True,
    )
    matrix = [location_map[loc] for loc in sorted_locations]
    max_count = max((max(row) for row in matrix), default=0)

    return {
        "hours": hour_labels,
        "locations": sorted_locations,
        "matrix": matrix,
        "max_count": max_count,
        "sample_count": sample_count,
    }


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _format_bytes(raw_bytes: int) -> str:
    if raw_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(raw_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{raw_bytes} B"


def _compact_error(text: str, *, max_lines: int = 4) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "Unknown error"
    if len(lines) <= max_lines:
        return " | ".join(lines)
    return " | ".join(lines[:max_lines]) + " | ..."


def _run_first_success(
    commands: list[list[str]],
    *,
    timeout: int = 8,
    cwd: Optional[str] = None,
) -> tuple[bool, Optional[subprocess.CompletedProcess[str]], str]:
    last_error = ""
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue

        if result.returncode == 0:
            return True, result, ""

        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if output:
            last_error = output
        else:
            last_error = f"Command exited with code {result.returncode}"

    if not last_error:
        last_error = "Command is unavailable on this host"
    return False, None, last_error


def _read_last_log_line() -> str:
    try:
        if os.path.exists(_LOG_PATH):
            with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as handle:
                lines = handle.readlines()
            if lines:
                return lines[-1].rstrip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _normalize_warning_text(raw: str) -> str:
    text = (raw or "").lower()
    return " ".join(token for token in re.split(r"[^a-z0-9]+", text) if token)


def _warning_label_from_filename(filename: str) -> str:
    stem, _ = os.path.splitext(filename)
    marker = "_warning_"
    lowered = stem.lower()
    marker_index = lowered.find(marker)
    if marker_index >= 0:
        label = stem[marker_index + len(marker):]
    else:
        label = stem

    label = re.sub(r"[_\-]+", " ", label).strip()
    return label or filename


def _classify_warning_severity(text: str) -> str:
    normalized = _normalize_warning_text(text)
    if not normalized:
        return "other"

    for severity in _WARNING_SEVERITY_ORDER:
        if severity == "other":
            continue
        for phrase in _WARNING_SEVERITY_KEYWORDS.get(severity, ()):  # pragma: no branch - small static map
            if _normalize_warning_text(phrase) in normalized:
                return severity
    return "other"


def _warning_screenshot_inventory() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []

    if not os.path.isdir(_ARTIFACTS_PATH):
        return items

    try:
        for entry in os.scandir(_ARTIFACTS_PATH):
            if not entry.is_file():
                continue

            name = entry.name
            lowered = name.lower()
            if not lowered.endswith(".png"):
                continue
            if "_warning_" not in lowered:
                continue

            stat = entry.stat()
            created_unix = int(stat.st_mtime)
            created = datetime.fromtimestamp(created_unix, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            label = _warning_label_from_filename(name)
            severity = _classify_warning_severity(f"{name} {label}")
            items.append(
                {
                    "name": name,
                    "url": f"/artifacts/warnings/{name}",
                    "created_unix": created_unix,
                    "created": created,
                    "size_bytes": stat.st_size,
                    "size_human": _format_bytes(stat.st_size),
                    "severity": severity,
                    "label": label,
                }
            )
    except OSError:
        return []

    items.sort(key=lambda item: int(item["created_unix"]), reverse=True)
    return items


def _warning_gallery_trends(items: list[dict[str, object]]) -> dict[str, dict[str, list[object]]]:
    now = datetime.now(timezone.utc)

    hour_anchor = now.replace(minute=0, second=0, microsecond=0)
    hour_buckets = [hour_anchor - timedelta(hours=offset) for offset in range(23, -1, -1)]
    hour_labels = [bucket.strftime("%H:%M") for bucket in hour_buckets]
    hour_values = [0 for _ in hour_buckets]
    hour_index = {int(bucket.timestamp()): idx for idx, bucket in enumerate(hour_buckets)}

    day_anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_buckets = [day_anchor - timedelta(days=offset) for offset in range(6, -1, -1)]
    day_labels = [bucket.strftime("%m-%d") for bucket in day_buckets]
    day_values = [0 for _ in day_buckets]
    day_index = {bucket.date().isoformat(): idx for idx, bucket in enumerate(day_buckets)}

    for item in items:
        created_unix = int(item.get("created_unix", 0) or 0)
        if created_unix <= 0:
            continue

        created_dt = datetime.fromtimestamp(created_unix, timezone.utc)
        hour_key = int(created_dt.replace(minute=0, second=0, microsecond=0).timestamp())
        if hour_key in hour_index:
            hour_values[hour_index[hour_key]] += 1

        day_key = created_dt.date().isoformat()
        if day_key in day_index:
            day_values[day_index[day_key]] += 1

    return {
        "hourly": {
            "labels": hour_labels,
            "values": hour_values,
        },
        "daily": {
            "labels": day_labels,
            "values": day_values,
        },
    }


def _warning_gallery_stats(items: list[dict[str, object]]) -> dict[str, object]:
    total_size = sum(int(item.get("size_bytes", 0) or 0) for item in items)
    severity_counts = {key: 0 for key in _WARNING_SEVERITY_ORDER}

    for item in items:
        severity = str(item.get("severity", "other") or "other")
        if severity not in severity_counts:
            severity = "other"
        severity_counts[severity] += 1

    newest = None
    oldest = None
    if items:
        newest = {
            "name": str(items[0].get("name", "") or ""),
            "created": str(items[0].get("created", "") or ""),
            "created_unix": int(items[0].get("created_unix", 0) or 0),
        }
        oldest_item = items[-1]
        oldest = {
            "name": str(oldest_item.get("name", "") or ""),
            "created": str(oldest_item.get("created", "") or ""),
            "created_unix": int(oldest_item.get("created_unix", 0) or 0),
        }

    return {
        "artifact_count": len(items),
        "total_size_bytes": total_size,
        "total_size_human": _format_bytes(total_size),
        "oldest": oldest,
        "newest": newest,
        "severity_counts": severity_counts,
        "trends": _warning_gallery_trends(items),
    }


def _warning_screenshot_payload(limit: int = 40, severity: str = "") -> dict[str, object]:
    max_items = max(1, min(limit, 200))
    normalized_severity = (severity or "").strip().lower()

    all_items = _warning_screenshot_inventory()
    stats = _warning_gallery_stats(all_items)

    if normalized_severity in _WARNING_SEVERITY_ORDER and normalized_severity != "other":
        filtered_items = [item for item in all_items if item.get("severity") == normalized_severity]
    elif normalized_severity == "other":
        filtered_items = [item for item in all_items if item.get("severity") == "other"]
    else:
        filtered_items = all_items

    items = filtered_items[:max_items]
    return {
        "count": len(items),
        "items": items,
        "severity_filter": normalized_severity if normalized_severity in _WARNING_SEVERITY_ORDER else "",
        "retention": {
            "artifact_count": int(stats.get("artifact_count", 0) or 0),
            "total_size_bytes": int(stats.get("total_size_bytes", 0) or 0),
            "total_size_human": str(stats.get("total_size_human", "0 B") or "0 B"),
            "oldest": stats.get("oldest"),
            "newest": stats.get("newest"),
        },
        "severity_counts": stats.get("severity_counts", {}),
        "trends": stats.get("trends", {}),
    }


def _parse_key_value_output(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in (raw or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _load_process_snapshot(pid: int) -> dict[str, str]:
    snapshot = {
        "pid": str(pid),
        "cpu_percent": "",
        "mem_percent": "",
        "elapsed": "",
        "command": "",
        "error": "",
    }
    if pid <= 0:
        return snapshot

    ok, result, err = _run_first_success(
        [["ps", "-p", str(pid), "-o", "%cpu=,%mem=,etime=,cmd="]],
        timeout=4,
    )
    if not ok or not result:
        snapshot["error"] = _compact_error(err)
        return snapshot

    line = (result.stdout or "").strip()
    if not line:
        snapshot["error"] = "Process information unavailable"
        return snapshot

    parts = line.split(None, 3)
    if len(parts) >= 4:
        snapshot["cpu_percent"] = parts[0]
        snapshot["mem_percent"] = parts[1]
        snapshot["elapsed"] = parts[2]
        snapshot["command"] = parts[3]
    else:
        snapshot["command"] = line
    return snapshot


def _service_status_snapshot() -> dict[str, object]:
    status: dict[str, object] = {
        "name": _SERVICE_NAME,
        "control_supported": False,
        "active_state": "unknown",
        "sub_state": "unknown",
        "main_pid": 0,
        "restarts": 0,
        "exec_status": "",
        "started_at": "",
        "memory_bytes": 0,
        "memory_human": "0 B",
        "cpu_seconds": 0.0,
        "tasks_current": 0,
        "process": {},
        "last_error": "",
    }

    if shutil.which("systemctl") is None:
        status["last_error"] = "systemctl is not available on this host"
        return status

    status["control_supported"] = True
    props = (
        "ActiveState,SubState,MainPID,NRestarts,ExecMainStatus,"
        "ExecMainStartTimestamp,MemoryCurrent,CPUUsageNSec,TasksCurrent"
    )
    show_commands = [
        ["systemctl", "show", _SERVICE_NAME, f"--property={props}"],
        ["sudo", "-n", "systemctl", "show", _SERVICE_NAME, f"--property={props}"],
    ]
    ok, result, err = _run_first_success(show_commands, timeout=6)
    if not ok or not result:
        status["last_error"] = _compact_error(err)

        active_ok, active_result, active_err = _run_first_success(
            [
                ["systemctl", "is-active", _SERVICE_NAME],
                ["sudo", "-n", "systemctl", "is-active", _SERVICE_NAME],
            ],
            timeout=4,
        )
        if active_ok and active_result:
            status["active_state"] = (active_result.stdout or "").strip() or "unknown"
            status["sub_state"] = status["active_state"]
        elif active_err:
            status["last_error"] = _compact_error(active_err)
        return status

    fields = _parse_key_value_output(result.stdout or "")
    active_state = fields.get("ActiveState", "unknown")
    sub_state = fields.get("SubState", "unknown")
    main_pid = _safe_int(fields.get("MainPID", "0"), 0)
    restarts = _safe_int(fields.get("NRestarts", "0"), 0)
    cpu_nsec = _safe_int(fields.get("CPUUsageNSec", "0"), 0)
    memory_bytes = _safe_int(fields.get("MemoryCurrent", "0"), 0)
    tasks_current = _safe_int(fields.get("TasksCurrent", "0"), 0)

    status.update(
        {
            "active_state": active_state,
            "sub_state": sub_state,
            "main_pid": main_pid,
            "restarts": restarts,
            "exec_status": fields.get("ExecMainStatus", ""),
            "started_at": fields.get("ExecMainStartTimestamp", ""),
            "memory_bytes": memory_bytes,
            "memory_human": _format_bytes(memory_bytes),
            "cpu_seconds": round(cpu_nsec / 1_000_000_000, 2),
            "tasks_current": tasks_current,
            "process": _load_process_snapshot(main_pid),
        }
    )
    return status


def _service_action(action: str) -> tuple[bool, str]:
    action = action.strip().lower()
    if action not in {"start", "stop", "restart"}:
        return False, f"Unsupported action: {action}"
    if shutil.which("systemctl") is None:
        return False, "systemctl is not available on this host"

    commands = [
        ["systemctl", action, _SERVICE_NAME],
        ["sudo", "-n", "systemctl", action, _SERVICE_NAME],
    ]
    ok, _, err = _run_first_success(commands, timeout=12)
    if ok:
        return True, f"Service {_SERVICE_NAME} {action} command succeeded"
    return False, _compact_error(err)


def _ledger_snapshot(*, include_history: bool, history_limit: int) -> tuple[dict[str, object], list[dict[str, str]]]:
    stats: dict[str, object] = {
        "total_sightings": 0,
        "unique_dates_seen": 0,
        "locations_seen": 0,
        "first_seen": "",
        "last_seen": "",
        "by_source": {},
        "total_slots": 0,
        "booked": 0,
        "notified": 0,
        "total_valid_dates": 0,
        "unique_valid_dates": 0,
        "valid_locations": 0,
        "best_days_earlier": 0,
        "valid_by_source": {},
    }
    history: list[dict[str, str]] = []

    try:
        from slot_ledger import SlotLedger

        ledger = SlotLedger()
        unique_stats = ledger.analytics_summary()
        sighting_stats = ledger.sightings_summary()
        valid_stats = ledger.valid_reschedule_summary()
        stats.update(
            {
                "total_sightings": int(sighting_stats.get("total_sightings", 0) or 0),
                "unique_dates_seen": int(sighting_stats.get("unique_dates", 0) or 0),
                "locations_seen": int(sighting_stats.get("locations", 0) or 0),
                "first_seen": str(sighting_stats.get("first_seen", "") or ""),
                "last_seen": str(sighting_stats.get("last_seen", "") or ""),
                "by_source": sighting_stats.get("by_source", {}) or {},
                "total_slots": int(unique_stats.get("total_slots", 0) or 0),
                "booked": int(unique_stats.get("booked", 0) or 0),
                "notified": int(unique_stats.get("notified", 0) or 0),
                "total_valid_dates": int(valid_stats.get("total_valid_dates", 0) or 0),
                "unique_valid_dates": int(valid_stats.get("unique_valid_dates", 0) or 0),
                "valid_locations": int(valid_stats.get("valid_locations", 0) or 0),
                "best_days_earlier": int(valid_stats.get("best_days_earlier", 0) or 0),
                "valid_by_source": valid_stats.get("by_source", {}) or {},
            }
        )
        if include_history:
            safe_limit = max(1, min(history_limit, 1000))
            history = ledger.recent_sightings(limit=safe_limit)
    except Exception as exc:  # noqa: BLE001
        stats["error"] = str(exc)

    return stats, history


def _runtime_snapshot(*, include_history: bool = False, history_limit: int = 200) -> dict[str, object]:
    service = _service_status_snapshot()
    date_stats, history = _ledger_snapshot(include_history=include_history, history_limit=history_limit)
    heartbeat = _load_heartbeat_payload()
    strategy = _strategy_snapshot()
    heatmap = _opportunity_heatmap(history if include_history else [])

    payload: dict[str, object] = {
        "checker_service": str(service.get("active_state", "unknown")),
        "checker_sub_state": str(service.get("sub_state", "unknown")),
        "service_name": _SERVICE_NAME,
        "service_status": service,
        "log_path": _LOG_PATH,
        "last_log_line": _read_last_log_line(),
        "update_running": _update_running,
        "timestamp": int(time.time()),
        "date_stats": date_stats,
        "gate_status": heartbeat.get("gate_status", {}),
        "heartbeat": {
            "available": bool(heartbeat.get("available", False)),
            "path": str(heartbeat.get("path", "") or ""),
            "timestamp": str(heartbeat.get("timestamp", "") or ""),
            "status": str(heartbeat.get("status", "") or ""),
        },
        "facility_priority": heartbeat.get("facility_priority", []),
        "strategy": strategy,
        "opportunity_heatmap": heatmap,
        "interventions": [
            {
                "name": name,
                "label": str(spec.get("label", name) or name),
                "description": str(spec.get("description", "") or ""),
            }
            for name, spec in _INTERVENTION_ACTIONS.items()
        ],
    }
    if include_history:
        payload["recent_dates"] = history
    return payload


def _collect_updates_from_form(current: dict[str, str]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for key in CONFIG_KEYS:
        if key in BOOLEAN_KEYS:
            if key in _FORM_BOOLEAN_KEYS:
                updates[key] = "True" if request.form.get(key) else "False"
            else:
                updates[key] = current.get(key, "")
            continue

        submitted = request.form.get(key)
        if submitted is None:
            updates[key] = current.get(key, "")
        else:
            updates[key] = submitted.strip()
    return updates


def _validate_updates(updates: dict[str, str]) -> list[str]:
    errors: list[str] = []

    for key in _INT_KEYS:
        raw = updates.get(key, "").strip()
        if raw == "":
            # Some advanced keys are intentionally absent from the web form and
            # can rely on backend defaults when left blank.
            continue
        try:
            int(raw)
        except ValueError:
            errors.append(f"{key} must be an integer")

    for key in _FLOAT_KEYS:
        raw = updates.get(key, "").strip()
        if raw == "":
            continue
        try:
            float(raw)
        except ValueError:
            errors.append(f"{key} must be a float")

    if updates.get("CHECK_FREQUENCY_MINUTES", "").strip() == "":
        errors.append("CHECK_FREQUENCY_MINUTES must be an integer")
    if updates.get("SMTP_PORT", "").strip() == "":
        errors.append("SMTP_PORT must be an integer")

    try:
        datetime.strptime(updates.get("CURRENT_APPOINTMENT_DATE", ""), "%Y-%m-%d")
    except ValueError:
        errors.append("CURRENT_APPOINTMENT_DATE must be formatted as YYYY-MM-DD")

    try:
        start_dt = datetime.strptime(updates.get("START_DATE", ""), "%Y-%m-%d")
        end_dt = datetime.strptime(updates.get("END_DATE", ""), "%Y-%m-%d")
        if start_dt > end_dt:
            errors.append("START_DATE must be earlier than or equal to END_DATE")
    except ValueError:
        errors.append("START_DATE and END_DATE must be formatted as YYYY-MM-DD")

    frequency = updates.get("CHECK_FREQUENCY_MINUTES", "")
    if frequency.isdigit() and int(frequency) < 1:
        errors.append("CHECK_FREQUENCY_MINUTES must be greater than or equal to 1")

    smtp_port = updates.get("SMTP_PORT", "")
    if smtp_port.isdigit():
        port = int(smtp_port)
        if not 1 <= port <= 65535:
            errors.append("SMTP_PORT must be between 1 and 65535")

    return errors


def _is_token_valid(candidate: str) -> bool:
    if not _ACCESS_TOKEN:
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, _ACCESS_TOKEN)


def _safe_next_target(raw_target: str) -> str:
    target = (raw_target or "").strip()
    if not target:
        return url_for("index")
    if not target.startswith("/"):
        return url_for("index")
    parsed = urlsplit(target)
    if parsed.netloc or parsed.scheme:
        return url_for("index")
    if parsed.path in {"/login", "/logout"}:
        return url_for("index")
    return parsed.path or url_for("index")


@app.before_request
def _optional_token_auth():
    """Require token auth when WEB_UI_TOKEN is configured.

    Works with either:
    - query/form param: token
    - header: X-Access-Token
    - session after first successful auth
    """
    if not _ACCESS_TOKEN:
        return None

    if request.endpoint in {"login", "logout", "static"}:
        return None

    if session.get("web_ui_authed") is True:
        return None

    supplied = (
        request.headers.get("X-Access-Token", "")
        or request.args.get("token", "")
        or request.form.get("token", "")
    )
    if _is_token_valid(supplied):
        session["web_ui_authed"] = True
        return None

    wants_html = request.accept_mimetypes.best_match(["text/html", "application/json"]) == "text/html"
    if wants_html and not request.path.startswith("/api/"):
        return redirect(url_for("login", next=request.path))

    return (
        jsonify(
            {
                "error": "unauthorized",
                "message": "Set WEB_UI_TOKEN and access using ?token=... or X-Access-Token header.",
            }
        ),
        401,
    )


@app.after_request
def _apply_cache_policy(response: Response) -> Response:
    """Set cache headers for faster static loads and fresh dynamic pages."""
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    elif request.path.startswith("/artifacts/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.path in {"/", "/control", "/dashboard", "/logs", "/update", "/analytics"}:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Browser-friendly login page for token-gated access."""
    if request.method == 'POST':
        token = request.form.get('token', '').strip()
        next_url = _safe_next_target(request.form.get('next', ''))
        if _is_token_valid(token):
            session['web_ui_authed'] = True
            flash('Access granted.', 'success')
            return redirect(next_url)
        flash('Invalid token.', 'error')

    next_url = _safe_next_target(request.args.get('next', ''))
    return render_template('login.html', next_url=next_url, token_required=bool(_ACCESS_TOKEN))


@app.route('/logout')
def logout():
    session.pop('web_ui_authed', None)
    flash('Logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/', methods=['GET', 'POST'])
def index():
    current = CONFIG_MANAGER.ui_values()

    if request.method == 'POST':
        updates = _collect_updates_from_form(current)
        validation_errors = _validate_updates(updates)

        if validation_errors:
            flash(
                "Configuration not saved:\n- " + "\n- ".join(validation_errors),
                "error",
            )
            return render_template('index.html', current=updates)

        CONFIG_MANAGER.save_updates(updates)

        flash('🚀 Strategic configuration saved successfully! Your optimization settings are now active.', 'success')
        return redirect(url_for('index'))

    return render_template('index.html', current=current)


@app.route('/analytics')
def analytics():
    """Web dashboard showing slot ledger analytics."""
    try:
        from slot_ledger import SlotLedger
        ledger = SlotLedger()
        stats = ledger.analytics_summary()
        recent = ledger.recent_slots(limit=50)
        valid_stats = ledger.valid_reschedule_summary()
        valid_recent = ledger.recent_valid_reschedule_dates(limit=50)
    except Exception:
        stats = {}
        recent = []
        valid_stats = {}
        valid_recent = []

    total = stats.get("total_slots", 0)
    unique_dates = stats.get("unique_dates", 0)
    locations = stats.get("locations", 0)
    booked = stats.get("booked", 0)
    notified = stats.get("notified", 0)
    total_valid = valid_stats.get("total_valid_dates", 0)
    unique_valid = valid_stats.get("unique_valid_dates", 0)
    valid_locations = valid_stats.get("valid_locations", 0)
    best_days_earlier = valid_stats.get("best_days_earlier", 0)

    rows_html = ""
    for slot in recent:
        status = ""
        if slot.get("booked"):
            status = "&#x2705; Booked"
        elif slot.get("notified"):
            status = "&#x1F514; Notified"
        else:
            status = "&#x1F7E2; Seen"
        rows_html += (
            f"<tr><td>{slot.get('slot_date','')}</td>"
            f"<td>{slot.get('location','')}</td>"
            f"<td>{slot.get('discovered','')[:19]}</td>"
            f"<td>{status}</td></tr>\n"
        )

    valid_rows_html = ""
    for slot in valid_recent:
        valid_rows_html += (
            f"<tr><td>{slot.get('slot_date','')}</td>"
            f"<td>{slot.get('location','')}</td>"
            f"<td>{slot.get('discovered','')[:19]}</td>"
            f"<td>{slot.get('days_earlier', 0)}</td>"
            f"<td>{slot.get('source','')}</td></tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>Slot Analytics</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       max-width: 960px; margin: 2rem auto; padding: 0 1rem; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
          gap: 1rem; margin-bottom: 2rem; }}
.stat {{ background: #161b22; padding: 1.2rem; border-radius: 8px; text-align: center;
         border: 1px solid #30363d; }}
.stat .value {{ font-size: 2rem; font-weight: bold; color: #58a6ff; }}
.stat .label {{ font-size: 0.85rem; color: #8b949e; margin-top: 0.3rem; }}
table {{ width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px;
         overflow: hidden; border: 1px solid #30363d; }}
th, td {{ padding: 0.6rem 0.8rem; text-align: left; border-bottom: 1px solid #21262d; }}
th {{ background: #0d1117; color: #58a6ff; font-weight: 600; }}
a {{ color: #58a6ff; }}
</style>
</head>
<body>
<h1>&#x1F4CA; Slot Analytics Dashboard</h1>
<p>
    <a href=\"/\">&larr; Back to Config</a>
    &nbsp;|&nbsp;
    <a href=\"/dashboard\">Remote Control</a>
    &nbsp;|&nbsp;
    <a href=\"/logs\">Live Logs</a>
</p>
<div class=\"stats\">
  <div class=\"stat\"><div class=\"value\">{total}</div><div class=\"label\">Total Slots</div></div>
  <div class=\"stat\"><div class=\"value\">{unique_dates}</div><div class=\"label\">Unique Dates</div></div>
  <div class=\"stat\"><div class=\"value\">{locations}</div><div class=\"label\">Locations</div></div>
  <div class=\"stat\"><div class=\"value\">{booked}</div><div class=\"label\">Booked</div></div>
  <div class=\"stat\"><div class=\"value\">{notified}</div><div class=\"label\">Notified</div></div>
    <div class=\"stat\"><div class=\"value\">{total_valid}</div><div class=\"label\">Valid Reschedule Dates</div></div>
    <div class=\"stat\"><div class=\"value\">{unique_valid}</div><div class=\"label\">Unique Valid Dates</div></div>
    <div class=\"stat\"><div class=\"value\">{valid_locations}</div><div class=\"label\">Valid Locations</div></div>
    <div class=\"stat\"><div class=\"value\">{best_days_earlier}</div><div class=\"label\">Best Days Earlier</div></div>
</div>
<h2>Recent Slots</h2>
<table><thead><tr><th>Date</th><th>Location</th><th>Discovered</th><th>Status</th></tr></thead>
<tbody>{rows_html if rows_html else '<tr><td colspan=\"4\">No slots recorded yet</td></tr>'}
</tbody></table>
<h2 style=\"margin-top: 1.6rem;\">Recent Valid Reschedule Candidates</h2>
<table><thead><tr><th>Date</th><th>Location</th><th>Discovered</th><th>Days Earlier</th><th>Source</th></tr></thead>
<tbody>{valid_rows_html if valid_rows_html else '<tr><td colspan=\"5\">No valid reschedule dates recorded yet</td></tr>'}
</tbody></table>
</body></html>"""
    return html


@app.route('/logs')
def logs():
    """Live log viewer page."""
    return render_template('logs.html')


@app.route('/stream/logs')
def stream_logs():
    """Server-Sent Events endpoint that tails the log file."""
    def generate():
        # Send the last 200 lines of the log file first (catch-up)
        try:
            if os.path.exists(_LOG_PATH):
                with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                catchup = lines[-200:] if len(lines) > 200 else lines
                for line in catchup:
                    yield f"data: {line.rstrip()}\n\n"
        except OSError:
            yield "data: [log file not found – start the checker to generate logs]\n\n"

        # Now tail for new lines
        try:
            with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(0, 2)  # seek to end
                idle_ticks = 0
                while True:
                    line = f.readline()
                    if line:
                        idle_ticks = 0
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        time.sleep(0.5)
                        idle_ticks += 1
                        # Send a keep-alive comment every 30 seconds of inactivity
                        if idle_ticks >= 60:
                            idle_ticks = 0
                            yield ": keep-alive\n\n"
        except OSError:
            yield "data: [log file disappeared]\n\n"

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/api/warnings/screenshots', methods=['GET'])
def api_warning_screenshots():
    """Return recent warning PNG artifacts for WebUI rendering."""
    limit = _safe_int(request.args.get('limit', '40'), 40)
    severity = request.args.get('severity', '')
    payload = _warning_screenshot_payload(limit=limit, severity=severity)
    payload['timestamp'] = int(time.time())
    return jsonify(payload)


@app.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    """Return token/session visibility for compact UI status widgets."""
    token_required = bool(_ACCESS_TOKEN)
    session_authenticated = True
    if token_required:
        session_authenticated = session.get('web_ui_authed') is True

    return jsonify(
        {
            'token_required': token_required,
            'session_authenticated': session_authenticated,
            'timestamp': int(time.time()),
        }
    )


@app.route('/artifacts/warnings/<path:filename>', methods=['GET'])
def warning_artifact_file(filename: str):
    """Serve warning PNG artifacts from artifacts/ safely."""
    if not filename or filename != os.path.basename(filename):
        abort(400)

    lowered = filename.lower()
    if not lowered.endswith('.png') or '_warning_' not in lowered:
        abort(404)

    file_path = os.path.join(_ARTIFACTS_PATH, filename)
    if not os.path.isfile(file_path):
        abort(404)

    return send_from_directory(_ARTIFACTS_PATH, filename, mimetype='image/png')


@app.route('/update', methods=['GET'])
def update_page():
    """Web-based update page."""
    return render_template('update.html')


@app.route('/control', methods=['GET'])
@app.route('/dashboard', methods=['GET'])
def control_page():
    """Unified remote control + runtime dashboard."""
    return render_template('control.html')


@app.route('/api/update', methods=['POST'])
def api_update():
    """Trigger a git pull + pip install and stream output back as plain text."""
    global _update_running

    with _update_lock:
        if _update_running:
            return jsonify({'error': 'An update is already in progress'}), 409
        _update_running = True
        _update_output.clear()

    def run_update():
        global _update_running
        project_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            _append_output("🔄 Starting update...\n")
            # git pull
            result = subprocess.run(
                ['git', 'pull', 'origin', 'main'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            _append_output(result.stdout or '')
            if result.stderr:
                _append_output(result.stderr)
            if result.returncode != 0:
                _append_output(f"⚠️  git pull exited with code {result.returncode}\n")
                return

            # pip install
            _append_output("\n📦 Updating Python dependencies...\n")
            pip_cmd = ['pip', 'install', '--upgrade', '-r', 'requirements.txt']
            result = subprocess.run(
                pip_cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            _append_output(result.stdout or '')
            if result.stderr:
                _append_output(result.stderr)
            if result.returncode == 0:
                _append_output("\n✅ Update complete! Restart the checker for changes to take effect.\n")
            else:
                _append_output(f"\n⚠️  pip install exited with code {result.returncode}\n")
        except Exception as exc:
            _append_output(f"\n❌ Error during update: {exc}\n")
        finally:
            with _update_lock:
                _update_running = False

    thread = threading.Thread(target=run_update, daemon=True)
    thread.start()
    return jsonify({'status': 'started'})


@app.route('/api/update/status')
def api_update_status():
    """Return current update output and running state."""
    with _update_lock:
        running = _update_running
        output = list(_update_output)
    return jsonify({'running': running, 'output': output})


@app.route('/api/runtime')
def api_runtime():
    """Return runtime visibility for remote monitoring."""
    return jsonify(_runtime_snapshot(include_history=False))


@app.route('/api/service/status', methods=['GET'])
def api_service_status():
    """Return service/process stats plus recent date sightings."""
    limit = _safe_int(request.args.get('limit', '200'), 200)
    return jsonify(_runtime_snapshot(include_history=True, history_limit=limit))


@app.route('/api/service/<action>', methods=['POST'])
def api_service_action(action: str):
    """Start, stop, or restart the checker service."""
    ok, message = _service_action(action)
    payload = _runtime_snapshot(include_history=False)
    payload['action'] = action
    payload['action_ok'] = ok
    payload['message'] = message

    status_code = 200 if ok else 500
    if not payload['service_status'].get('control_supported'):
        status_code = 501
    elif action.strip().lower() not in {'start', 'stop', 'restart'}:
        status_code = 400
    return jsonify(payload), status_code


@app.route('/api/strategy/<preset>', methods=['POST'])
def api_strategy_apply(preset: str):
    """Apply a strategy preset and optionally restart the checker service."""
    restart = _parse_bool_flag(request.args.get('restart', '1'), default=True)
    ok, message, updates = _apply_strategy_preset(preset, restart_service=restart)

    payload = _runtime_snapshot(include_history=False)
    payload['action'] = 'strategy'
    payload['preset'] = preset
    payload['restart'] = restart
    payload['updates'] = updates
    payload['action_ok'] = ok
    payload['message'] = message

    status_code = 200 if ok else 400
    return jsonify(payload), status_code


@app.route('/api/intervention/<action>', methods=['POST'])
def api_intervention_action(action: str):
    """Run one-click intervention workflows (strategy change + restart policy)."""
    key = action.strip().lower()
    spec = _INTERVENTION_ACTIONS.get(key)
    if spec is None:
        payload = _runtime_snapshot(include_history=False)
        payload['action'] = action
        payload['action_ok'] = False
        payload['message'] = f"Unsupported intervention action: {action}"
        return jsonify(payload), 400

    preset = str(spec.get('preset', '') or '')
    restart_default = bool(spec.get('restart', True))
    restart = _parse_bool_flag(request.args.get('restart', ''), default=restart_default)

    ok, message, updates = _apply_strategy_preset(preset, restart_service=restart)

    payload = _runtime_snapshot(include_history=False)
    payload['action'] = key
    payload['preset'] = preset
    payload['restart'] = restart
    payload['updates'] = updates
    payload['action_ok'] = ok
    payload['message'] = message

    status_code = 200 if ok else 400
    return jsonify(payload), status_code


@app.route('/api/dates/history', methods=['GET'])
def api_dates_history():
    """Return saved date sightings (including duplicate sightings over time)."""
    limit = _safe_int(request.args.get('limit', '500'), 500)
    stats, history = _ledger_snapshot(include_history=True, history_limit=limit)
    return jsonify({
        'date_stats': stats,
        'history': history,
        'count': len(history),
        'timestamp': int(time.time()),
    })


def _append_output(text: str) -> None:
    with _update_lock:
        _update_output.append(text)
        # Keep buffer bounded
        if len(_update_output) > 500:
            _update_output.pop(0)


if __name__ == '__main__':
    import socket

    parser = argparse.ArgumentParser(description="US Visa Checker Web UI")
    parser.add_argument(
        "--host",
        default=os.getenv("WEB_UI_HOST", "127.0.0.1"),
        help="Host interface to bind (default: 127.0.0.1, use 0.0.0.0 for remote access)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("WEB_UI_PORT", "5000")),
        help="Port to bind (default: 5000)",
    )
    args = parser.parse_args()
    
    # Try to find an available port
    def find_free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port
    
    port = args.port
    try:
        app.run(debug=False, port=port, host=args.host)
    except OSError:
        port = find_free_port()
        print(f"Port {args.port} is in use, trying port {port}")
        app.run(debug=False, port=port, host=args.host)
