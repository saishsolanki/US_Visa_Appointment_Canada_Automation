# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Critical**: Added missing `import random` — `_schedule_backoff()` was crashing
  with `NameError` every time the calendar was busy or a captcha was detected.

### Added
- **Network health detection**: New `_is_network_error()`, `_check_internet_connectivity()`,
  `_record_network_success()`, and `_record_network_failure()` methods detect
  DNS / connectivity outages and track them separately from application errors.
- **Exponential backoff for network failures**: Main loop now enters a progressive
  backoff (30 s → 1 m → 2 m → … → 15 m cap) when consecutive network errors are
  detected instead of hammering a dead connection every 3 minutes.
- **Connectivity pre-check**: Before starting a check cycle, the loop probes TCP
  connectivity to well-known DNS servers. If the probe fails, the cycle is skipped
  and the backoff window is extended — saving Chrome startups and log noise.
- Added `import socket` for lightweight connectivity probing.

### Changed
- `_safe_get()`: Network errors now fail-fast instead of retrying linearly —
  the main loop handles the retry cadence with proper backoff.
- `_safe_get()`: Records network success on every successful page load.
- `_handle_error()`: Skips artifact capture and email notification for network
  errors (the screenshot would be empty and the email would fail anyway).
- `ProgressReporter._report_loop()`: Failed email sends now use exponential
  backoff (5 m → 10 m → 20 m → … → 2 h cap) instead of retrying every minute
  and flooding the log file.

### Added
- Minimal smoke test coverage for:
  - config loading/validation (`CheckerConfig.load`)
  - login selector presence (email/password/sign-in selectors)
  - calendar date parsing format compatibility
- New CI workflow (`.github/workflows/ci.yml`) that runs:
  - secrets safety path checks
  - `ruff` lint checks
  - `black --check` formatting checks
  - `pytest` test suite

### Changed
- Startup configuration validation now returns a structured error list for invalid values.
- Validation now explicitly checks:
  - `CURRENT_APPOINTMENT_DATE` format (`YYYY-MM-DD`)
  - `CHECK_FREQUENCY_MINUTES >= 1`
  - `SMTP_PORT` range (`1..65535`)

## [v2.0.0] - 2026-02-13

### Added
- Release packaging workflow for versioned GitHub releases and Docker image tags.
- Linux systemd service installation section in the README.

### Changed
- Docker healthcheck now validates that the visa checker process is active.
