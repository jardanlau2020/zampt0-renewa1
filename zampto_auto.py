#!/usr/bin/env python3
"""Zampto Auto Renewal - CloakBrowser-based automation."""

import os, re, sys, json, time, logging
from datetime import datetime, timezone
import requests
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
        log.warning("Telegram not configured, skip")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": f"{title}\n\n{body}", "parse_mode": "Markdown"}, timeout=15)
        r.raise_for_status()
        log.info("Telegram sent OK")
    except Exception as e:
        log.error("Telegram failed: %s", e)


def wait_for_sel(page, selector, timeout=30, label="element"):
    try:
        page.wait_for_selector(selector, timeout=timeout * 1000)
        log.info("Found %s: %s", label, selector)
        return True
    except Exception:
        log.warning("Timeout %s: %s", label, selector)
        return False


def snap(page, name, path="./screenshots"):
    os.makedirs(path, exist_ok=True)
    fp = os.path.join(path, name)
    page.screenshot(path=fp)
    log.info("Screenshot: %s", fp)
    return fp


def parse_expiry(text):
    text = text.strip()
    days = h = m = 0
    dm = re.search(r"(\d+)\s*day", text)
    hm = re.search(r"(\d+)\s*h", text)
    mm = re.search(r"(\d+)\s*m", text)
    if dm:
        days = int(dm.group(1))
    if hm:
        h = int(hm.group(1))
    if mm:
        m = int(mm.group(1))
    log.info("Parsed expiry: %d days %d hours %d min", days, h, m)
    return days, h, m, days * 24 + h


def solve_turnstile(page):
    """Find Cloudflare Turnstile iframe, click its checkbox, return True if solved."""
    log.info("Waiting for Turnstile iframe...")
    time.sleep(3)

    for attempt in range(2):
        cf_iframe = page.query_selector("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']")
        if cf_iframe:
            log.info("Turnstile iframe found (attempt %d)", attempt + 1)
            try:
                cf_frame = cf_iframe.content_frame()
                if cf_frame:
                    # Try multiple checkbox selectors inside the iframe
                    checkbox = None
                    for sel in ["[role='checkbox']", "[class*='checkbox']", "[class*='turnstile']",
                                "input[type='checkbox']", "[class*='challenge']", "button"]:
                        checkbox = cf_frame.query_selector(sel)
                        if checkbox:
                            log.info("  >>> Found checkbox in iframe: %s", sel)
                            break
                    if checkbox:
                        # Try JS-native click first (triggers proper handlers)
                        try:
                            checkbox.evaluate("node => node.click()")
                            log.info("  Clicked checkbox via JS")
                        except Exception:
                            checkbox.click()
                            log.info("  Clicked checkbox via Playwright")
                        time.sleep(10)
                        log.info("Turnstile checkbox clicked, waiting for verification...")
                        return True
                    else:
                        txt = cf_frame.text_content()[:200]
                        log.info("  No checkbox found in iframe. Text: %s", txt)
                else:
                    log.info("  content_frame() returned None")
            except Exception as e:
                log.warning("  Failed to access Turnstile iframe: %s", e)
        else:
            log.info("Turnstile iframe not found (attempt %d), waiting...", attempt + 1)
            time.sleep(3)

    return False


def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing env vars: ZAMPTO_USERNAME, ZAMPTO_PASSWORD, ZAMPTO_SERVER_ID")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s  |  Force: %s", SERVER_ID, FORCE_RENEW)

    report = {
        "server_id": SERVER_ID,
        "status": "unknown",
        "action": "none",
        "expiry": None,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    browser = None

    try:
        # ── 1. Launch ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── 2. Navigate ──
        log.info("Navigating to %s", DASHBOARD_URL)
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "01_dashboard.png")

        # ── 3. Login ──
        login_ok = False
        if "login" in page.url.lower():
            log.info("Login page: %s", page.url)
            snap(page, "02_login.png")

            # Email
            if wait_for_sel(page, "input[name='email'], input[type='email'], input[name='username']", 15, "email"):
                page.query_selector("input[name='email'], input[type='email'], input[name='username']").fill(USERNAME)
                log.info("Email filled")
                time.sleep(1)

            # Password
            if wait_for_sel(page, "input[type='password']", 15, "password"):
                pwd_input = page.query_selector("input[type='password']")
                pwd_input.fill(PASSWORD)
                log.info("Password filled")
                time.sleep(1)

            # Login button
            login_btn = None
            for sel in ["button:has-text('Login')", "button:has-text('login')",
                         "button[type='submit']", "text=Login", "text=登录"]:
                try:
                    login_btn = page.query_selector(sel)
                    if login_btn:
                        log.info("Login button found: %s", sel)
                        break
                except Exception:
                    continue

            if login_btn:
                log.info("Clicking Login")
                login_btn.click()
            else:
                pwd_input.press("Enter")

            # ── Solve Turnstile ──
            turned = solve_turnstile(page)

            # Click Login again if Turnstile was interacted with
            if turned:
                log.info("Turnstile solved, clicking Login again")
                lb2 = page.query_selector("button:has-text('Login')")
                if lb2:
                    lb2.click()
                    time.sleep(5)

            # Check result
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            snap(page, "03_post_login.png")
            url_now = page.url
            txt_now = page.inner_text("body")[:300]
            log.info("Post-login URL: %s", url_now)
            log.info("Post-login text: %s", txt_now)

            if "login" not in url_now.lower() and "Welcome Back" not in txt_now:
                log.info(">>> Login SUCCESS! <<<")
                snap(page, "03_login_success.png")
                login_ok = True
            else:
                log.warning("Login FAILED — still on login page")
                snap(page, "03_login_failed.png")

        # ── 4. Server detail page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to server: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(2)
        snap(page, "04_server_detail.png")

        if not login_ok:
            log.warning("Proceeding without confirmed login — status may be unreliable")

        # ── 5. Server status ──
        status_text = ""
        for cls in ["status-running", "status-stopped", "status-starting", "status-stopping"]:
            el = page.query_selector(f".{cls}")
            if el:
                status_text = el.inner_text()
                break
        if not status_text:
            el = page.query_selector("text=/Running|Stopped|Starting|Stopping/i")
            if el:
                status_text = el.inner_text()

        is_running = "running" in status_text.lower() if status_text else False
        report["status"] = "running" if is_running else "stopped"
        log.info("Server status: %s", report["status"])

        # ── 6. Start if stopped ──
        if not is_running:
            log.info("Server stopped, trying to Start")
            log.info("Page text (500): %s", page.inner_text("body")[:500])
            start_btn = None
            for sel in ["button:has-text('Start')", "button:has-text('start')",
                         "a:has-text('Start')", "div:has-text('Start')",
                         "button:has-text('启动')", "text=Start", "text=启动"]:
                try:
                    start_btn = page.query_selector(sel)
                    if start_btn:
                        log.info("Start button: %s", sel)
                        break
                except Exception:
                    continue
            if start_btn:
                start_btn.click()
                time.sleep(3)
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                snap(page, "05_started.png")
                report["action"] = "started"
            else:
                report["action"] = "start-failed"
                report["error"] = "Start button not found"

        # ── 7. Expiry & Renew ──
        expiry_el = None
        for sel in ["text=/Expiry|Renew|到期|剩余/i", "text=/Expire|过期|有效期/i",
                     "text=/Plan|套餐/i", "text=/days|h/m/i", "text=/Remaining/i"]:
            try:
                expiry_el = page.query_selector(sel)
                if expiry_el:
                    log.info("Expiry element: %s", sel)
                    break
            except Exception:
                continue

        if expiry_el:
            expiry_text = expiry_el.inner_text()
            report["expiry"] = expiry_text
            log.info("Expiry: %s", expiry_text)
            days, hours, mins, total_h = parse_expiry(expiry_text)
            if FORCE_RENEW or total_h < 48:
                log.info("Initiating renewal (days=%d hours=%d force=%s)", days, hours, FORCE_RENEW)
                report["action"] = "renewed"
                renew_btn = None
                for sel in ["button:has-text('Renew')", "button:has-text('续期')",
                             "button:has-text('续费')", "a:has-text('Renew')",
                             "text=Renew", "text=续期", "text=续费"]:
                    try:
                        renew_btn = page.query_selector(sel)
                        if renew_btn:
                            log.info("Renew button: %s", sel)
                            break
                    except Exception:
                        continue
                if renew_btn:
                    renew_btn.click()
                    time.sleep(2)
                    wait_for_sel(page, "[data-sitekey], .cf-turnstile", 30, "turnstile")
                    time.sleep(8)
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    snap(page, "06_renew.png")
                    el2 = page.query_selector("text=/Expiry|到期/i")
                    if el2:
                        report["expiry"] = el2.inner_text()
                else:
                    report["action"] = "renew-failed"
                    report["error"] = "Renew button not found"
            else:
                log.info("No renewal needed (total_h=%d)", total_h)
                report["action"] = "skipped"
        else:
            report["error"] = "Expiry element not found"
            log.info("Page text (800): %s", page.inner_text("body")[:800])

        snap(page, "07_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    # ── Notification ──
    status_icon = "🟢" if report["status"] == "running" else "🔴"
    icons = {"started": "▶️", "renewed": "🔄", "skipped": "⏭️", "renew-failed": "❌", "none": "⚪"}
    ai = icons.get(report["action"], "❓")
    body = (f"🖥️ **Zampto Server Report**\n\n"
            f"**Server ID:** `{SERVER_ID}`\n"
            f"**Status:** {status_icon} {report['status'].title()}\n"
            f"**Action:** {ai} {report['action']}")
    if report.get("expiry"):
        body += f"\n**Expiry:** {report['expiry']}"
    if report.get("error"):
        body += f"\n**⚠️ Error:** {report['error']}"
    body += f"\n\n_Generated: {report['timestamp']}_"

    log.info("--- Report ---\n%s", body)
    push_tg("🖥️ Zampto Server Report", body)

    os.makedirs("./screenshots", exist_ok=True)
    with open("./screenshots/report.json", "w") as f:
        json.dump(report, f, indent=2)
    log.info("Report saved to ./screenshots/report.json")


if __name__ == "__main__":
    main()
