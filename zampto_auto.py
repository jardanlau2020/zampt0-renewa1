#!/usr/bin/env python3
"""Zampto Auto Renewal v5 - Hybrid mode with Session Cookie Reuse.

Phase 1 (First run): 
  - Run locally with desktop GUI
  - Complete login manually in browser (solve Turnstile)
  - Script saves session cookies to ./screenshots/session.json

Phase 2 (GitHub Actions):
  - Encode session.json as base64 and store as SECRET (e.g., ZAMPTO_SESSION)
  - Decode at runtime, use requests.Session with saved cookies
  - Skip browser entirely, call /api/server/ status & renewal APIs directly

This bypasses Cloudflare Turnstile completely after the initial manual setup.
"""

import os, re, sys, json, time, logging, base64, tempfile
from datetime import datetime, timezone
try:
    import requests
except ImportError:
    print("requests not installed. Install: pip install requests")
    raise

# Try importing cloakbrowser only if needed
HAS_CLOAKBROWSER = False
try:
    from cloakbrowser import launch
    HAS_CLOAKBROWSER = True
except Exception:
    pass

USERNAME = os.getenv("ZAMPTO_USERNAME", "")
PASSWORD = os.getenv("ZAMPTO_PASSWORD", "")
SERVER_ID = os.getenv("ZAMPTO_SERVER_ID", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FORCE_RENEW = os.getenv("FORCE_RENEW", "false").lower() == "true"
DASHBOARD_URL = "https://dash.zampto.net"
SESSION_FILE = "./screenshots/session.json"
LOG_DIR = "./screenshots"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto")


def push_tg(title, body):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram config missing, skipping send")
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


def snap(page, name):
    os.makedirs(LOG_DIR, exist_ok=True)
    fp = os.path.join(LOG_DIR, name)
    page.screenshot(path=fp)
    log.info("Screenshot: %s", fp)
    return fp


def save_session_cookies(session_cookies, path=SESSION_FILE):
    """Save browser cookies to JSON file for reuse."""
    data = {"cookies": session_cookies, "saved_at": datetime.now(timezone.utc).isoformat()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Session saved to %s", path)


def load_session(path=SESSION_FILE):
    """Load saved session cookies from JSON file."""
    if not os.path.exists(path):
        log.info("No session file found - will need to log in via browser")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        log.info("Loaded %d saved cookies from session file", len(cookies))
        return cookies
    except Exception as e:
        log.error("Failed to load session: %s", e)
        return None


def sync_cookies_to_session(session_obj, cookies):
    """Sync a list of cookie dicts into a requests.Session."""
    session_obj.cookies.clear()
    for c in cookies:
        try:
            session_obj.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                secure=c.get("secure", False),
                expires=c.get("expires"),
            )
        except Exception as e:
            log.warning("Cookie set failed (%s): %s", c.get("name"), e)


def find_csrf_cookie(cookies):
    """Find the CSRF token from cookie list."""
    for c in cookies:
        name_lower = c.get("name", "").lower()
        if "csrf" in name_lower:
            return c["value"]
    return None


def get_api_session():
    """Create a requests.Session with all necessary headers for Zampto API."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def fill_field_el(page, selector, value):
    try:
        el = page.query_selector(selector)
        if el:
            el.fill(value)
            log.info("Filled field with selector: %s", selector)
            return True
    except Exception as e:
        log.warning("Fill failed with %s: %e", selector, e)
    return False


def click_btn(page, selector):
    try:
        btn = page.query_selector(selector)
        if btn:
            btn.click()
            log.info("Clicked button with selector: %s", selector)
            return True
    except Exception as e:
        log.warning("Click failed with %s: %e", selector, e)
    return False


def wait_for_url_change(page, start_url, max_wait=30):
    """Poll until URL changes from start_url."""
    for i in range(max_wait):
        time.sleep(1)
        if page.url != start_url:
            log.info("URL changed to: %s", page.url)
            return True
    log.info("Max wait exceeded, still on %s", page.url)
    return False


def phase_browser_login_interactive():
    """Phase 1 (LOCAL ONLY): Interactive browser login with user-visible window."""
    log.info("=== INTERACTIVE BROWSER LOGIN ===")
    log.info("A browser window will open on your desktop. Please complete the following:")
    log.info("1. Enter your email and password")
    log.info("2. Solve the Cloudflare Turnstile CAPTCHA if prompted")
    log.info("3. Click the Login button")
    log.info("4. After successful login, close the browser window")
    log.info("")

    if not HAS_CLOAKBROWSER:
        raise RuntimeError("CloakBrowser not available - cannot run browser login")

    proxy = None
    if os.getenv("HY2_CONFIG", ""):
        proxy = {"server": "socks5://127.0.0.1:1080"}

    browser = launch(headless=False, proxy=proxy)  # headless=False for visible UI
    page = browser.new_page()

    start_url = f"{DASHBOARD_URL}/auth/login"
    log.info("Opening browser at: %s", start_url)
    page.goto(start_url, wait_until="domcontentloaded", timeout=90000)
    snap(page, "01_login_ready.png")

    # User handles everything manually in the visible browser
    input("Please complete login in the browser window, then press Enter to continue...")

    # Check if we're still on login page
    if "/auth/login" in page.url.lower():
        log.warning("Login appears to have failed - still on login page")
        snap(page, "01_login_failed.png")
    else:
        log.info("Login seems successful. URL is now: %s", page.url)
        snap(page, "01_login_success.png")

    # Save cookies
    cookies = page.context.cookies()
    save_session_cookies(cookies)
    browser.close()
    log.info("Interactive login completed. Session saved to session.json.")


def phase_api_renewal(use_cookies=None):
    """Phase 2: Use provided cookies to renew server via pure API (no browser)."""
    log.info("=== PURE API RENEWAL MODE ===")

    # Use provided cookies or fall back to loading from file
    cookies = use_cookies
    if not cookies:
        cookies = load_session()

    if not cookies:
        log.error("No valid cookies/session available - cannot proceed")
        return False

    log.info("Using %d cookies for API authentication", len(cookies))
    api_session = get_api_session()
    sync_cookies_to_session(api_session, cookies)

    # Verify session by checking a safe endpoint
    try:
        test_resp = api_session.get(f"{DASHBOARD_URL}/auth/login", timeout=10)
        # If we got the login page (HTML), that's good - means we have a session
        if test_resp.status_code == 200 and "antialiased" in test_resp.text[:500]:
            log.info("Session verified - authenticated")
        else:
            log.warning("Unexpected response from login page: %d", test_resp.status_code)
    except Exception as e:
        log.error("Session verification failed: %e", e)

    # Fetch server info
    try:
        server_url = f"{DASHBOARD_URL}/api/server/{SERVER_ID}"
        log.info("Fetching server info from: %s", server_url)
        resp = api_session.get(server_url, timeout=15)
        resp.raise_for_status()
        server_data = resp.json()
        log.info("Server data received (truncated): %s", json.dumps(server_data, indent=2, ensure_ascii=False)[:600])

        # Parse status
        state_info = server_data.get("data", {}) if isinstance(server_data, dict) else {}
        status_state = state_info.get("status", {}).get("state", "").lower() if isinstance(state_info, dict) else ""
        is_running = status_state in ["running", "started", "active", "online"]

        report = {
            "server_id": SERVER_ID,
            "status": "running" if is_running else "stopped",
            "action": "none",
            "expiry": None,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log.info("Server status: %s", report["status"])

        # Start if stopped
        started = False
        if not is_running:
            log.info("Server is stopped, attempting to start...")
            start_url = f"{DASHBOARD_URL}/api/server/{SERVER_ID}/start"
            resp = api_session.post(start_url, timeout=15)
            if resp.status_code in [200, 201, 204, 202]:
                report["action"] = "started"
                log.info("Server start initiated successfully (status %d)", resp.status_code)
                is_running = True
                report["status"] = "running"
                started = True
            else:
                log.error("Start attempt failed: %d %s", resp.status_code, resp.text[:200])
                report["error"] = f"Start failed: {resp.status_code}"

        # Check expiry and renew
        if report["action"] in ("started", "skipped"):
            expiry_val = state_info.get("expiry") if isinstance(state_info, dict) else None
            if expiry_val:
                report["expiry"] = str(expiry_val)
                # Parse hours from expiry string (basic parsing)
                m = re.search(r'(\d+)\s*(?:day|d|天)', expiry_val, re.IGNORECASE)
                h = re.search(r'(\d+)\s*(?:hour|h|小时)', expiry_val, re.IGNORECASE)
                days = int(m.group(1)) if m else 0
                hours = int(h.group(1)) if h else 0
                total_h = days * 24 + hours
                log.info("Expiry: %s = %d days %d h = %d h total", expiry_val, days, hours, total_h)

                should_renew = FORCE_RENEW or total_h < 48
                if should_renew:
                    log.info("Renewing server (%d h left, threshold: 48h)", total_h)
                    renew_url = f"{DASHBOARD_URL}/api/server/{SERVER_ID}/renew"
                    resp = api_session.post(renew_url, timeout=15)
                    if resp.status_code in [200, 201, 204, 202]:
                        report["action"] = "renewed"
                        log.info("Renewal successful (status %d)", resp.status_code)
                    else:
                        log.error("Renewal failed: %d %s", resp.status_code, resp.text[:200])
                        report["error"] = f"Renewal failed: {resp.status_code}"
                else:
                    report["action"] = "skipped"
                    log.info("Not renewing - %d hours remaining (threshold: 48)", total_h)
            else:
                report["action"] = "skipped"
                log.warning("No expiry field in API response")

        _report(report)
        return True

    except requests.exceptions.RequestException as e:
        log.error("API request error: %e", e)
        report["error"] = f"API request failed: {str(e)}"
        _report(report)
        return False
    except Exception as e:
        log.error("API renewal failed unexpectedly: %e", e)
        report["error"] = str(e)
        _report(report)
        return False


def _report(report):
    status_icon = "\U0001F7E2" if report["status"] == "running" else "\U0001F534"
    icons = {
        "started": "\u25B6\ufe0f", "renewed": "\U0001F504", "skipped": "\u23ED\ufe0f",
        "renew-failed": "\u26A0\ufe0f", "none": "\U0001F4CB",
        "start-failed": "\u2753", "login-failed": "\U0001F512",
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

    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Report saved")


def main():
    # Validate env vars
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing required env vars: USERNAME, PASSWORD, SERVER_ID")
        push_tg("🚨 Setup Error", "Missing ZAMPTO credentials. Configure GitHub Secrets.")
        return

    log.info("=== Zampto Auto Renewal v5 ===")
    log.info("Server ID: %s | Force: %s", SERVER_ID, FORCE_RENEW)

    # ── Determine mode: GitHub (pure API) or Local (hybrid) ────────
    is_github_actions = bool(os.getenv("GITHUB_ACTION") or os.getenv("CI"))
    session_secret = os.getenv("ZAMPTO_SESSION_SECRET")
    cookies = None

    # Mode A: GitHub Actions – pure API via ZAMPTO_SESSION_SECRET
    if is_github_actions and session_secret:
        log.info("=== GITHUB ACTIONS MODE: Pure API, no browser ===")
        try:
            decoded = base64.b64decode(session_secret).decode("utf-8")
            session_data = json.loads(decoded)
            cookies = session_data.get("cookies", [])
            log.info("Loaded %d cookies from ZAMPTO_SESSION_SECRET", len(cookies))
            if not cookies:
                raise ValueError("No cookies found in session secret")
        except Exception as e:
            log.error("Failed to parse ZAMPTO_SESSION_SECRET: %e", e)
            push_tg("🚨 Session Error", f"Cannot decode ZAMPTO_SESSION_SECRET: {str(e)}")
            cookies = None  # fall through to fail cleanly

    # Mode B: Local dev – try saved session file
    elif not is_github_actions:
        cookies = load_session()
        if cookies:
            log.info("Found session file, using API mode directly")
        else:
            log.info("No session file found - will use interactive login")

    # Mode C: No session available – FAIL (GitHub should have secret)
    if not cookies:
        log.error("No valid authentication available - cannot proceed")
        reason = "Missing ZAMPTO_SESSION_SECRET (GitHub) OR missing ./screenshots/session.json (local)"
        push_tg("🚨 Authentication Error", reason)
        report = {
            "server_id": SERVER_ID, "status": "unknown", "action": "none",
            "expiry": None, "error": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _report(report)
        return

    # Execute API renewal with the loaded cookies
    log.info("Starting API-based server status check & renewal...")
    success = phase_api_renewal(use_cookies=cookies)

    if success:
        log.info("✓ Renewal completed successfully!")
        if not is_github_actions:
            log.info("Tip: Run again without interactive mode to save session for future automated runs")
    else:
        log.error("✗ Renewal failed - check session validity and API endpoints")
        push_tg("🔴 Renewal Failed", "Authentication succeeded but renewal operation failed. Check session expiration.")


if __name__ == "__main__":
    main()
