#!/usr/bin/env python3
"""
Installation script for US Visa Appointment Checker
This script installs all necessary dependencies and creates default configuration.
"""

import subprocess
import sys
import os
import configparser

def run_command(command, description):
    """Run a command and print status"""
    print(f"Installing {description}...")
    try:
        subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        print(f"✓ {description} installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to install {description}: {e}")
        print(f"Error output: {e.stderr}")
        return False

def install_dependencies():
    """Install Python dependencies"""
    print("Installing Python dependencies...")

    dependencies = [
        "selenium",
        "webdriver-manager",
        "flask"
    ]

    success = True
    for dep in dependencies:
        if not run_command(f"pip install {dep}", dep):
            success = False

    return success

def create_default_config():
    """Create default configuration file"""
    print("Creating default configuration...")

    config = configparser.ConfigParser()

    # Default configuration with Gmail SMTP (free and commonly available)
    config['DEFAULT'] = {
        'EMAIL': 'your_email@gmail.com',
        'PASSWORD': 'your_password',
        'CURRENT_APPOINTMENT_DATE': '2025-12-01',
        'LOCATION': 'Ottawa - U.S. Embassy',
        'START_DATE': '2025-09-25',
        'END_DATE': '2025-12-31',
        'CHECK_FREQUENCY_MINUTES': '5',
        'SMTP_SERVER': 'smtp.gmail.com',
        'SMTP_PORT': '587',
        'SMTP_USER': 'your_email@gmail.com',
        'SMTP_PASS': 'your_app_password',
        'NOTIFY_EMAIL': 'your_email@gmail.com',
        'AUTO_BOOK': 'False'
    }

    try:
        with open('config.ini', 'w') as f:
            config.write(f)
        print("✓ Default configuration created (config.ini)")
        return True
    except Exception as e:
        print(f"✗ Failed to create config: {e}")
        return False

def main():
    """Main installation function"""
    print("US Visa Appointment Checker - Installation")
    print("=" * 50)

    # Check Python version
    if sys.version_info < (3, 8):
        print("✗ Python 3.8 or higher is required")
        sys.exit(1)

    print(f"✓ Python {sys.version.split()[0]} detected")

    # Install dependencies
    if not install_dependencies():
        print("✗ Some dependencies failed to install. Please check the errors above.")
        sys.exit(1)

    # Create default config
    if not create_default_config():
        print("✗ Failed to create default configuration.")
        sys.exit(1)

    print("\n" + "=" * 50)
    print("Installation completed successfully!")
    print("\nNext steps:")
    print("1. Edit config.ini with your actual credentials and settings")
    print("2. For Gmail SMTP, create an app password at:")
    print("   https://myaccount.google.com/apppasswords")
    print("3. Run the web UI: python web_ui.py")
    print("4. Configure settings at http://127.0.0.1:5000")
    print("5. Run the checker: python visa_appointment_checker.py")
    print("\\nNote: Gmail SMTP is used as the default (free service)")
    print("You can change to another email provider if preferred.")


if __name__ == "__main__":
    main()