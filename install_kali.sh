#!/bin/bash
# Installation script for Kali Linux
# US Visa Appointment Checker

echo "US Visa Appointment Checker - Kali Linux Installation"
echo "====================================================="

# Check if running on Kali Linux
if ! grep -q "Kali" /etc/os-release; then
    echo "This script is designed for Kali Linux. Please use the appropriate script for your distribution."
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

# Install python3-venv for virtual environment
echo "Installing python3-venv..."
sudo apt install -y python3-venv

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv visa_env

# Activate virtual environment and install dependencies
echo "Installing Python dependencies in virtual environment..."
source visa_env/bin/activate
pip install selenium webdriver-manager flask

# Create a wrapper script to run with virtual environment
echo "Creating wrapper script..."
cat > run_visa_checker.sh << 'EOF'
#!/bin/bash
# Wrapper script to run visa checker with virtual environment

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Activate virtual environment
source "$DIR/visa_env/bin/activate"

# Run the visa checker with all arguments
python "$DIR/visa_appointment_checker.py" "$@"

# Deactivate virtual environment
deactivate
EOF

chmod +x run_visa_checker.sh

# Create wrapper for web UI
cat > run_web_ui.sh << 'EOF'
#!/bin/bash
# Wrapper script to run web UI with virtual environment

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Activate virtual environment
source "$DIR/visa_env/bin/activate"

# Run the web UI
python "$DIR/web_ui.py"

# Deactivate virtual environment
deactivate
EOF

chmod +x run_web_ui.sh

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
echo "====================================================="
echo "Installation completed successfully!"
echo ""
echo "Virtual environment created: visa_env/"
echo "Wrapper scripts created: run_visa_checker.sh, run_web_ui.sh"
echo ""
echo "Next steps:"
echo "1. Edit config.ini with your actual credentials and settings"
echo "2. For Gmail SMTP, create an app password at:"
echo "   https://myaccount.google.com/apppasswords"
echo "3. Run the web UI: ./run_web_ui.sh"
echo "4. Configure settings at http://127.0.0.1:5000"
echo "5. Run the checker: ./run_visa_checker.sh --frequency 10"
echo ""
echo "Note: Always use the wrapper scripts to run the application"
echo "Note: Gmail SMTP is used as the default (free service)"