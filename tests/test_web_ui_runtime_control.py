from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_ui


def _runtime_payload(control_supported: bool = True) -> dict[str, object]:
    return {
        "checker_service": "active",
        "checker_sub_state": "running",
        "service_name": "visa-checker.service",
        "service_status": {
            "name": "visa-checker.service",
            "control_supported": control_supported,
            "active_state": "active",
            "sub_state": "running",
            "main_pid": 1234,
            "restarts": 0,
            "exec_status": "0",
            "started_at": "Sat 2026-04-18 02:30:32 UTC",
            "memory_human": "80.2 MB",
            "cpu_seconds": 12.5,
            "process": {
                "pid": "1234",
                "cpu_percent": "1.2",
                "mem_percent": "2.1",
                "elapsed": "00:11:22",
                "command": "python visa_appointment_checker.py",
                "error": "",
            },
            "last_error": "",
        },
        "log_path": "logs/visa_checker.log",
        "last_log_line": "Sample log line",
        "update_running": False,
        "timestamp": 1713300000,
        "date_stats": {
            "total_sightings": 5,
            "unique_dates_seen": 3,
            "locations_seen": 2,
            "first_seen": "2026-04-17T22:00:00",
            "last_seen": "2026-04-17T22:10:00",
            "by_source": {"api": 4, "ui": 1},
            "total_slots": 3,
            "booked": 0,
            "notified": 2,
        },
        "gate_status": {
            "warning_gate_streak": 0,
            "breaker_active": False,
            "breaker_remaining_seconds": 0,
            "last_real_slot_eval_at": "",
            "api_checks": 10,
            "ui_checks": 5,
            "api_vs_ui_ratio": 0.667,
            "warning_page_hits": 1,
            "continue_success_count": 1,
        },
        "heartbeat": {
            "available": True,
            "path": "logs/runtime_heartbeat.json",
            "timestamp": "2026-04-18T02:30:32+00:00",
            "status": "success",
        },
        "strategy": {
            "active_preset": "balanced",
            "presets": [],
            "current": {},
        },
        "opportunity_heatmap": {
            "hours": [],
            "locations": [],
            "matrix": [],
            "max_count": 0,
            "sample_count": 0,
        },
        "interventions": [],
        "facility_priority": [],
    }


def test_api_service_status_includes_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    def fake_runtime_snapshot(*, include_history: bool = False, history_limit: int = 200) -> dict[str, object]:
        payload = _runtime_payload()
        if include_history:
            payload["recent_dates"] = [
                {
                    "slot_date": "2026-08-15",
                    "location": "Ottawa",
                    "discovered": "2026-04-17T22:09:00",
                    "source": "api",
                }
            ]
        return payload

    monkeypatch.setattr(web_ui, "_runtime_snapshot", fake_runtime_snapshot)

    client = web_ui.app.test_client()
    response = client.get("/api/service/status?limit=25")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["service_status"]["active_state"] == "active"
    assert len(payload["recent_dates"]) == 1


def test_api_service_action_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")
    monkeypatch.setattr(web_ui, "_service_action", lambda action: (True, f"ok {action}"))
    monkeypatch.setattr(web_ui, "_runtime_snapshot", lambda **kwargs: _runtime_payload())

    client = web_ui.app.test_client()
    response = client.post("/api/service/restart")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["action"] == "restart"
    assert payload["action_ok"] is True
    assert payload["message"] == "ok restart"


def test_api_service_action_invalid_action_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")
    monkeypatch.setattr(web_ui, "_service_action", lambda action: (False, f"unsupported {action}"))
    monkeypatch.setattr(web_ui, "_runtime_snapshot", lambda **kwargs: _runtime_payload())

    client = web_ui.app.test_client()
    response = client.post("/api/service/bogus")
    assert response.status_code == 400

    payload = response.get_json()
    assert payload["action_ok"] is False


def test_api_service_action_unsupported_host_returns_501(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")
    monkeypatch.setattr(web_ui, "_service_action", lambda action: (False, "systemctl missing"))
    monkeypatch.setattr(web_ui, "_runtime_snapshot", lambda **kwargs: _runtime_payload(control_supported=False))

    client = web_ui.app.test_client()
    response = client.post("/api/service/start")
    assert response.status_code == 501


def test_api_strategy_apply_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")
    monkeypatch.setattr(
        web_ui,
        "_apply_strategy_preset",
        lambda preset, restart_service: (True, f"applied {preset}", {"CHECK_FREQUENCY_MINUTES": "3"}),
    )
    monkeypatch.setattr(web_ui, "_runtime_snapshot", lambda **kwargs: _runtime_payload())

    client = web_ui.app.test_client()
    response = client.post("/api/strategy/balanced?restart=1")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["action"] == "strategy"
    assert payload["action_ok"] is True
    assert payload["preset"] == "balanced"


def test_api_intervention_invalid_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")
    monkeypatch.setattr(web_ui, "_runtime_snapshot", lambda **kwargs: _runtime_payload())

    client = web_ui.app.test_client()
    response = client.post("/api/intervention/not-real")
    assert response.status_code == 400

    payload = response.get_json()
    assert payload["action_ok"] is False


def test_api_dates_history_uses_ledger_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    def fake_ledger_snapshot(*, include_history: bool, history_limit: int):
        assert include_history is True
        assert history_limit == 12
        return (
            {
                "total_sightings": 9,
                "unique_dates_seen": 3,
                "locations_seen": 2,
                "first_seen": "",
                "last_seen": "",
                "by_source": {},
                "total_slots": 3,
                "booked": 1,
                "notified": 2,
            },
            [
                {
                    "slot_date": "2026-08-15",
                    "location": "Ottawa",
                    "discovered": "2026-04-17T22:09:00",
                    "source": "api",
                }
            ],
        )

    monkeypatch.setattr(web_ui, "_ledger_snapshot", fake_ledger_snapshot)

    client = web_ui.app.test_client()
    response = client.get("/api/dates/history?limit=12")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["date_stats"]["total_sightings"] == 9
    assert payload["count"] == 1


def test_dashboard_alias_renders_control_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    client = web_ui.app.test_client()
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Remote Runtime Control" in response.data


def test_index_contains_immediate_control_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    client = web_ui.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"/dashboard" in response.data
    assert b"Control Center" in response.data


def test_api_warning_screenshots_lists_warning_pngs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "20260418-010101-000001_warning_rate_limit.png").write_bytes(b"png")
    (artifacts_dir / "20260418-010101-000002_debug_state.png").write_bytes(b"png")

    monkeypatch.setattr(web_ui, "_ARTIFACTS_PATH", str(artifacts_dir))

    client = web_ui.app.test_client()
    response = client.get("/api/warnings/screenshots?limit=10")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["items"][0]["name"].endswith("_warning_rate_limit.png")
    assert payload["items"][0]["url"].startswith("/artifacts/warnings/")
    assert payload["items"][0]["severity"] == "rate-limit"
    assert payload["retention"]["artifact_count"] == 1
    assert payload["severity_counts"]["rate-limit"] == 1
    assert len(payload["trends"]["hourly"]["values"]) == 24
    assert len(payload["trends"]["daily"]["values"]) == 7


def test_warning_artifact_file_serves_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    filename = "20260418-010101-000001_warning_test.png"
    (artifacts_dir / filename).write_bytes(b"fakepng")

    monkeypatch.setattr(web_ui, "_ARTIFACTS_PATH", str(artifacts_dir))

    client = web_ui.app.test_client()
    response = client.get(f"/artifacts/warnings/{filename}")
    assert response.status_code == 200
    assert response.mimetype == "image/png"


def test_warning_artifact_file_rejects_non_warning_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    filename = "20260418-010101-000001_debug_state.png"
    (artifacts_dir / filename).write_bytes(b"fakepng")

    monkeypatch.setattr(web_ui, "_ARTIFACTS_PATH", str(artifacts_dir))

    client = web_ui.app.test_client()
    response = client.get(f"/artifacts/warnings/{filename}")
    assert response.status_code == 404


def test_api_auth_status_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "")

    client = web_ui.app.test_client()
    response = client.get("/api/auth/status")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["token_required"] is False
    assert payload["session_authenticated"] is True


def test_api_auth_status_with_token_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui, "_ACCESS_TOKEN", "example-token")

    client = web_ui.app.test_client()
    with client.session_transaction() as sess:
        sess["web_ui_authed"] = True

    response = client.get("/api/auth/status")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["token_required"] is True
    assert payload["session_authenticated"] is True
