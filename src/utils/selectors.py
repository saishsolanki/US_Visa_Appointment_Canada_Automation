import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Tuple

from selenium.webdriver.common.by import By

Selector = Tuple[str, str]

BY_MAP = {
    "ID": By.ID,
    "NAME": By.NAME,
    "XPATH": By.XPATH,
    "CSS_SELECTOR": By.CSS_SELECTOR,
    "TAG_NAME": By.TAG_NAME,
    "CLASS_NAME": By.CLASS_NAME,
    "LINK_TEXT": By.LINK_TEXT,
    "PARTIAL_LINK_TEXT": By.PARTIAL_LINK_TEXT,
}
_APPLIED_LOCK = threading.Lock()
_APPLIED_TARGETS = set()


def _load_yaml_like(path: Path):
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as json_exc:
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text)
        except Exception as yaml_exc:
            parsed = {}
            current_key = None
            current_item = None
            for raw_line in text.splitlines():
                line = raw_line.rstrip()
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not line.startswith(" ") and stripped.endswith(":"):
                    current_key = stripped[:-1]
                    parsed[current_key] = []
                    current_item = None
                    continue
                if current_key is None:
                    continue
                if stripped.startswith("- "):
                    current_item = {}
                    parsed[current_key].append(current_item)
                    stripped = stripped[2:].strip()
                    if ":" in stripped:
                        k, v = stripped.split(":", 1)
                        current_item[k.strip()] = v.strip().strip("'\"")
                    continue
                if ":" in stripped and current_item is not None:
                    k, v = stripped.split(":", 1)
                    current_item[k.strip()] = v.strip().strip("'\"")
            if parsed:
                return parsed
            logging.warning(
                "Unable to parse selector registry file: %s (json_error=%s, yaml_error=%s)",
                path,
                json_exc,
                yaml_exc,
            )
            return {}


def load_selector_registry(path: str = "selectors.yml") -> Dict[str, List[Selector]]:
    registry_path = Path(path)
    if not registry_path.exists():
        return {}
    raw = _load_yaml_like(registry_path)
    if not isinstance(raw, dict):
        return {}

    parsed: Dict[str, List[Selector]] = {}
    for key, value in raw.items():
        if not isinstance(value, list):
            continue
        selectors: List[Selector] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            by_name = str(item.get("by", "")).upper().strip()
            selector_value = str(item.get("value", "")).strip()
            if by_name in BY_MAP and selector_value:
                selectors.append((BY_MAP[by_name], selector_value))
        if selectors:
            parsed[str(key).strip()] = selectors
    return parsed


def apply_selector_overrides(target_cls, path: str = "selectors.yml") -> None:
    target_key = (id(target_cls), path)
    with _APPLIED_LOCK:
        if target_key in _APPLIED_TARGETS:
            return

    registry = load_selector_registry(path)
    if not registry:
        return

    for selector_name, override_selectors in registry.items():
        if not hasattr(target_cls, selector_name):
            continue
        default_selectors = list(getattr(target_cls, selector_name))
        merged = list(override_selectors)
        for selector in default_selectors:
            if selector not in merged:
                merged.append(selector)
        setattr(target_cls, selector_name, merged)
        logging.info(
            "Selector registry applied for %s (%d overrides + %d defaults)",
            selector_name,
            len(override_selectors),
            len(default_selectors),
        )
    with _APPLIED_LOCK:
        _APPLIED_TARGETS.add(target_key)
