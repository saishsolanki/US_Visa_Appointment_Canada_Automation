import os

from selenium.webdriver.chrome.options import Options


def build_chrome_options(*, headless: bool) -> Options:
    options = Options()
    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")

    minimal_browser = os.getenv("MINIMAL_BROWSER", "true").lower() == "true"
    if minimal_browser:
        options.add_argument("--disable-images")
        options.add_argument("--disable-plugins")
        options.add_argument("--disable-java")
        options.add_argument("--disable-web-security")
        options.add_argument("--no-proxy-server")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")

    options.add_argument("--memory-pressure-off")
    options.add_argument("--max_old_space_size=4096")

    prefs = {
        "profile.default_content_setting_values": {
            "images": 2 if minimal_browser else 0,
            "plugins": 2,
            "popups": 2,
            "geolocation": 2,
            "notifications": 2,
            "media_stream": 2,
        }
    }
    options.add_experimental_option("prefs", prefs)

    user_agent = os.getenv(
        "CHECKER_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    options.add_argument(f"--user-agent={user_agent}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return options
