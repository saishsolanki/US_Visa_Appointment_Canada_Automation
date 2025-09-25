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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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
    msg = MIMEText(message)
    msg['Subject'] = subject
    msg['From'] = EMAIL
    msg['To'] = NOTIFY_EMAIL

    server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(EMAIL, NOTIFY_EMAIL, msg.as_string())
    server.quit()

def check_appointments():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(ChromeDriverManager().install(), options=options)

    try:
        driver.get("https://ais.usvisa-info.com/en-ca/niv/users/sign_in")

        # Wait for email field
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "user_email")))
        email_field = driver.find_element(By.ID, "user_email")
        email_field.send_keys(EMAIL)

        password_field = driver.find_element(By.ID, "user_password")
        password_field.send_keys(PASSWORD)

        # For captcha, this is tricky. You may need to solve it manually or use a service like 2captcha
        # For now, assume no captcha or handle manually
        sign_in_button = driver.find_element(By.NAME, "commit")
        sign_in_button.click()

        # Wait for login
        WebDriverWait(driver, 10).until(EC.url_contains("dashboard"))

        # Navigate to reschedule
        driver.get("https://ais.usvisa-info.com/en-ca/niv/schedule/")  # adjust URL

        # Select location
        # This depends on the page structure
        # Assume there's a dropdown for location
        location_select = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "location")))
        # Select option by visible text
        from selenium.webdriver.support.ui import Select
        select = Select(location_select)
        select.select_by_visible_text(LOCATION)

        # Now, check available dates
        # The calendar might be dynamic
        # Find the date picker
        date_picker = driver.find_element(By.ID, "date")
        date_picker.click()

        # Get available dates - this is simplified
        available_dates = driver.find_elements(By.CLASS_NAME, "available-date")  # adjust class

        current_date = datetime.strptime(CURRENT_APPOINTMENT_DATE, "%Y-%m-%d")
        start_date = datetime.strptime(START_DATE, "%Y-%m-%d")
        end_date = datetime.strptime(END_DATE, "%Y-%m-%d")

        for date_elem in available_dates:
            date_str = date_elem.get_attribute("data-date")  # adjust
            date = datetime.strptime(date_str, "%Y-%m-%d")
            if start_date <= date < current_date:
                # Found earlier date
                send_notification("Earlier Visa Appointment Available", f"Available date: {date_str}")
                # Optionally, book it
                date_elem.click()
                # Then submit
                submit_button = driver.find_element(By.ID, "submit")
                submit_button.click()
                break

    except Exception as e:
        print(f"Error: {e}")
        send_notification("Script Error", str(e))

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