from getpass import getpass

from config_manager import ConfigManager


def run_cli_setup_wizard(config_path: str = "config.ini", template_path: str = "config.ini.template") -> None:
    manager = ConfigManager(config_path=config_path, template_path=template_path)
    parser = manager.load_parser()

    def _get(name: str, fallback: str = "") -> str:
        return ConfigManager.get_case_insensitive(parser, name, fallback).strip()

    def _set(name: str, value: str) -> None:
        ConfigManager.set_case_insensitive(parser, name, value)

    def _prompt(name: str, label: str, *, secret: bool = False, required: bool = True) -> str:
        current = _get(name)
        prompt = f"{label}"
        if current:
            prompt += f" [{current}]"
        prompt += ": "
        while True:
            raw = getpass(prompt) if secret else input(prompt)
            value = raw.strip() or current
            if value or not required:
                _set(name, value)
                return value
            print("This value is required.")

    print("🛠️  CLI Setup Wizard")
    print("Press Enter to accept defaults shown in brackets.\n")
    _prompt("EMAIL", "AIS login email")
    _prompt("PASSWORD", "AIS login password", secret=True)
    _prompt("CURRENT_APPOINTMENT_DATE", "Current appointment date (YYYY-MM-DD)")
    _prompt("LOCATION", "Preferred location (example: Ottawa - U.S. Embassy)")
    _prompt("COUNTRY_CODE", "AIS country code (example: en-ca)", required=False)
    _prompt("SCHEDULE_ID", "AIS schedule ID (optional, numeric)", required=False)
    _prompt("FACILITY_ID", "Facility ID override (optional, numeric)", required=False)
    _prompt("START_DATE", "Search start date (YYYY-MM-DD)")
    _prompt("END_DATE", "Search end date (YYYY-MM-DD)")
    _prompt("CHECK_FREQUENCY_MINUTES", "Check frequency in minutes")

    smtp_profiles = {
        "1": ("Gmail", "smtp.gmail.com", "587"),
        "2": ("Outlook", "smtp.office365.com", "587"),
        "3": ("SendGrid", "smtp.sendgrid.net", "587"),
        "4": ("Amazon SES", "email-smtp.us-east-1.amazonaws.com", "587"),
        "5": ("Custom", _get("SMTP_SERVER", "smtp.gmail.com"), _get("SMTP_PORT", "587")),
    }
    print("\nSMTP provider:")
    print("  1) Gmail  2) Outlook  3) SendGrid  4) Amazon SES  5) Custom")
    profile_choice = input("Choose provider [1]: ").strip() or "1"
    _, smtp_server, smtp_port = smtp_profiles.get(profile_choice, smtp_profiles["1"])
    _set("SMTP_SERVER", smtp_server)
    _set("SMTP_PORT", smtp_port)
    smtp_user = _prompt("SMTP_USER", "SMTP username")
    _prompt("SMTP_PASS", "SMTP password / app password / API key", secret=True)
    _prompt("NOTIFY_EMAIL", "Notification email", required=False)
    if not _get("NOTIFY_EMAIL") and smtp_user:
        _set("NOTIFY_EMAIL", smtp_user)
    _prompt("AUTO_BOOK", "Auto-book when eligible appointment found? (True/False)")
    _prompt("TEST_MODE", "Run in dedicated safe test mode? (True/False)", required=False)
    _prompt("EXCLUDED_DATE_RANGES", "Excluded date ranges (YYYY-MM-DD:YYYY-MM-DD;...)", required=False)
    _prompt("SAFETY_FIRST_MODE", "Enable safety-first conservative polling mode? (True/False)", required=False)
    _prompt("AUDIO_ALERTS_ENABLED", "Enable audio alerts? (True/False)", required=False)
    _prompt("PUSHOVER_APP_TOKEN", "Pushover app token (optional)", required=False)
    _prompt("PUSHOVER_USER_KEY", "Pushover user key (optional)", required=False)
    _prompt("SENDGRID_API_KEY", "SendGrid API key (optional)", required=False)
    _prompt("SENDGRID_FROM_EMAIL", "SendGrid from email (optional)", required=False)
    _prompt("SENDGRID_TO_EMAIL", "SendGrid to email (optional)", required=False)
    _prompt("ACCOUNT_ROTATION_ENABLED", "Enable multi-account rotation? (True/False)", required=False)
    _prompt("ROTATION_ACCOUNTS", "Rotation accounts email|password;email|password (optional)", required=False)
    _prompt("ROTATION_INTERVAL_CHECKS", "Rotate account every N checks", required=False)

    manager.save_parser(parser)
    print(f"\n✅ Saved configuration to {config_path}")
