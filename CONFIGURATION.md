# Configuration Reference

All settings go in `config.ini` (or as environment variables).

Start with `cp config.ini.template config.ini` and edit.

---

## Essential Settings

| Setting | Example | Required |
|---------|---------|----------|
| `email` | your_ais_email@example.com | ✅ Yes |
| `password` | your_ais_password | ✅ Yes |
| `current_appointment_date` | 2025-12-01 | ✅ Yes |
| `location` | Ottawa - U.S. Embassy | ✅ Yes |
| `start_date` | 2025-09-25 | ✅ Yes |
| `end_date` | 2025-12-31 | ✅ Yes |
| `country_code` | en-ca | ✅ Yes |

---

## Notifications

| Setting | Example | Purpose |
|---------|---------|---------|
| `smtp_server` | smtp.gmail.com | Email provider |
| `smtp_port` | 587 | SMTP port |
| `smtp_user` | your_gmail@gmail.com | Gmail address |
| `smtp_pass` | your_16_char_app_password | Gmail App Password (not main password!) |
| `notify_email` | your_gmail@gmail.com | Send alerts to |
| `telegram_bot_token` | YOUR_TOKEN | Telegram bot token |
| `telegram_chat_id` | YOUR_CHAT_ID | Telegram chat ID |
| `pushover_app_token` | YOUR_APP_TOKEN | Pushover app token |
| `pushover_user_key` | YOUR_USER_KEY | Pushover user key |
| `sendgrid_api_key` | YOUR_API_KEY | SendGrid API key |
| `sendgrid_from_email` | sender@example.com | SendGrid sender |
| `sendgrid_to_email` | recipient@example.com | SendGrid recipient |
| `webhook_url` | https://your-webhook.url | Custom webhook |
| `audio_alerts_enabled` | True | Beep on new slots |

---

## Behavior & Safety

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `check_frequency_minutes` | 2-15 | 5 | How often to check (lower = more traffic) |
| `auto_book` | True/False | False | Automatically book found slots |
| `auto_book_dry_run` | True/False | True | Validate auto-book logic without booking |
| `abort_on_captcha` | True/False | False | Stop if captcha is detected |
| `test_mode` | True/False | False | Run without probing (test login/nav only) |
| `safety_first_mode` | True/False | False | Conservative polling (lower anti-bot risk) |
| `safety_first_min_interval_minutes` | 10-30 | 10 | Minimum interval in safety mode |

---

## Optimization Features

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `burst_mode_enabled` | True/False | True | Rapid checks during peak hours |
| `multi_location_check` | True/False | True | Check backup locations |
| `backup_locations` | Comma-separated | Toronto,Montreal | Other embassies to check |
| `prime_hours_start` | Hours | 6,12,17,22 | Prime time hours |
| `prime_hours_end` | Hours | 9,14,19,1 | Prime time ends |
| `prime_time_backoff_multiplier` | 0.1-1.0 | 0.5 | 0.5 = 50% faster during prime time |
| `weekend_frequency_multiplier` | 1.0-3.0 | 2.0 | 2.0 = 2x slower on weekends |
| `pattern_learning_enabled` | True/False | True | Learn when slots release |

---

## Advanced Settings

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `max_retry_attempts` | 1-5 | 2 | Retry failed page loads |
| `retry_backoff_seconds` | 5-60 | 5 | Wait between retries |
| `sleep_jitter_seconds` | 0-120 | 60 | Random delay to avoid detection |
| `busy_backoff_min_minutes` | 5-30 | 10 | Min wait when calendar is busy |
| `busy_backoff_max_minutes` | 30-120 | 30 | Max wait when calendar is busy |
| `driver_restart_checks` | 30-100 | 50 | Restart browser after N checks |
| `heartbeat_path` | /path/to/file | (none) | Write status to file |
| `max_requests_per_hour` | 1-500 | 120 | Rate limit requests |
| `max_api_requests_per_hour` | 1-500 | 120 | Rate limit API calls |
| `max_ui_navigations_per_hour` | 1-500 | 60 | Rate limit UI navigation |
| `slot_ttl_hours` | 1-24 | 24 | Keep found slots for N hours |

---

## Account Rotation

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `account_rotation_enabled` | True/False | False | Rotate between multiple accounts |
| `rotation_accounts` | email1:pass1,email2:pass2 | (none) | Additional accounts |
| `rotation_interval_checks` | 1-100 | 1 | Rotate every N checks |

---

## VPN

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `vpn_provider` | none/proton | none | VPN to use |
| `vpn_cli_path` | /path/to/cli | protonvpn | VPN CLI path |
| `vpn_server` | server name | (auto) | Specific VPN server |
| `vpn_country` | country code | (auto) | VPN country |
| `vpn_require_connected` | True/False | False | Fail if VPN disconnects |
| `vpn_rotate_on_captcha` | True/False | True | Change VPN on captcha |
| `vpn_reconnect_on_network_error` | True/False | True | Reconnect on network error |
| `vpn_min_session_minutes` | 5-60 | 10 | Min VPN connection time |

---

## Date Exclusions

Use `excluded_date_ranges` to skip specific date windows (up to 9 ranges):

```ini
excluded_date_ranges = 2025-09-25 2025-09-30, 2025-10-15 2025-10-20
```

---

## Timezone

| Setting | Example | Default |
|---------|---------|---------|
| `timezone` | America/Toronto | System timezone |

---

## Getting Help

- Copy template: `cp config.ini.template config.ini`
- Run setup wizard: `python visa_appointment_checker.py --setup`
- Check email setup: [GMAIL_SETUP_GUIDE.md](GMAIL_SETUP_GUIDE.md)
- See all CLI options: `python visa_appointment_checker.py --help`
