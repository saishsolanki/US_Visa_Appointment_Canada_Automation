import configparser
from getpass import getpass


def run_cli_setup_wizard(config_path: str = "config.ini", template_path: str = "config.ini.template") -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(template_path)
    if "DEFAULT" not in parser:
        parser["DEFAULT"] = {}
    defaults = parser["DEFAULT"]

    def _get(name: str, fallback: str = "") -> str:
        for key, value in defaults.items():
            if key.upper() == name:
                return str(value).strip()
        return fallback

    def _set(name: str, value: str) -> None:
        for key in list(defaults.keys()):
            if key.upper() == name:
                defaults[key] = value
                return
        defaults[name.lower()] = value

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

    print("CLI Setup Wizard")
    print("Press Enter to accept defaults shown in brackets.\n")
    _prompt("EMAIL", "AIS login email")
    _prompt("PASSWORD", "AIS login password", secret=True)
    _prompt("CURRENT_APPOINTMENT_DATE", "Current appointment date (YYYY-MM-DD)")
    _prompt("LOCATION", "Preferred location (example: Ottawa - U.S. Embassy)")
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

    with open(config_path, "w", encoding="utf-8") as handle:
        parser.write(handle)
    print(f"\nSaved configuration to {config_path}")
