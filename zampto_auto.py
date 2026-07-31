#!/usr/bin/env python3
"""Zampto Auto Renewal — CloakBrowser + Turnstile solver.

Strategy:
1. Navigate to login page
2. Fill email + password
3. Click Login button → this triggers the JS submit handler
   which shows Cloudflare Turnstile
4. Find all iframes, look for Turnstile
5. Click Turnstile checkbox inside the iframe
6. Wait for Cloudflare verification
7. Login should auto-complete after Turnstile passes
8. Navigate to server detail page, check status, start if needed
"""

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
        log.warning("Telegram not configured")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": f"{title}\n\n{body}", "parse_mode": "Markdown"},
                          timeout=15)
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


def find_turnstile_iframe(page):
    """Find the Turnstile iframe among all iframes."""
    for f in page.frames:
        url = f.url or ""
        # The Turnstile iframe has a cloudflare challenges URL
        if "challenges.cloudflare.com" in url or "cdn-cgi/challenge-platform" in url:
            return f, url
        # Also check for Turnstile in frame name or HTML
        try:
            html = f.content()
            if "turnstile" in html.lower() or "cloudflare" in html.lower():
                return f, url
        except Exception:
            pass
    return None, ""


def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing env vars")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s | Force: %s", SERVER_ID, FORCE_RENEW)

    report = {"server_id": SERVER_ID, "status": "unknown", "action": "none",
              "expiry": None, "error": None, "timestamp": datetime.now(timezone.utc).isoformat()}
    browser = None

    try:
        # ── 1. Launch ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── 2. Navigate to login ──
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)  # Let initial page + Turnstile load
        snap(page, "01_login.png")

        # Check Turnstile before login
        t_frame, t_url = find_turnstile_iframe(page)
        log.info("Pre-login Turnstile: %s (%s)", "FOUND" if t_frame else "NOT FOUND", t_url[:80])

        # ── 3. Fill form ──
        email_el = page.query_selector("input[id='email'], input[type='email']")
        if email_el:
            email_el.fill(USERNAME)
            time.sleep(1)
        else:
            log.warning("Email input not found")

        pwd_el = page.query_selector("input[id='password'], input[type='password']")
        if pwd_el:
            pwd_el.fill(PASSWORD)
            time.sleep(1)
        else:
            log.warning("Password input not found")

        snap(page, "02_filled.png")

        # ── 4. Click Login ──
        log.info("Clicking Login button...")
        login_btn = page.query_selector("button[type='submit']")
        if not login_btn:
            # Try alternate selectors
            login_btn = page.query_selector("button:has-text('Login')")
        if login_btn:
            login_btn.click()
        else:
            log.warning("Login button not found, trying Enter on password field")
            if pwd_el:
                pwd_el.press("Enter")
            else:
                log.error("Cannot submit form")

        # ── 5. Wait for Turnstile iframe to appear ──
        log.info("Waiting for Turnstile iframe to appear...")
        cf_frame = None
        for i in range(20):
            time.sleep(2)
            cf_frame, cf_url = find_turnstile_iframe(page)
            if cf_frame:
                log.info("Turnstile iframe found at %2ds: %s", i * 2 + 2, cf_url[:100])
                break
            else:
                log.info("Turnstile not found (attempt %d/20)", i + 1)

        if not cf_frame:
            log.warning("Turnstile iframe never appeared. Checking all frames...")
            for f in page.frames:
                log.info("  Frame: url=%s", f.url or "(about:blank)")

            # Maybe Turnstile is in the main frame directly
            ts_in_main = page.query_selector("[data-turnstile]")
            log.info("Turnstile data-turnstile in main: %s", "FOUND" if ts_in_main else "NOT FOUND")

            snap(page, "03_no_turnstile.png")
            log.info("Current URL: %s", page.url)
            log.info("Body text: %s", page.inner_text("body")[:200])

            # Try clicking Turnstile checkbox in main page directly
            cb_main = page.query_selector("[role='checkbox'], [class*='checkbox'], [class*='cf-turnstile']")
            if cb_main:
                log.info("Found Turnstile checkbox in main page, clicking...")
                cb_main.evaluate("n => n.click()")
                time.sleep(10)

        # ── 6. Try to click Turnstile checkbox in iframe ──
        if cf_frame:
            log.info("Attempting to click Turnstile checkbox in iframe...")
            try:
                # Look for the checkbox in the Turnstile frame
                checkbox = cf_frame.query_selector("[role='checkbox']")
                if not checkbox:
                    checkbox = cf_frame.query_selector("[class*='checkbox']")
                if not checkbox:
                    checkbox = cf_frame.query_selector("[class*='challenge']")
                if not checkbox:
                    checkbox = cf_frame.query_selector("input[type='checkbox']")
                if not checkbox:
                    # Dump frame content for debugging
                    frame_html = cf_frame.content()[:500]
                    log.info("Frame HTML (first 500): %s", frame_html)
                    # Try clicking the whole frame
                    log.info("No checkbox found, clicking frame area...")
                    # Click the Turnstile container
                    turnstile_el = cf_frame.query_selector("[class*='turnstile']")
                    if turnstile_el:
                        turnstile_el.evaluate("n => n.click()")
                    else:
                        log.info("Clicking at [200,200] in frame...")
                        cf_frame.click(x=200, y=200)
                else:
                    checkbox.evaluate("n => n.click()")
                    log.info("Turnstile checkbox clicked!")

                # Wait for Cloudflare verification
                log.info("Waiting 15s for Cloudflare verification...")
                time.sleep(15)
            except Exception as e:
                log.warning("Turnstile solve failed: %s", e)

        # ── 7. Poll for login success ──
        log.info("Waiting for login redirect (up to 30s)...")
        login_ok = False
        for i in range(30):
            time.sleep(1)
            url = page.url
            txt = page.inner_text("body")[:200]
            if i < 5 or i % 5 == 0:
                log.info("[%2ds] URL: %s | Text: %s", i + 1, url, txt[:100])
            if "login" not in url.lower() and "dash.zampto" in url.lower() and "auth" not in url:
                log.info(">>> LOGIN SUCCESS at %ds!", i + 1)
                login_ok = True
                break

        snap(page, "04_post_login.png")

        if not login_ok:
            log.warning("Login FAILED. Final URL: %s", page.url)
            log.info("Body: %s", page.inner_text("body")[:300])
            snap(page, "04_failed.png")

        # ── 8. Navigate to server page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(2)
        snap(page, "05_server.png")

        srv_url = page.url
        srv_txt = page.inner_text("body")[:300]
        log.info("Server page URL: %s", srv_url)
        log.info("Server page text: %s", srv_txt)

        # ── 9. Determine status ──
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

        # ── 10. Start if stopped ──
        if not is_running:
            start_btn = None
            for sel in ["button:has-text('Start')", "a:has-text('Start')",
                         "button:has-text('start')", "a:has-text('start')", "text=Start"]:
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
                snap(page, "06_started.png")
                report["action"] = "started"
            else:
                report["action"] = "start-failed"
                report["error"] = "Start button not found"

        # ── 11. Expiry & renew ──
        expiry_el = None
        for sel in ["text=/Expiry|Renew|到期|剩余/i", "text=/Expire|过期/i",
                     "text=/Plan|套餐/i", "text=/days/h/i", "text=/Remaining/i",
                     "text=/days/h/m/i"]:
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
            days = h = 0
            dm = re.search(r"(\d+)\s*day", expiry_text)
            hm = re.search(r"(\d+)\s*h", expiry_text)
            if dm: days = int(dm.group(1))
            if hm: h = int(hm.group(1))
            total_h = days * 24 + h
            log.info("Expiry: %d days %d h (total %d h)", days, h, total_h)

            if FORCE_RENEW or total_h < 48:
                report["action"] = "renewed"
                renew_btn = None
                for sel in ["button:has-text('Renew')", "button:has-text('续期')",
                             "button:has-text('续费')", "a:has-text('Renew')", "text=Renew"]:
                    try:
                        renew_btn = page.query_selector(sel)
                        if renew_btn:
                            log.info("Renew button: %s", sel)
                            break
                    except Exception:
                        continue
                if renew_btn:
                    renew_btn.click()
                    time.sleep(5)
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    snap(page, "07_renew.png")
                else:
                    report["action"] = "renew-failed"
                    report["error"] = "Renew button not found"
            else:
                log.info("No renewal needed (total_h=%d)", total_h)
                report["action"] = "skipped"
        else:
            report["error"] = "Expiry element not found"

        snap(page, "08_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    # ── Report ──
    status_icon = "🟢" if report["status"] == "running" else "🔴"
    icons = {"started": "▶️", "renewed": "🔄", "skipped": "⏭️", "renew-failed": "❌", "none": "⚪"}
    body = (f"🖥️ **Zampto Server Report**\n\n"
            f"**Server ID:** `{SERVER_ID}`\n"
            f"**Status:** {status_icon} {report['status'].title()}\n"
            f"**Action:** {icons.get(report['action'], '❓')} {report['action']}")
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
    log.info("Report saved")


if __name__ == "__main__":
    main()
