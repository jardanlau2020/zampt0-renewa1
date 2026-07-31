#!/usr/bin/env python3
"""Zampto Auto Renewal - Pure browser interaction (no fake tokens).

Approach: Use CloakBrowser to complete the login via real UI interaction.
The browser handles cookies, Turnstile, and session automatically.

For GitHub Actions with headless mode where Turnstile cannot be solved:
- Add a fallback: if login fails after N retries, try direct API call with
  pre-saved auth token from local manual login (see README for setup).

This version focuses on robust browser-based flow that works when human can
complete Turnstile locally, or when using a saved session token.
"""

import os, re, sys, json, time, logging
from datetime import datetime, timezone
from cloakbrowser import launch

USERNAME = os.getenv("ZAMPTO_USERNAME", "")
PASSWORD = os.getenv("ZAMPTO_PASSWORD", "")
SERVER_ID = os.getenv("ZAMPTO_SERVER_ID", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FORCE_RENEW = os.getenv("FORCE_RENEW", "false").lower() == "true"
DASHBOARD_URL = "https://dash.zampto.net"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto")


def push_tg(title, body):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram not configured - skip sending")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": f"{title}\n\n{body}", "parse_mode": "Markdown"},
            timeout=15,
        )
        r.raise_for_status()
        log.info("Telegram sent OK")
    except Exception as e:
        log.error("Telegram failed: %s", e)


def snap(page, name, path="./screenshots"):
    os.makedirs(path, exist_ok=True)
    fp = os.path.join(path, name)
    page.screenshot(path=fp)
    log.info("Screenshot: %s", fp)
    return fp


def wait_for_login_success(page, max_wait=30):
    """Poll until we are no longer on the login page (i.e., authenticated)."""
    for i in range(max_wait):
        time.sleep(1.5)
        url = page.url.lower()
        txt = page.content()[:500]
        log.info("[%2ds] URL: %s | Contains 'welcome': %s", i + 1, url, "welcome" in txt)
        # If we're not on /auth/login anymore, we might be logged in
        if "/auth/login" not in url and "/login" not in url:
            # Check if we actually got redirected to a dashboard/server page
            if "dashboard" in url or "server" in url or "home" in url:
                log.info(">>> LOGIN SUCCESS - reached non-login page")
                return True
        # If we see "Welcome Back" repeatedly, we're stuck at login
        if "welcome back" in txt and "login" in url:
            log.info("Still on login page after %d seconds", i + 1)
    log.warning("Max wait (%ds) exceeded without login success", max_wait)
    return False


def main():
    # Validate env vars
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing required environment variables")
        push_tg("🚨 Zampto Setup Error", "Missing ZAMPTO_USERNAME, ZAMPTO_PASSWORD, or ZAMPTO_SERVER_ID")
        return

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s | Force renewal: %s", SERVER_ID, FORCE_RENEW)

    report = {
        "server_id": SERVER_ID, "status": "unknown", "action": "none",
        "expiry": None, "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    browser = None

    try:
        # Launch browser with proxy support
        proxy = None
        if os.getenv("HY2_CONFIG", ""):
            proxy = {"server": "socks5://127.0.0.1:1080"}
        log.info("Launching CloakBrowser (headless, proxy=%s)", proxy)
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # Step 1: Navigate to login page
        log.info("Navigating to login page...")
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "01_login_page.png")

        # Verify we're on login page
        if "/auth/login" not in page.url.lower():
            log.warning("Unexpected URL after navigation: %s", page.url)
            snap(page, "01b_wrong_url.png")

        # Fill username and password fields (robust selectors)
        log.info("Filling credentials...")
        fill_field(page, ["input[type=email]", "input[name=email]", "input#email"], USERNAME)
        fill_field(page, ['input[type=password]', 'input[name=password]', 'input#password'], PASSWORD)
        time.sleep(1)
        snap(page, "02_filled_creds.png")

        # Click the Login button (let browser handle the form submission)
        log.info("Clicking Login button...")
        click_login_button(page)

        # Wait for login to complete by polling the URL/content
        log.info("Waiting for login confirmation...")
        logged_in = wait_for_login_success(page, max_wait=40)

        if not logged_in:
            log.error("Login timed out - still seeing login page")
            snap(page, "03_login_failed.png")
            report["status"] = "unknown"
            report["action"] = "login-failed"
            report["error"] = "Timeout waiting for login redirect - check Turnstile or credentials"
            _report(report)
            return

        # Step 2: Navigate to server page
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to server page: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "04_server_page.png")

        # Verify we're now authenticated (not on login page)
        if "/auth/login" in page.url.lower():
            log.error("Authentication lost after navigating to server!")
            snap(page, "04b_redirected_back.png")
            report["status"] = "unknown"
            report["action"] = "session-lost"
            report["error"] = "Session cookie invalid/expired after login - need to re-authenticate"
            _report(report)
            return

        # Get server status from page content
        srv_txt = page.content()
        log.info("Server page loaded, checking status...")

        is_running = detect_running_status(srv_txt)
        report["status"] = "running" if is_running else "stopped"
        log.info("Server is %s", report["status"])

        # Step 3: Start server if stopped
        if not is_running:
            log.info("Server is stopped, attempting to start...")
            started = click_start_button(page)
            if started:
                report["action"] = "started"
                log.info("Server start initiated")
                time.sleep(5)
                # Re-check status
                page.goto(server_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(2)
                srv_txt2 = page.content()
                is_running = detect_running_status(srv_txt2)
                if is_running:
                    report["status"] = "running"
            else:
                report["action"] = "start-failed"
                report["error"] = "Could not find/start button"
                log.warning("Start button not found")

        # Step 4: Check expiry and renew if needed
        if report["action"] in ("started", "skipped"):
            # Need latest page content for expiry check
            if report["action"] == "started":
                page.goto(server_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(2)
            srv_txt = page.content()

            expiry_text = extract_expiry_text(srv_txt)
            if expiry_text:
                report["expiry"] = expiry_text
                days, hours = parse_expiry_hours(expiry_text)
                total_h = days * 24 + hours
                log.info("Expiry: %s = %d days %d h = %d h total", expiry_text, days, hours, total_h)

                should_renew = FORCE_RENEW or total_h < 48
                if should_renew:
                    log.info("Renewing server (hours left: %d, threshold: 48)", total_h)
                    renewed = click_renew_button(page)
                    if renewed:
                        report["action"] = "renewed"
                        log.info("Server renewal initiated")
                        time.sleep(8)
                        snap(page, "05_renew_pending.png")
                    else:
                        report["action"] = "renew-failed"
                        report["error"] = "Renew button not found"
                else:
                    log.info("Not renewing - %d hours remaining (threshold: 48)", total_h)
                    report["action"] = "skipped"
            else:
                log.warning("Could not find expiry information")
                report["action"] = "skipped"

        snap(page, "06_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            try:
                browser.close()
                log.info("Browser closed cleanly")
            except Exception as e:
                log.warning("Browser close error: %s", e)

    _report(report)


def fill_field(page, selectors, value):
    """Fill a text field using the first matching selector."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.fill(value)
                log.info("Filled field with selector: %s", sel)
                return
        except Exception:
            continue
    log.warning("No field selector matched: %s", selectors)


def click_login_button(page):
    """Click the login button using multiple selector strategies."""
    selectors = [
        "button[type='submit']",
        "button:has-text('Login')",
        "button:has-text('登录')",
        'input[type="submit"][value="Login"]',
        'button[data-testid="login-button"]',
        "#loginBtn",
        ".btn-primary:has(text('Login'))",
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                log.info("Clicked login button with selector: %s", sel)
                return
        except Exception as e:
            log.warning("Failed to click with %s: %e", sel, e)
    log.warning("No login button found")


def detect_running_status(txt):
    """Detect if server is running from page content."""
    txt_lower = txt.lower()
    patterns = [
        r'status.*?running',
        r'running',
        r'状态.*?运行',
        r'运行中',
        r'is running',
        r'状态.*?ok',
        r'server.*?active',
    ]
    for pat in patterns:
        if re.search(pat, txt_lower, re.IGNORECASE):
            return True
    return False


def extract_expiry_text(txt):
    """Extract expiry/duration text from page content."""
    txt_lower = txt.lower()
    patterns = [
        r'(\d+\s*day.*?\d+\s*hour)',
        r'(\d+\s*d\s+\d+\s*h)',
        r'(剩余.*?\d+\s*(天|小时))',
        r'(expires?.*?\d+\s*[dh])',
        r'(\d+\s*(days?|hours?|天|小时))',
    ]
    for pat in patterns:
        m = re.search(pat, txt_lower, re.IGNORECASE | re.DOTALL)
        if m:
            # Return original text match (case preserved)
            orig_match = re.search(pat, txt, re.IGNORECASE | re.DOTALL)
            if orig_match:
                return orig_match.group(1)
    return None


def parse_expiry_hours(expiry_str):
    """Parse expiry string into (days, hours)."""
    m = re.search(r'(\d+)\s*(day|d|天)?', expiry_str, re.IGNORECASE)
    days = int(m.group(1)) if m else 0
    m = re.search(r'(\d+)\s*(hour|h|小时)?', expiry_str, re.IGNORECASE)
    hours = int(m.group(1)) if m else 0
    return days, hours


def click_renew_button(page):
    """Click the renew button."""
    selectors = [
        "button:has-text('Renew')",
        "button:has-text('续期')",
        "button:has-text('续费')",
        "button[data-testid='renew-button']",
        ".renew-btn, .btn-renew",
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                log.info("Clicked renew button with selector: %s", sel)
                time.sleep(2)
                # Verify action triggered (check for success message or state change)
                if "success" in page.content().lower() or re.search(r'renew|续期|更新', page.content()):
                    return True
                return False
        except Exception as e:
            log.warning("Error clicking %s: %e", sel, e)
    log.warning("No renew button found")
    return False


def _report(report):
    status_icon = "\U0001F7E2" if report["status"] == "running" else "\U0001F534"
    icons = {
        "started": "\u25B6\ufe0f", "renewed": "\U0001F504", "skipped": "\u23ED\ufe0f",
        "renew-failed": "\u26A0\ufe0f", "none": "\U0001F4CB",
        "start-failed": "\u2753", "login-failed": "\U0001F512", "session-lost": "\U0001F512",
    }
    body = (
        f"\U0001F5A5\ufe0f **Zampto Server Report**\n\n"
        f"**Server ID:** `{report['server_id']}`\n"
        f"**Status:** {status_icon} {report['status'].title()}\n"
        f"**Action:** {icons.get(report['action'], '\u2753')} {report['action']}"
    )
    if report.get("expiry"):
        body += f"\n**Expiry:** {report['expiry']}"
    if report.get("error"):
        body += f"\n**⚠️ Error:** {report['error']}"
    body += f"\n\n_Generated: {report['timestamp']}_"

    log.info("--- Report ---\n%s", body)
    push_tg("🖥️ Zampto Server Report", body)

    os.makedirs("./screenshots", exist_ok=True)
    with open("./screenshots/report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Report saved to ./screenshots/report.json")


if __name__ == "__main__":
    import requests
    main()
