#!/bin/bash
# Installation script for Debian
# US Visa Appointment Checker

echo "US Visa Appointment Checker - Debian Installation"
echo "================================================="

# Check if running on Debian
if ! grep -q "Debian" /etc/os-release; then
    echo "This script is designed for Debian. Please use the appropriate script for your distribution."
    exit 1
fi

# Update package list
echo "Updating package list..."
sudo apt update

# Install Python3 and pip if not present
echo "Checking Python3..."
if ! command -v python3 &> /dev/null; then
    echo "Installing Python3..."
    sudo apt install -y python3
else
    echo "Python3 already installed"
fi

# Install pip if not present
if ! command -v pip3 &> /dev/null; then
    echo "Installing pip3..."
    sudo apt install -y python3-pip
else
    echo "pip3 already installed"
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install selenium webdriver-manager flask

# Create default configuration
echo "Creating default configuration..."
cat > config.ini << 'EOF'
[DEFAULT]
EMAIL = your_email@gmail.com
PASSWORD = your_password
CURRENT_APPOINTMENT_DATE = 2025-12-01
LOCATION = Ottawa - U.S. Embassy
START_DATE = 2025-09-25
END_DATE = 2025-12-31
CHECK_FREQUENCY_MINUTES = 5
SMTP_SERVER = smtp.gmail.com
SMTP_PORT = 587
SMTP_USER = your_email@gmail.com
SMTP_PASS = your_app_password
NOTIFY_EMAIL = your_email@gmail.com
AUTO_BOOK = False
EOF

echo ""
echo "=================================================="
echo "Installation completed successfully!"
echo ""
echo "Next steps:"
echo "1. Edit config.ini with your actual credentials and settings"
echo "2. For Gmail SMTP, create an app password at:"
echo "   https://myaccount.google.com/apppasswords"
echo "3. Run the web UI: python3 web_ui.py"
echo "4. Configure settings at http://127.0.0.1:5000"
echo "5. Run the checker: python3 visa_appointment_checker.py"
echo ""
echo "Note: Gmail SMTP is used as the default (free service)"