# US Visa Appointment Canada Automation

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

This comprehensive automation tool helps Canadian users find earlier US visa appointment dates by continuously monitoring the official US Visa Information Service (AIS) website. Built with Python, Selenium, and a user-friendly web interface.

## 🚀 Features

- **Automated Appointment Monitoring**: Continuously checks for earlier appointment availability
- **Configurable Check Frequency**: Set custom intervals (recommended: 5-15 minutes)
- **Date Range Filtering**: Only accepts appointments within your preferred date range
- **Location-Specific**: Monitors specific embassy/consulate locations
- **Email Notifications**: Instant alerts via Gmail SMTP (free)
- **Optional Auto-Booking**: Automatically book found appointments (use with caution)
- **Headless Operation**: Runs in background without interfering with your work
- **Comprehensive Logging**: Detailed logs for monitoring and debugging
- **Web UI Configuration**: User-friendly web interface for easy setup
- **Cross-Platform**: Works on Windows, Ubuntu, Debian, Fedora, Arch Linux, and Kali Linux
- **Open Source**: Completely free with no hidden costs

## 📋 Requirements

- **Python**: 3.8 or higher
- **Internet Connection**: Stable connection for website monitoring
- **US Visa Account**: Valid AIS account credentials
- **Email Account**: Gmail account for notifications (or configure alternative SMTP)

## 📁 Project Structure

```
US_Visa_Appointment_Canada_Automation/
├── visa_appointment_checker.py    # Main automation script
├── web_ui.py                      # Web interface for configuration
├── install.py                     # Windows installation script
├── install.bat                    # Windows batch installer
├── install_ubuntu.sh             # Ubuntu installation script
├── install_debian.sh             # Debian installation script
├── install_fedora.sh             # Fedora installation script
├── install_arch.sh               # Arch Linux installation script
├── install_kali.sh               # Kali Linux installation script
├── config.ini                    # Configuration file (created by installer)
├── visa_checker.log              # Application logs
├── templates/
│   └── index.html                # Web UI template
├── run.bat                       # Windows runner script
├── README.md                     # This file
└── LICENSE                       # MIT License
```

## 🛠️ Installation

### Windows
```bash
# Option 1: Python script
python install.py

# Option 2: Batch file
install.bat
```

### Linux
Choose the script for your distribution:

**Ubuntu:**
```bash
chmod +x install_ubuntu.sh && ./install_ubuntu.sh
```

**Debian:**
```bash
chmod +x install_debian.sh && ./install_debian.sh
```

**Fedora:**
```bash
chmod +x install_fedora.sh && ./install_fedora.sh
```

**Arch Linux:**
```bash
chmod +x install_arch.sh && ./install_arch.sh
```

**Kali Linux:**
```bash
chmod +x install_kali.sh && ./install_kali.sh
```

### What the Installer Does
- ✅ Installs Python 3.8+ and pip (if not present)
- ✅ Installs required dependencies: `selenium`, `webdriver-manager`, `flask`
- ✅ Creates default `config.ini` with Gmail SMTP configuration
- ✅ Provides setup instructions

## ⚙️ Configuration

### Method 1: Web UI (Recommended)
```bash
python web_ui.py
```
Then open http://127.0.0.1:5000 in your browser.

### Method 2: Manual Configuration
Edit `config.ini` with your details:

```ini
[DEFAULT]
EMAIL = your_ais_email@example.com
PASSWORD = your_ais_password
CURRENT_APPOINTMENT_DATE = 2025-12-01
LOCATION = Ottawa - U.S. Embassy
START_DATE = 2025-09-25
END_DATE = 2025-12-31
CHECK_FREQUENCY_MINUTES = 5
SMTP_SERVER = smtp.gmail.com
SMTP_PORT = 587
SMTP_USER = your_email@gmail.com
SMTP_PASS = your_gmail_app_password
NOTIFY_EMAIL = your_email@gmail.com
AUTO_BOOK = False
```

### Gmail SMTP Setup
1. Go to [Google Account Settings](https://myaccount.google.com/apppasswords)
2. Generate an "App Password" for this application
3. Use your Gmail address as `SMTP_USER`
4. Use the generated app password as `SMTP_PASS`

## 🚀 Usage

### Start the Appointment Checker
```bash
# Default frequency (5 minutes)
python visa_appointment_checker.py

# Custom frequency (10 minutes)
python visa_appointment_checker.py --frequency 10

# Windows batch file
run.bat
```

### Web UI for Configuration
```bash
python web_ui.py
```
Access at: http://127.0.0.1:5000

### Command Line Options
- `--frequency`: Check interval in minutes (default: 5)

## 📊 Monitoring

### Logs
Check `visa_checker.log` for detailed execution information:
```bash
tail -f visa_checker.log
```

### Log Levels
- **INFO**: Normal operations and appointment checks
- **WARNING**: Non-critical issues
- **ERROR**: Critical errors requiring attention

## ⚠️ Important Notes

### CAPTCHA Handling
The AIS website uses hCaptcha. To handle this:
- **Recommended**: Use a residential VPN to reduce CAPTCHA frequency
- **Alternative**: Use CAPTCHA solving services (paid)
- **Manual**: Run non-headless and solve CAPTCHAs manually (not automated)

### Legal & Ethical Considerations
- ⚖️ **Terms of Service**: Automated access may violate AIS terms
- 🔒 **Use Responsibly**: Don't overload the servers
- 📧 **Rate Limiting**: Respect the check frequency settings
- 🚫 **Auto-Booking Risk**: Test thoroughly before enabling auto-booking

### Security Best Practices
- Use app passwords, not your main Gmail password
- Store credentials securely
- Don't share your configuration files
- Use VPN for additional privacy

## 🔧 Troubleshooting

### Common Issues

**"Chrome driver not found"**
```bash
pip install webdriver-manager
# Restart the script
```

**"SMTP Authentication failed"**
- Verify Gmail app password is correct
- Check Gmail account settings
- Ensure less secure apps are allowed (for older accounts)

**"Login failed"**
- Verify AIS credentials
- Check if account is locked
- Try logging in manually on the website

**"No available dates found"**
- Check your location selection
- Verify date range settings
- Confirm current appointment exists

**"Permission denied" on Linux**
```bash
chmod +x install_*.sh
```

### Getting Help
1. Check the logs: `cat visa_checker.log`
2. Verify configuration: `cat config.ini`
3. Test manual login on AIS website
4. Check internet connection

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes
4. Test thoroughly
5. Submit a pull request

### Development Setup
```bash
git clone https://github.com/yourusername/us-visa-appointment-checker.git
cd us-visa-appointment-checker
pip install -r requirements.txt  # Create this file with dependencies
```

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ⚖️ Disclaimer

This tool is provided for educational purposes only. Users are responsible for complying with the US Visa Information Service terms of service and applicable laws. The authors are not responsible for any consequences arising from the use of this software.

**Use at your own risk. Automated booking may violate website terms of service.**

---

**Made with ❤️**
