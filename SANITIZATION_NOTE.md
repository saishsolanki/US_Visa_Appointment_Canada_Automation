# Sensitive Capture Handling

`visa.txt` contains raw HTTP captures and may include credentials, session cookies, CSRF tokens, and personal data.

## What to do
- Do not commit or share `visa.txt`.
- Rotate any exposed secrets (AIS password, SMTP app password, session cookies).
- Use a sanitized excerpt/fixture for tests instead of raw captures.

## Why this file exists
On some Windows + OneDrive setups, `visa.txt` may be locked read-only and cannot be modified in-place. This repo uses a small, sanitized fixture under `tests/fixtures/` for regression tests.
