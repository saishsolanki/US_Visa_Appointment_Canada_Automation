# 🔒 Security and Privacy Guide

## Quick Start

1. Copy template: `cp config.ini.template config.ini`
2. Edit with your details
3. **Never** commit `config.ini` (already in `.gitignore`)
4. Use Gmail App Password (not your main password) — see [GMAIL_SETUP_GUIDE.md](GMAIL_SETUP_GUIDE.md)

---

## What's Private

**Never share or commit:**
- `config.ini` — Contains AIS + Gmail credentials
- `visa_checker.log*` — Contains session logs
- `artifacts/` — Website screenshots
- `logs/` — Detailed logs

**Safe to share:**
- `config.ini.template`, `*.py`, `*.md`, all `.sh` files, `.gitignore`

---

## Before Pushing to GitHub

```bash
# Verify no credentials leaked
grep -r "your_actual_email\|password" .

# Check what you're about to commit
git status
git diff --cached
```

---

## If Credentials Are Accidentally Exposed

1. **Gmail**: Revoke app password at https://myaccount.google.com/apppasswords
2. **AIS**: Change password on usvisa-info.com
3. **Git**: Rewrite history or create a new repo if committed

---

## Using Environment Variables (Advanced)

For extra security, use environment variables instead of config.ini:

```bash
export EMAIL="your_ais_email@domain.com"
export PASSWORD="your_ais_password"
export SMTP_USER="your_gmail@gmail.com"
export SMTP_PASS="your_app_password"
```

The app uses environment variables if available, falling back to `config.ini`.

---

## Protection Built-In

✅ `.gitignore` protects `config.ini`, `*.log`, `artifacts/`, `logs/`  
✅ Template-based config keeps sensitive data local  
✅ All credentials use app-password or environment-variable patterns  
✅ Safe to clone and share the repo without worrying about exposing data