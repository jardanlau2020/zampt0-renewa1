#!/usr/bin/env python3
"""Zampto Auto Renewal — CloakBrowser + Turnstile solver.

Known issue: Cloudflare Turnstile is embedded in a srcdoc iframe and
Cloudflare JS Challenge may block form submission in headless mode.

Strategy:
1. Navigate to login page, wait for Turnstile to load
2. Fill email + password
3. Click Login button
4. Wait up to 25s for Turnstile iframe to appear (it loads AFTER form submission)
5. Find Turnstile via page.frames (all frames incl. srcdoc) OR main page
6. Click Turnstile checkbox
7. Wait for Cloudflare verification, then form auto-submits
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


def find_turnstile(page):
    """
    Find Turnstile checkbox. Returns (element, source) or (None, "").
    
    Checks in order:
    1. Main page: [data-turnstile], [class*='cf-turnstile'], [role='checkbox']
    2. All frames via page.frames (catches srcdoc iframes)
       - matches URL containing challenges.cloudflare.com or cdn-cgi
       - OR HTML containing turnstile/cloudflare
    """
    # 1. Check main page
    ts = page.query_selector("[data-turnstile]")
    if ts:
        return ts, "main-page[data-turnstile]"
    
    ts = page.query_selector("[class*='cf-turnstile']")
    if ts:
        return ts, "main-page[class*='cf-turnstile']"

    cb = page.query_selector("[role='checkbox']")
    if cb:
        # Check if it's in a turnstile context
        cls = cb.get_attribute("class") or ""
        parent_cls = cb.evaluate("n => n.parentElement ? n.parentElement.className : ''") or ""
        if "turnstile" in cls or "turnstile" in parent_cls or "challenge" in cls or "challenge" in parent_cls:
            return cb, "main-page[role=checkbox]"

    # 2. Check all frames (catches srcdoc)
    for f in page.frames:
        f_url = f.url or ""
        try:
            f_html = f.content()
        except Exception:
            f_html = ""

        # Match by URL or HTML content
        if ("challenges.cloudflare.com" in f_url or
                "cdn-cgi" in f_url or
                "turnstile" in f_url.lower() or
                "turnstile" in f_html.lower() or
                "cloudflare" in f_html.lower()):
            
            # Try to find checkbox in this frame
            cb = f.query_selector("[role='checkbox']")
            if cb:
                return cb, f"frame[url={f_url[:60]}]"

            cb = f.query_selector("[class*='checkbox']")
            if cb:
                return cb, f"frame[class*='checkbox'][url={f_url[:60]}]"

            cb = f.query_selector("[class*='challenge']")
            if cb:
                return cb, f"frame[class*='challenge'][url={f_url[:60]}]"

            # No checkbox found but Turnstile detected - dump frame content
            log.info("  [DETECT] Turnstile frame found (no checkbox yet): url=%s, html_len=%d",
                     f_url[:80], len(f_html))
            if "checkbox" in f_html.lower():
                # Try clicking a checkbox element
                el = f.query_selector("input[type='checkbox'], [role='checkbox']")
                if el:
                    return el, f"frame[input=checkbox]"

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
        # ── 1. Launch CloakBrowser ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── 2. Navigate to login ──
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
        snap(page, "01_login.png")

        # Check Turnstile before login
        ts_before, src_before = find_turnstile(page)
        log.info("Pre-login Turnstile: %s (%s)", "FOUND" if ts_before else "NOT FOUND", src_before)

        # Dump all frames for debugging
        log.info("All frames before login:")
        for i, f in enumerate(page.frames):
            f_url = f.url or "(about:blank)"
            f_html = ""
            try:
                f_html = f.content()[:200]
            except Exception:
                f_html = "(error)"
            has_ts = "turnstile" in f_html.lower()
            has_cf = "cloudflare" in f_html.lower()
            log.info("  [%d] url=%s, turnstile=%s, cloudflare=%s", i, f_url[:80], has_ts, has_cf)

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
            login_btn = page.query_selector("button:has-text('Login')")
        if login_btn:
            login_btn.click()
        else:
            if pwd_el:
                pwd_el.press("Enter")
            else:
                log.error("Cannot submit form")

        # ── 5. Wait for Turnstile to appear ──
        log.info("Waiting up to 25s for Turnstile to load...")
        ts = None
        ts_src = ""
        for i in range(25):
            time.sleep(1)
            ts, ts_src = find_turnstile(page)
            if ts:
                log.info(">>> Turnstile found at %ds: %s", i + 1, ts_src)
                break
            if i == 0 or i % 5 == 0:
                log.info("[%2ds] Turnstile not found, URL=%s", i + 1, page.url)

        if not ts:
            log.warning("Turnstile never appeared!")
            log.info("Current URL: %s", page.url)
            log.info("Body text: %s", page.inner_text("body")[:200])

            # Try clicking Turnstile container in main page
            ts_container = page.query_selector("[class*='cf-turnstile'], [class*='turnstile']")
            if ts_container:
                log.info("Found Turnstile container in main page, clicking...")
                ts_container.evaluate("n => n.click()")
                time.sleep(5)
                ts, ts_src = find_turnstile(page)
                if ts:
                    log.info(">>> Turnstile found after click: %s", ts_src)

        # ── 6. Click Turnstile checkbox ──
        if ts:
            log.info("Clicking Turnstile checkbox...")
            ts.evaluate("n => n.scrollIntoView({block:'center'}); n.click();")
            log.info("Waiting 12s for Cloudflare verification...")
            time.sleep(12)

            # After Turnstile passes, check if form auto-submitted
            post_ts_url = page.url
            log.info("Post-Turnstile URL: %s", post_ts_url)
        else:
            # Try to find and click Turnstile via JS
            log.info("Trying JS-based Turnstile click...")
            page.evaluate("""
() => {
    // Try clicking any Turnstile-related element
    const els = document.querySelectorAll('[class*="cf-turnstile"], [class*="turnstile"], [data-turnstile]');
    for (const el of els) {
        el.click();
    }
    // Also try clicking any checkbox that looks like Turnstile
    const cbs = document.querySelectorAll('[role="checkbox"]');
    for (const cb of cbs) {
        const parent = cb.parentElement;
        if (parent && (parent.className || '').toLowerCase().includes('turnstile')) {
            cb.click();
        }
    }
}
""")
            time.sleep(8)

        # ── 7. Poll for login success ──
        log.info("Waiting for login redirect (up to 20s)...")
        login_ok = False
        for i in range(20):
            time.sleep(1)
            url = page.url
            txt = page.inner_text("body")[:200]
            if i % 5 == 0:
                log.info("[%2ds] URL: %s | Text: %s", i + 1, url, txt[:100])
            if "login" not in url.lower() and "dash.zampto" in url.lower() and "auth" not in url:
                log.info(">>> LOGIN SUCCESS at %ds!", i + 1)
                login_ok = True
                break

        snap(page, "03_post_login.png")

        if not login_ok:
            log.warning("Login FAILED. Final URL: %s", page.url)
            log.info("Body: %s", page.inner_text("body")[:300])
            snap(page, "03_failed.png")

            # ── 8. Final attempt: re-click Login after Turnstile wait ──
            log.info("Trying one more Login click...")
            login_btn2 = page.query_selector("button[type='submit'], button:has-text('Login')")
            if login_btn2:
                login_btn2.click()
                time.sleep(8)
                url2 = page.url
                log.info("After retry URL: %s", url2)
                if "login" not in url2.lower() and "dash.zampto" in url2.lower() and "auth" not in url2:
                    login_ok = True
                    log.info(">>> LOGIN SUCCESS on retry!")

        snap(page, "04_final_login.png")

        # ── 9. Navigate to server page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(2)
        snap(page, "05_server.png")

        srv_url = page.url
        srv_txt = page.inner_text("body")[:300]
        log.info("Server page URL: %s", srv_url)
        log.info("Server page text: %s", srv_txt)

        # ── 10. Determine status ──
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

        # ── 11. Start if stopped ──
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

        # ── 12. Expiry & renew ──
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
            log.info("Expiry: %d days %d h (total %d h)", days, h, total_h)

            if FORCE_RENEW or total_h < 48:
                report["action"] = "renewed"
                renew_btn = None
                for sel in ["button:has-text('Renew')", "button:has-text('续期')",
                             "button:has-text('续费')", "text=Renew"]:
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
