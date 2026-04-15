import json
import logging
import smtplib
import urllib.error
import urllib.parse
import urllib.request
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


def send_telegram_notification(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a Telegram push notification via Bot API (<1s delivery)."""
    if not bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logging.info("Telegram notification sent successfully")
                return True
            logging.warning("Telegram API returned status %d", resp.status)
    except urllib.error.HTTPError as exc:
        logging.warning("Telegram notification failed (HTTP %d): %s", exc.code, exc.reason)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Telegram notification failed: %s", exc)

    return False


def send_webhook_notification(webhook_url: str, subject: str, message: str) -> bool:
    """Send a notification to a generic webhook URL (Discord, Slack, custom)."""
    if not webhook_url:
        return False

    lower_url = webhook_url.lower()
    if "discord.com/api/webhooks" in lower_url:
        payload = json.dumps({"content": f"**{subject}**\n{message}"[:2000]}).encode("utf-8")
    elif "hooks.slack.com" in lower_url:
        payload = json.dumps({"text": f"*{subject}*\n{message}"}).encode("utf-8")
    else:
        payload = json.dumps({"subject": subject, "message": message}).encode("utf-8")

    req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                logging.info("Webhook notification sent successfully")
                return True
            logging.warning("Webhook returned status %d", resp.status)
    except urllib.error.HTTPError as exc:
        logging.warning("Webhook notification failed (HTTP %d): %s", exc.code, exc.reason)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Webhook notification failed: %s", exc)

    return False


def send_pushover_notification(app_token: str, user_key: str, subject: str, message: str) -> bool:
    """Send an instant mobile push notification via Pushover."""
    if not app_token or not user_key:
        return False

    payload = urllib.parse.urlencode({
        "token": app_token,
        "user": user_key,
        "title": subject[:250],
        "message": message[:1024],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logging.info("Pushover notification sent successfully")
                return True
            logging.warning("Pushover API returned status %d", resp.status)
    except urllib.error.HTTPError as exc:
        logging.warning("Pushover notification failed (HTTP %d): %s", exc.code, exc.reason)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Pushover notification failed: %s", exc)

    return False


def send_all_notifications(cfg, subject: str, message: str) -> bool:
    """Send notification via all configured channels."""
    results = []

    # Email
    results.append(send_notification(cfg, subject, message))

    # Telegram
    bot_token = getattr(cfg, "telegram_bot_token", "") or ""
    chat_id = getattr(cfg, "telegram_chat_id", "") or ""
    if bot_token and chat_id:
        results.append(send_telegram_notification(bot_token, chat_id, f"<b>{subject}</b>\n\n{message}"))

    # Webhook
    webhook_url = getattr(cfg, "webhook_url", "") or ""
    if webhook_url:
        results.append(send_webhook_notification(webhook_url, subject, message))

    # Pushover
    pushover_app_token = getattr(cfg, "pushover_app_token", "") or ""
    pushover_user_key = getattr(cfg, "pushover_user_key", "") or ""
    if pushover_app_token and pushover_user_key:
        results.append(send_pushover_notification(pushover_app_token, pushover_user_key, subject, message))

    return any(results)
