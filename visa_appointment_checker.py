import argparse
import time
import smtplib
import configparser
import logging
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

EMAIL = config['DEFAULT']['EMAIL']
PASSWORD = config['DEFAULT']['PASSWORD']
CURRENT_APPOINTMENT_DATE = config['DEFAULT']['CURRENT_APPOINTMENT_DATE']
LOCATION = config['DEFAULT']['LOCATION']
START_DATE = config['DEFAULT']['START_DATE']
END_DATE = config['DEFAULT']['END_DATE']
CHECK_FREQUENCY_MINUTES = int(config['DEFAULT']['CHECK_FREQUENCY_MINUTES'])
SMTP_SERVER = config['DEFAULT']['SMTP_SERVER']
SMTP_PORT = int(config['DEFAULT']['SMTP_PORT'])
SMTP_USER = config['DEFAULT']['SMTP_USER']
SMTP_PASS = config['DEFAULT']['SMTP_PASS']
NOTIFY_EMAIL = config['DEFAULT']['NOTIFY_EMAIL']
AUTO_BOOK = config.getboolean('DEFAULT', 'AUTO_BOOK')

logging.basicConfig(filename='visa_checker.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def send_notification(subject, message):
    """Send email notification with error handling"""
    try:
        msg = MIMEText(message)
        msg['Subject'] = subject
        msg['From'] = SMTP_USER
        msg['To'] = NOTIFY_EMAIL

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        server.quit()
        logging.info("Email notification sent successfully")
    except smtplib.SMTPAuthenticationError as e:
        logging.error(f"SMTP Authentication failed: {e}")
        logging.error("Please check your Gmail credentials and app password")
    except Exception as e:
        logging.error(f"Failed to send email notification: {e}")

def check_appointments():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        logging.info("Starting appointment check")
        driver.get("https://ais.usvisa-info.com/en-ca/niv/users/sign_in")
        logging.info("Page loaded, waiting for elements")

        # Wait for page to load and check for common elements
        WebDriverWait(driver, 20).until(
            lambda driver: driver.execute_script("return document.readyState") == "complete"
        )
        logging.info("Page ready")

        # Try different selectors for email field
        email_selectors = [
            (By.ID, "user_email"),
            (By.NAME, "user[email]"),
            (By.CSS_SELECTOR, "input[type='email']"),
            (By.CSS_SELECTOR, "input[placeholder*='email']")
        ]

        email_field = None
        for by, selector in email_selectors:
            try:
                email_field = WebDriverWait(driver, 5).until(EC.presence_of_element_located((by, selector)))
                logging.info(f"Found email field with selector: {by}={selector}")
                break
            except:
                continue

        if not email_field:
            logging.error("Could not find email field on login page")
            raise Exception("Email field not found - website may have changed")

        email_field.clear()
        email_field.send_keys(EMAIL)

        # Try different selectors for password field
        password_selectors = [
            (By.ID, "user_password"),
            (By.NAME, "user[password]"),
            (By.CSS_SELECTOR, "input[type='password']")
        ]

        password_field = None
        for by, selector in password_selectors:
            try:
                password_field = WebDriverWait(driver, 5).until(EC.presence_of_element_located((by, selector)))
                logging.info(f"Found password field with selector: {by}={selector}")
                break
            except:
                continue

        if not password_field:
            logging.error("Could not find password field on login page")
            raise Exception("Password field not found - website may have changed")

        password_field.clear()
        password_field.send_keys(PASSWORD)

        # Try different selectors for sign in button
        button_selectors = [
            (By.NAME, "commit"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//input[@value='Sign In']"),
            (By.XPATH, "//button[contains(text(), 'Sign In')]")
        ]

        sign_in_button = None
        for by, selector in button_selectors:
            try:
                sign_in_button = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, selector)))
                logging.info(f"Found sign in button with selector: {by}={selector}")
                break
            except:
                continue

        if not sign_in_button:
            logging.error("Could not find sign in button on login page")
            raise Exception("Sign in button not found - website may have changed")

        sign_in_button.click()
        logging.info("Clicked sign in button")

        # Wait for login to complete - check for dashboard or error
        try:
            WebDriverWait(driver, 30).until(
                lambda driver: "dashboard" in driver.current_url or
                              "sign_in" not in driver.current_url or
                              EC.presence_of_element_located((By.CLASS_NAME, "alert"))(driver)
            )
            logging.info("Login process completed")
        except:
            logging.warning("Login timeout - may have succeeded or failed")

        # Check if login was successful
        if "dashboard" in driver.current_url:
            logging.info("Successfully logged in to dashboard")
        elif "sign_in" in driver.current_url:
            logging.error("Still on sign in page - login may have failed")
            raise Exception("Login failed - check credentials or CAPTCHA")
        else:
            logging.info(f"Redirected to: {driver.current_url}")

        # Navigate to reschedule - try different URLs
        reschedule_urls = [
            "https://ais.usvisa-info.com/en-ca/niv/schedule/",
            "https://ais.usvisa-info.com/en-ca/niv/appointment",
            "https://ais.usvisa-info.com/en-ca/niv/"
        ]

        for url in reschedule_urls:
            try:
                driver.get(url)
                WebDriverWait(driver, 10).until(
                    lambda driver: driver.execute_script("return document.readyState") == "complete"
                )
                logging.info(f"Navigated to: {url}")
                break
            except:
                continue

        # Try to find location selector
        location_selectors = [
            (By.ID, "location"),
            (By.NAME, "location"),
            (By.CSS_SELECTOR, "select[name*='location']")
        ]

        location_select = None
        for by, selector in location_selectors:
            try:
                location_select = WebDriverWait(driver, 5).until(EC.presence_of_element_located((by, selector)))
                logging.info(f"Found location selector with: {by}={selector}")
                break
            except:
                continue

        if location_select:
            select = Select(location_select)
            try:
                select.select_by_visible_text(LOCATION)
                logging.info(f"Selected location: {LOCATION}")
            except:
                logging.warning(f"Could not select location: {LOCATION}")
        else:
            logging.warning("Location selector not found")

        # For now, just log that we reached this point
        logging.info("Appointment check completed - website structure may need updating")

    except Exception as e:
        logging.error(f"Error during appointment check: {e}")
        # Only try to send notification if SMTP is configured
        if SMTP_USER != "your_email@gmail.com" and SMTP_PASS != "your_app_password":
            try:
                send_notification("Script Error", str(e))
            except Exception as email_error:
                logging.error(f"Failed to send error notification: {email_error}")
        else:
            logging.info("Skipping email notification - SMTP not configured")

    finally:
        driver.quit()

def main():
    parser = argparse.ArgumentParser(description="US Visa Appointment Checker")
    parser.add_argument("--frequency", type=int, default=CHECK_FREQUENCY_MINUTES, help="Check frequency in minutes")
    args = parser.parse_args()

    while True:
        check_appointments()
        time.sleep(args.frequency * 60)

if __name__ == "__main__":
    main()