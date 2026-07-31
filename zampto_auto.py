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
        # ── Launch ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── Navigate to login ──
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)  # Let Turnstile load
        snap(page, "01_login.png")

        # ── Check what's on the page ──
        html = page.content()
        log.info("HTML length: %d, turnstile count: %d, cloudflare count: %d",
                 len(html), html.count("turnstile"), html.count("cloudflare"))
        log.info("iframe count: %d", html.count("<iframe"))
        log.info("Form action: %s", re.findall(r'action=["\']([^"\']*)', html)[:3])

        # ── Fill email ──
        log.info("Filling email...")
        email_el = page.query_selector("input[id='email'], input[type='email']")
        if email_el:
            email_el.fill(USERNAME)
            time.sleep(1)
        else:
            log.warning("Email input not found")

        # ── Fill password ──
        log.info("Filling password...")
        pwd_el = page.query_selector("input[id='password'], input[type='password']")
        if pwd_el:
            pwd_el.fill(PASSWORD)
            time.sleep(1)
        else:
            log.warning("Password input not found")

        snap(page, "02_filled.png")

        # ── Click Login ──
        log.info("Clicking Login button")
        login_btn = page.query_selector("button[type='submit']")
        if login_btn:
            login_btn.click()
        else:
            # Try alternate
            page.evaluate("() => { const bs = document.querySelectorAll('button'); for (const b of bs) { if (b.textContent.trim() === 'Login') { b.click(); break; } } }")

        # ── Wait and check URL changes ──
        log.info("Waiting for redirect (30s)...")
        for i in range(30):
            time.sleep(1)
            url = page.url
            txt = page.inner_text("body")[:200]
            log.info("[%2ds] URL: %s | Text: %s", i + 1, url, txt)
            if "login" not in url.lower() and "dash.zampto" in url.lower():
                log.info(">>> Login SUCCESS at %ds!", i + 1)
                break

        snap(page, "03_post_login.png")

        # Check Turnstile iframe
        turnstile_found = page.query_selector("iframe[src*='turnstile']")
        log.info("Turnstile iframe after login click: %s", "FOUND" if turnstile_found else "NOT FOUND")

        # ── Try Turnstile solve ──
        if turnstile_found:
            log.info("Attempting to solve Turnstile...")
            try:
                cf_frame = turnstile_found.content_frame()
                if cf_frame:
                    cb = cf_frame.query_selector("[role='checkbox'], [class*='checkbox']")
                    if cb:
                        cb.evaluate("n => n.click()")
                        log.info("Turnstile checkbox clicked, waiting 15s...")
                        time.sleep(15)

                        # Click Login again
                        lb2 = page.query_selector("button[type='submit']")
                        if lb2:
                            lb2.click()
                        time.sleep(10)
            except Exception as e:
                log.warning("Turnstile solve failed: %s", e)

        # ── Final check ──
        final_url = page.url
        final_txt = page.inner_text("body")[:300]
        log.info("Final URL: %s", final_url)
        log.info("Final text: %s", final_txt)
        snap(page, "04_after_turnstile.png")

        login_ok = ("login" not in final_url.lower() and "welcome back" not in final_txt.lower()
                    and "security verification" not in final_txt.lower())

        if not login_ok:
            log.warning("Login FAILED. Dumping HTML for analysis...")
            snap(page, "04_failed.png")

        # ── Go to server page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to server: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(2)
        snap(page, "05_server.png")

        srv_txt = page.inner_text("body")[:500]
        srv_url = page.url
        log.info("Server page URL: %s", srv_url)
        log.info("Server page text: %s", srv_txt)

        # ── Determine status ──
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

        # ── Start if stopped ──
        if not is_running:
            start_btn = None
            for sel in ["button:has-text('Start')", "a:has-text('Start')", "text=Start"]:
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

        # ── Expiry & renew ──
        expiry_el = None
        for sel in ["text=/Expiry|Renew|到期|剩余/i", "text=/Expire|过期/i",
                     "text=/Plan|套餐/i", "text=/days/h/i", "text=/Remaining/i"]:
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
            log.info("Expiry: %d days %d h", days, h)

            if FORCE_RENEW or total_h < 48:
                report["action"] = "renewed"
                renew_btn = None
                for sel in ["button:has-text('Renew')", "button:has-text('续期')", "text=Renew"]:
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
