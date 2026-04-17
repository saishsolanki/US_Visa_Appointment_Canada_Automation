# US Visa Automation Ecosystem Analysis (AIS-Focused)

## Scope
This document summarizes the current open-source ecosystem around US visa appointment rescheduling automation, especially projects targeting the AIS portal (`ais.usvisa-info.com`). It is intended as a practical engineering reference for this repository.

## 1) Technology Stacks and Language Patterns

- Python (most common): Selenium browser automation, requests-based endpoint polling, HTML parsing fallback.
- Node.js/JavaScript: Puppeteer-based headless Chromium automation, high async throughput.
- PowerShell: scheduler-friendly scripts that run per-invocation via Task Scheduler/cron rather than persistent loops.
- Browser console scripts: pasted JavaScript snippets for local monitoring/alerts with no local install.
- PHP helpers: lightweight relay endpoints for notification fanout.

## 2) Common AIS Execution Flow

- Login and authenticated session capture.
- Capture schedule context (`schedule_id`, cookies, CSRF/auth tokens).
- Poll JSON endpoints for date/time availability.
- Compare against user constraints (date range, current appointment, exclusions).
- Notify or auto-reschedule when candidate slot qualifies.

## 3) Advanced Features Observed in Higher-Reliability Repositories

- Dry-run/test mode to validate end-to-end flow safely.
- Multi-window date exclusions (not just one range).
- Multi-facility checks (Canada IDs 89-95) to widen opportunity surface.
- Headless + non-headless modes for both unattended and manual-supervised execution.
- Account/session rotation and adaptive cooldown behavior.
- Fast-path JSON polling (requests) with browser fallback when needed.

## 4) Anti-Bot and Safety Constraints

- Aggressive refresh can trigger throttling, forbidden responses, or lockouts.
- Jittered intervals and adaptive backoff reduce detectability and stress.
- Manual intervention points (captcha, warning pages) need explicit handling and user alerts.
- Reschedule attempt limits are finite; accidental retries must be guarded with safety checks and dry-run defaults.

## 5) Notification Reliability Landscape

- SMTP works but can be brittle with provider security policies.
- Push channels (Pushover/Telegram) are fast for flash-release windows.
- API mail channels (SendGrid) generally offer better automation reliability than personal mailbox auth flows.
- Local audio alerts remain useful during active desktop sessions.

## 6) Essential Configuration Parameters

- `COUNTRY_CODE`: AIS regional path segment (e.g., `en-ca`, `en-gb`).
- `SCHEDULE_ID`: schedule context ID from AIS URL path.
- `FACILITY_ID`: specific post/consulate numeric identifier.

## 7) Official Portal Context (AIS vs CGI)

### Primary official systems

- AIS portal: `ais.usvisa-info.com` (region-keyed routing such as `en-ca`, `en-gb`).
- CGI-style portals: `usvisascheduling.com` and/or `ustraveldocs.com` in several regions.

### Technical implications for tooling

- Tools must model portal-specific identifiers (facility/post IDs, schedule IDs, application-state IDs).
- Session state is tightly coupled to account navigation context and can invalidate fast with atypical automation cadence.
- JSON endpoint polling is generally faster and lower overhead than full-page scraping, but still constrained by server throttles and anti-bot controls.

### Defensive behaviors commonly observed

- Rate throttling and temporary lockouts after aggressive refresh patterns.
- `Forbidden` responses, empty availability arrays, or `System Busy` states during high-demand release windows.
- Scheduling/booking warning pages and captcha gates that require human intervention.

### Administrative and applicant risk surface

- Potential terms/policy violations when using third-party automation.
- Consequences of incorrect profile/questionnaire data can be severe and not always reversible after certain workflow states.
- MRV fee validity windows can expire while waiting for improved slots, independent of technical bot success.
- Reschedule attempt caps (commonly reported as limited attempts) require conservative automation behavior and clear guardrails.

## 8) Reliability-Oriented Repository Positioning

No public tool is universally "most reliable" across all regions and portal states. Reliability is situational and depends on:

- region/portal behavior,
- current anti-bot policies,
- operator risk tolerance,
- maintenance cadence,
- notification path reliability.

For this repository, practical reliability comes from:

- conservative defaults,
- adaptive backoff,
- run-once scheduler compatibility,
- multiple notification channels,
- safe test mode,
- and browser + API hybrid checks.

## 9) Action Checklist Applied in This Repository

- Added configurable AIS portal parameters (`COUNTRY_CODE`, `SCHEDULE_ID`, `FACILITY_ID`).
- Added scheduler-friendly one-shot mode (`--run-once`).
- Added SendGrid API notification channel.
- Preserved and documented safety-focused features (test mode, exclusions, safety-first, account rotation).

## 10) Legal and Operational Note

Automation can conflict with portal terms or local policy interpretations. Operators should review local rules and use conservative settings that prioritize account safety over aggressiveness.
