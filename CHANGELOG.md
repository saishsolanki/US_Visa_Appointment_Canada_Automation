# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
