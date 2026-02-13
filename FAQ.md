# FAQ / Troubleshooting Quick Flow

## Start here

1. Run `python visa_appointment_checker.py --help`
2. If `config.ini` is missing, run `python visa_appointment_checker.py --setup`
3. Re-run the checker and match your error below

## If this error, then do this

| Error / Symptom | Do this |
|---|---|
| `Unable to load configuration` | Run `python visa_appointment_checker.py --setup` and complete all required fields. |
| `START_DATE and END_DATE must be formatted as YYYY-MM-DD` | Fix dates in `config.ini` to `YYYY-MM-DD` format. |
| `START_DATE must be earlier than or equal to END_DATE` | Update the date range so start date is not after end date. |
| `SMTP Authentication failed` | Verify SMTP user/password or app password/API key. For Gmail setup see `GMAIL_SETUP_GUIDE.md`. |
| Gmail auth still fails | Try Outlook/SendGrid/SES SMTP values from `README.md` non-Gmail examples. |
| Login fails or CAPTCHA loops | Run with `--no-headless`, log in manually once, then retry. Reduce check frequency if rate-limited. |
| `Could not find email/password field` | AIS page likely changed. Retry with `--no-headless`, accept cookies, and check `logs/visa_checker.log`. |
| No appointments found for long time | Expand date range and backup locations; keep checker running during prime hours. |

## CLI quick commands

```bash
# Show CLI help
python visa_appointment_checker.py --help

# Guided setup wizard
python visa_appointment_checker.py --setup

# Run in visible mode for troubleshooting
python visa_appointment_checker.py --no-headless
```
