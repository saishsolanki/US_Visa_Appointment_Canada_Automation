# 🔒 Security and Privacy Guide

## Important Security Notes

### Before Running the Script
1. **Copy the template**: `cp config.ini.template config.ini`
2. **Never commit config.ini**: The `.gitignore` file prevents this, but double-check
3. **Use Gmail App Password**: Never use your main Gmail password
4. **Verify your .gitignore**: Ensure personal data is excluded from version control

### Files That Contain Personal Data
These files should NEVER be shared or committed to version control:
- `config.ini` - Contains your AIS credentials and Gmail app password
- `visa_checker.log*` - Contains execution logs with email attempts
- `artifacts/` - Contains website screenshots that may show personal info
- `logs/` - Contains detailed execution logs

### Safe Files for Sharing
These files are safe to share and commit:
- `config.ini.template` - Template with placeholder values
- `.env.performance` - Only contains browser optimization settings
- All `.py` files - No hardcoded credentials
- All `.md` files - Documentation only
- All installation scripts - Use placeholder values

### Before Pushing to GitHub
1. Verify no personal data: `grep -r "your_actual_email\|your_actual_password" .`
2. Check git status: `git status` (should not show config.ini)
3. Review files to be committed: `git diff --cached`

### Gmail App Password Setup
1. Enable 2FA on your Gmail account
2. Generate an App Password specifically for this application
3. Use the 16-character app password (not your regular Gmail password)
4. See [`GMAIL_SETUP_GUIDE.md`](GMAIL_SETUP_GUIDE.md) for detailed instructions

### Environment Variables (Advanced)
For extra security, you can use environment variables instead of config.ini:
```bash
export EMAIL="your_email@domain.com"
export PASSWORD="your_ais_password"
export SMTP_USER="your_gmail@gmail.com"
export SMTP_PASS="your_app_password"
# ... etc
```

The application will use environment variables if they exist, falling back to config.ini values.

## 🚨 What to Do If You Accidentally Exposed Credentials

1. **Gmail App Password**: Revoke it immediately at https://myaccount.google.com/apppasswords
2. **AIS Password**: Change it on the US Visa website
3. **Git History**: If committed to Git, rewrite history or create a new repo
4. **Email Logs**: Clear any email logs that might contain failed authentication attempts

## Safe Repository Practices

### .gitignore Protection
The included `.gitignore` file protects:
- `config.ini` - Your actual configuration
- `*.log` files - Runtime logs
- `artifacts/` - Screenshot captures
- `logs/` - Detailed execution logs
- `__pycache__/` - Python cache files

### Template-Based Configuration
- `config.ini.template` contains safe placeholder values
- Copy to `config.ini` and customize with your real data
- `config.ini` is gitignored, so your real data stays local

This approach allows safe sharing while protecting your personal information.