import logging
import smtplib
from email.mime.text import MIMEText


def send_notification(cfg, subject: str, message: str) -> bool:
    if not cfg.is_smtp_configured():
        logging.info("Skipping email notification - SMTP not fully configured.")
        return False

    try:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = cfg.smtp_user
        msg["To"] = cfg.notify_email

        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port) as server:
            server.starttls()
            server.login(cfg.smtp_user, cfg.smtp_pass)
            server.sendmail(cfg.smtp_user, cfg.notify_email, msg.as_string())

        logging.info("Email notification sent successfully")
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logging.error("SMTP authentication failed: %s", exc)
        guidance = "Please verify your SMTP username and password/app key."
        if "gmail" in str(getattr(cfg, "smtp_server", "")).lower():
            guidance += " For Gmail, use an App Password and enable 2FA."
        logging.error(
            guidance
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to send email notification: %s", exc)

    return False
