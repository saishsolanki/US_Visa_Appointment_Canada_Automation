#!/bin/bash
# Configuration helper script
# US Visa Appointment Checker

echo "US Visa Appointment Checker - Configuration Helper"
echo "=================================================="

# Check if config.ini exists
if [ ! -f "config.ini" ]; then
    echo "Error: config.ini not found. Please run the installer first."
    exit 1
fi

echo "Current configuration:"
echo "---------------------"
cat config.ini
echo ""
echo "You need to replace the placeholder values with your actual information."
echo ""

# Interactive configuration
read -p "Enter your AIS email: " ais_email
read -p "Enter your AIS password: " ais_password
read -p "Enter your current appointment date (YYYY-MM-DD): " current_date
read -p "Enter your location (e.g., 'Ottawa - U.S. Embassy'): " location
read -p "Enter start date for appointments (YYYY-MM-DD): " start_date
read -p "Enter end date for appointments (YYYY-MM-DD): " end_date
read -p "Enter check frequency in minutes (default 5): " frequency
frequency=${frequency:-5}
read -p "Enter your Gmail address for notifications: " gmail_user
read -p "Enter your Gmail app password: " gmail_pass

# Update config.ini
cat > config.ini << EOF
[DEFAULT]
EMAIL = $ais_email
PASSWORD = $ais_password
CURRENT_APPOINTMENT_DATE = $current_date
LOCATION = $location
START_DATE = $start_date
END_DATE = $end_date
CHECK_FREQUENCY_MINUTES = $frequency
SMTP_SERVER = smtp.gmail.com
SMTP_PORT = 587
SMTP_USER = $gmail_user
SMTP_PASS = $gmail_pass
NOTIFY_EMAIL = $gmail_user
AUTO_BOOK = False
EOF

echo ""
echo "Configuration updated successfully!"
echo ""
echo "Important reminders:"
echo "- Make sure 2-factor authentication is enabled on your Gmail account"
echo "- Generate an app password at: https://myaccount.google.com/apppasswords"
echo "- Test the script with: ./run_visa_checker.sh --frequency 60 (longer interval for testing)"
echo ""
echo "To get Gmail app password:"
echo "1. Go to https://myaccount.google.com/apppasswords"
echo "2. Sign in with your Gmail account"
echo "3. Select 'Mail' and 'Other (custom name)'"
echo "4. Enter 'Visa Checker' as the name"
echo "5. Copy the generated password and use it above"