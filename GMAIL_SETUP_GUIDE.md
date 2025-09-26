# Gmail SMTP Setup Guide

## Overview
This guide helps you configure Gmail SMTP for the US Visa Appointment Checker. The system supports Gmail notifications across all supported platforms: Windows, Ubuntu, Debian, Fedora, Arch Linux, and Kali Linux.

## Current Issue
The script shows this error:
```
SMTP authentication failed: (535, b'5.7.8 Username and Password not accepted')
```

## Solution Steps

### 1. Enable 2-Factor Authentication
1. Go to https://myaccount.google.com/security
2. Sign in with your Gmail account
3. Under "How you sign in to Google", click on "2-Step Verification"
4. Follow the prompts to enable 2FA using your phone

### 2. Generate App Password
Since 2FA is already enabled, you need to access App passwords:

**Method 1: Direct Link**
1. Go directly to: https://myaccount.google.com/apppasswords
2. If you don't see this option, try Method 2 below

**Method 2: Through Security Settings**
1. Go to https://myaccount.google.com/security
2. Click on "2-Step Verification" (where it shows "On since 9:47 PM")
3. Scroll down in the 2-Step Verification page
4. Look for "App passwords" section at the bottom
5. Click "App passwords"

**Method 3: Search in Account Settings**
1. Go to https://myaccount.google.com/
2. Use the search box at the top and type "app passwords"
3. Click on the App passwords result

**Once you find App passwords:**
1. Click "Select app" → Choose "Mail" 
2. Click "Select device" → Choose "Other (custom name)"
3. Enter "Visa Appointment Checker" as the name
4. Click "Generate"
5. **Copy the 16-character app password** (it looks like: `abcd efgh ijkl mnop`)

**Note:** If none of these methods work, Google may have disabled app passwords for your account type. In that case, use Method 4 below.

### 3. Update Configuration
Replace the current password in `config.ini`:
```ini
smtp_pass = [PASTE YOUR 16-CHARACTER APP PASSWORD HERE]
```

### 4. Test Email

#### Windows Testing
```batch
python -c "from visa_appointment_checker import CheckerConfig, send_notification; cfg = CheckerConfig.load(); result = send_notification(cfg, 'Test Email', 'SMTP setup is working!'); print('Email sent successfully!' if result else 'Email failed - check credentials')"
```

#### Linux Testing
```bash
./visa_env/bin/python -c "
from visa_appointment_checker import CheckerConfig, send_notification
cfg = CheckerConfig.load()
result = send_notification(cfg, 'Test Email', 'SMTP setup is working!')
print('Email sent successfully!' if result else 'Email failed - check credentials')
"
```

#### Alternative Cross-Platform Test
You can also test by running the main checker for a few seconds and watching for email notifications in the logs:
```bash
# Linux
timeout 30s ./run_visa_checker.sh --frequency 5

# Windows
timeout 30 python visa_appointment_checker.py --frequency 5
```

## Security Notes
- App passwords are more secure than using your main Gmail password
- Each app password is unique and can be revoked individually
- The app password bypasses 2FA for this specific application

### Method 4: OAuth2 Alternative (Advanced)
If App passwords are not available, you can use OAuth2 authentication:
1. Go to https://console.developers.google.com/
2. Create a new project or select an existing one
3. Enable the Gmail API
4. Create credentials (OAuth2 client ID)
5. Download the credentials JSON file
6. This requires code modifications - contact support if needed

## Alternative: Disable Gmail and Use Console Logs Only
If you prefer not to set up Gmail, you can disable email notifications by setting placeholder credentials in config.ini:
```ini
smtp_user = disabled@example.com  
smtp_pass = disabled_password_here
```
The script will detect invalid credentials and skip email sending while continuing to log everything to the console and log files.

## Performance Considerations
The optimized system includes performance improvements for email notifications:
- **Faster SMTP connections** with connection reuse
- **Retry logic** with intelligent backoff for temporary email failures
- **Performance tracking** of email sending times
- **Memory efficient** email handling without resource leaks

Email sending times are included in performance metrics:
```
Performance stats [notification]: avg=1.2s, last=0.8s
```

## Cross-Platform Compatibility
This Gmail setup works identically across all supported platforms:
- ✅ **Windows 10/11** - Full support with python.exe and batch scripts
- ✅ **Ubuntu 20.04+** - Native support with virtual environment
- ✅ **Debian 10+** - Full compatibility with apt package management  
- ✅ **Fedora 35+** - Complete support with dnf package management
- ✅ **Arch Linux** - Full support with pacman package management
- ✅ **Kali Linux** - Complete compatibility with specialized security tools

The virtual environment setup ensures consistent behavior across all Linux distributions.

## Quick Troubleshooting
**Can't find App passwords?** Try these direct links:
- https://myaccount.google.com/apppasswords
- https://security.google.com/settings/security/apppasswords  
- https://myaccount.google.com/u/0/apppasswords

**Still can't access?** 
1. Make sure you're signed into the correct Gmail account (your configured Gmail address)
2. Some Google Workspace accounts have App passwords disabled by admin
3. Try using a different browser or incognito mode
4. Check if your account has "Less secure app access" restrictions