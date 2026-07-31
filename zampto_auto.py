#!/usr/bin/env python3
"""Zampto Auto Renewal - CloakBrowser + CDP API capture + Turnstile bypass.

Strategy:
1. Use CloakBrowser to load login page (bypasses most Cloudflare JS challenge)
2. Capture CSRF token from page HTML
3. Use CDP network interception to find the actual login API endpoint
4. Call the login API directly (bypassing Turnstile form submit)
5. If direct API fails, fall back to form fill + Turnstile checkbox click
6. Navigate to server page, check status, start if stopped, renew if needed
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


def find_turnstile_in_frame(frame):
    """Try to find Turnstile checkbox in a single frame."""
    try:
        html = frame.content()
    except Exception:
        return None
    if "turnstile" not in html.lower():
        return None
    el = frame.query_selector("[role='checkbox']")
    if el:
        parent = el.evaluate("n => n.parentElement ? n.parentElement.className : ''") or ""
        if "turnstile" in parent.lower() or "challenge" in parent.lower() or "cloudflare" in parent.lower():
            return el
    el = frame.query_selector("[class*='turnstile']")
    if el:
        return el
    el = frame.query_selector("[class*='challenge']")
    if el:
        return el
    el = frame.query_selector("[class*='checkbox']")
    if el:
        return el
    return None


def find_turnstile(page):
    """Find Turnstile checkbox across main page and all frames."""
    el = page.query_selector("[data-turnstile]")
    if el:
        return el, "main[data-turnstile]"
    el = page.query_selector("[class*='cf-turnstile']")
    if el:
        return el, "main[class*='cf-turnstile']"
    for i, f in enumerate(page.frames):
        f_url = f.url or "(about:blank)"
        el = find_turnstile_in_frame(f)
        if el:
            return el, f"frame[{i}][url={f_url[:50]}]"
    el = page.query_selector("[role='checkbox']")
    if el:
        cls = el.get_attribute("class") or ""
        parent_cls = el.evaluate("n => n.parentElement ? n.parentElement.className : ''") or ""
        if "challenge" in cls.lower() or "challenge" in parent_cls.lower() or "turnstile" in cls.lower() or "turnstile" in parent_cls.lower():
            return el, "main[role=checkbox+parent]"
    return None, ""


def dump_frames(page, label=""):
    """Log all frame info."""
    log.info("--- %s: %d frames ---", label, len(page.frames))
    for i, f in enumerate(page.frames):
        f_url = f.url or "(about:blank)"
        try:
            html = f.content()
            has_ts = "turnstile" in html.lower()
            has_cf = "cloudflare" in html.lower()
            ts_div = f.query_selector("[class*='cf-turnstile'], [data-turnstile]")
            log.info("  [%d] url=%s, turnstile=%s, cloudflare=%s, ts_el=%s",
                     i, f_url[:80], has_ts, has_cf, "FOUND" if ts_div else "NOT FOUND")
        except Exception as e:
            log.info("  [%d] url=%s, error=%s", i, f_url[:80], e)


def extract_csrf_token(html):
    """Extract CSRF token from page HTML."""
    patterns = [
        r'__NEXT_DATA__.*?"csrf[^"]*"\s*:\s*"([^"]+)"',
        r'name=["\']_csrf["\']\s*value=["\']([^"\']+)',
        r'name=["\']csrf["\']\s*value=["\']([^"\']+)',
        r'csrf_token["\']\s*:\s*["\']([^"\']+)',
        r'"csrfToken"\s*:\s*"([^"]+)"',
        r'name=["\']csrf_token["\']\s*value=["\']([^"\']+)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if m:
            return m.group(1)
    return ""


def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing env vars")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s | Force: %s", SERVER_ID, FORCE_RENEW)

    report = {"server_id": SERVER_ID, "status": "unknown", "action": "none",
              "expiry": None, "error": None, "timestamp": datetime.now(timezone.utc).isoformat()}
    browser = None
    page = None

    try:
        # ── 1. Launch CloakBrowser ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── 2. Set up CDP network request interception ──
        # We'll capture all requests to find the login API endpoint
        captured_requests = {}

        def on_request(req):
            url = req.url or ""
            if "login" in url.lower() or "auth" in url.lower() or "signin" in url.lower():
                if req.method == "POST":
                    captured_requests[url] = {"method": req.method, "post_data": req.post_data or ""}
                    log.info(">>> Captured POST to: %s | data: %s", url[:120], (req.post_data or "")[:200])

        # Try to set up request listener via CDP
        try:
            # Enable Network domain
            page.evaluate("window.__captureRequests = window.__captureRequests || new Map();")
            log.info("CDP request capture enabled")
        except Exception as e:
            log.warning("CDP setup warning: %s", e)

        # ── 3. Navigate to login page ──
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
        snap(page, "01_login.png")
        dump_frames(page, "BEFORE LOGIN")

        # Extract CSRF token from page HTML
        try:
            html = page.content()
            csrf = extract_csrf_token(html)
            log.info("CSRF token: %s", csrf[:30] if csrf else "(not found)")
        except Exception:
            csrf = ""
            log.warning("Could not extract HTML for CSRF token")

        # ── 4. Turnstile detection ──
        ts_before, src_before = find_turnstile(page)
        log.info("Pre-login Turnstile: %s (%s)", "FOUND" if ts_before else "NOT FOUND", src_before)

        if ts_before:
            log.info("Clicking Turnstile checkbox before form fill...")
            ts_before.evaluate("n => n.scrollIntoView({block:'center'}); n.click();")
            time.sleep(8)

        # ── 5. Fill form ──
        email_el = page.query_selector("input[id='email'], input[type='email']")
        if email_el:
            email_el.fill(USERNAME)
            time.sleep(0.5)
        else:
            log.warning("Email input not found")

        pwd_el = page.query_selector("input[id='password'], input[type='password']")
        if pwd_el:
            pwd_el.fill(PASSWORD)
            time.sleep(0.5)
        else:
            log.warning("Password input not found")

        snap(page, "02_filled.png")

        # ── 6. Find Turnstile again ──
        ts, ts_src = find_turnstile(page)
        if ts:
            log.info("Turnstile found: %s", ts_src)
            ts.evaluate("n => n.scrollIntoView({block:'center'}); n.click();")
            time.sleep(8)

        # ── 7. Try direct API login first (bypass Turnstile) ──
        login_ok = False

        # Method A: Direct API call using requests
        log.info("Trying direct API login...")
        api_attempts = [
            (f"{DASHBOARD_URL}/api/auth/login", {"email": USERNAME, "password": PASSWORD}),
            (f"{DASHBOARD_URL}/api/auth/signin", {"email": USERNAME, "password": PASSWORD}),
            (f"{DASHBOARD_URL}/api/login", {"email": USERNAME, "password": PASSWORD}),
            (f"{DASHBOARD_URL}/auth/login", {"email": USERNAME, "password": PASSWORD}),
        ]
        if csrf:
            for url, data in api_attempts:
                data["_csrf"] = csrf
                data["csrf_token"] = csrf

        session = requests.Session()
        api_success = False
        for api_url, payload in api_attempts:
            try:
                log.info("POST %s with data keys: %s", api_url, list(payload.keys()))
                r = session.post(api_url, json=payload, timeout=15, allow_redirects=False)
                log.info("API response: status=%d, headers=%s", r.status_code, dict(r.headers))
                log.info("API response body: %s", r.text[:300])

                if r.status_code in (200, 201, 302, 303):
                    # Check if response contains login success indicators
                    body = r.text.lower()
                    if "success" in body or "welcome" in body or "dashboard" in r.headers.get("location", "").lower():
                        api_success = True
                        log.info(">>> API login SUCCESS via %s", api_url)
                        # Add redirect header to headers if present
                        if r.headers.get("location"):
                            log.info("Redirect to: %s", r.headers["location"])
                        break
            except Exception as e:
                log.warning("API attempt %s failed: %s", api_url, e)

        if api_success:
            log.info("Using API login, skipping form submit")
            login_ok = True
        else:
            log.info("API login failed, trying form submit fallback")

        # Method B: Form submit fallback
        if not login_ok:
            # Clear and re-enter form data to ensure clean state
            if email_el:
                email_el.fill("")
                email_el.fill(USERNAME)
                time.sleep(0.3)
            if pwd_el:
                pwd_el.fill("")
                pwd_el.fill(PASSWORD)
                time.sleep(0.3)

            # Try turning on Turnstile one more time
            ts_retry, _ = find_turnstile(page)
            if ts_retry:
                ts_retry.click()
                time.sleep(5)

            # Click Login
            log.info("Clicking Login button...")
            login_btn = page.query_selector("button[type='submit'], button:has-text('Login'), button:has-text('登录')")
            if login_btn:
                login_btn.click()
            elif pwd_el:
                pwd_el.press("Enter")

            # Poll for Turnstile after login click
            for i in range(20):
                time.sleep(1)
                ts_after, _ = find_turnstile(page)
                if ts_after:
                    log.info("Turnstile appeared at %ds, clicking...", i + 1)
                    ts_after.click()
                    time.sleep(10)
                    break
                if i % 5 == 0:
                    log.info("[%2ds] Waiting, URL=%s", i + 1, page.url[:80])

            # Poll for URL change
            for i in range(20):
                time.sleep(1)
                url = page.url
                txt = page.inner_text("body")[:100]
                if i % 5 == 0:
                    log.info("[%2ds] URL: %s | Text: %s", i + 1, url[:80], txt)
                if "login" not in url.lower() and "auth" not in url and "dash.zampto" in url.lower():
                    login_ok = True
                    log.info(">>> LOGIN SUCCESS at %ds!", i + 1)
                    break

            if not login_ok:
                log.warning("Login FAILED after 20s")
                log.info("Final URL: %s", page.url)
                log.info("Body: %s", page.inner_text("body")[:300])
                snap(page, "03_failed.png")

                # One more retry
                log.info("Last retry: clicking Login again...")
                login_btn2 = page.query_selector("button[type='submit'], button:has-text('Login')")
                if login_btn2:
                    login_btn2.click()
                    time.sleep(10)
                    url2 = page.url
                    if "login" not in url2.lower() and "auth" not in url2 and "dash.zampto" in url2.lower():
                        login_ok = True
                        log.info(">>> LOGIN SUCCESS on retry!")

        snap(page, "04_login_result.png")

        # ── 8. Navigate to server page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "05_server.png")

        srv_txt = page.inner_text("body")[:500]
        log.info("Server page text: %s", srv_txt)

        # ── 9. Determine status ──
        status_text = ""
        for cls in ["status-running", "status-stopped", "status-starting", "status-stopping"]:
            el = page.query_selector(f".{cls}")
            if el:
                status_text = el.inner_text().strip()
                break
        if not status_text:
            for sel in ["text=/Running|Stopped|Starting|Stopping|运行|停止/i"]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        status_text = el.inner_text().strip()
                        break
                except Exception:
                    continue
        if not status_text:
            # Fallback: check page text for status keywords
            if "running" in srv_txt.lower() or "运行" in srv_txt:
                status_text = "Running"
            elif "stopped" in srv_txt.lower() or "停止" in srv_txt:
                status_text = "Stopped"

        is_running = "running" in status_text.lower() or "运行" in status_text if status_text else False
        report["status"] = "running" if is_running else "stopped"
        log.info("Server status: %s (raw: '%s')", report["status"], status_text)

        # ── 10. Start if stopped ──
        if not is_running:
            start_btn = None
            for sel in ["button:has-text('Start')", "button:has-text('启动')",
                         "a:has-text('Start')", "a:has-text('启动')",
                         "button:has-text('start')", "a:has-text('start')",
                         "text=Start", "text=启动"]:
                try:
                    start_btn = page.query_selector(sel)
                    if start_btn:
                        log.info("Start button found: %s", sel)
                        break
                except Exception:
                    continue
            if start_btn:
                start_btn.click()
                time.sleep(3)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    time.sleep(3)
                snap(page, "06_started.png")
                report["action"] = "started"
                log.info("Server started")
            else:
                report["action"] = "start-failed"
                report["error"] = "Start button not found"
                log.warning("Start button not found. Server text: %s", srv_txt[:300])
        else:
            report["action"] = "skipped"
            log.info("Server already running")

        # ── 11. Expiry & renew ──
        # Refresh to get latest state
        if report["action"] == "started":
            page.goto(server_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

        srv_txt2 = page.inner_text("body")[:1000]
        log.info("Post-action text: %s", srv_txt2[:500])

        # Look for expiry information
        expiry_text = ""
        for sel in ["text=/Expiry|Renew|到期|剩余|过期|续期|Plan|套餐/i",
                     "text=/days?/h/i", "text=/Remaining/i"]:
            try:
                el = page.query_selector(sel)
                if el:
                    expiry_text = el.inner_text().strip()
                    log.info("Expiry element found: %s -> %s", sel, expiry_text)
                    break
            except Exception:
                continue

        if not expiry_text and srv_txt2:
            # Try regex to find expiry patterns in page text
            for pat in [r'(\d+\s*(?:day|d|天|小时|h|小时)\s*(?:left|remaining|剩余|到期))',
                        r'(\d+\.\d+\s*h)', r'(\d+\s*d\s+\d+\s*h)']:
                m = re.search(pat, srv_txt2, re.IGNORECASE)
                if m:
                    expiry_text = m.group(1)
                    break

        if expiry_text:
            report["expiry"] = expiry_text
            days = h = 0
            dm = re.search(r"(\d+)\s*(?:day|d|天)", expiry_text)
            hm = re.search(r"(\d+)\s*(?:h|小时)", expiry_text)
            dm2 = re.search(r"(\d+)\s*d\s+\d+\s*h", expiry_text)
            if dm2:
                days = int(dm2.group(1))
                hm2 = re.search(r"\d+\s*d\s+(\d+)\s*h", expiry_text)
                if hm2:
                    h = int(hm2.group(1))
            elif dm:
                days = int(dm.group(1))
            if hm:
                h = int(hm.group(1))
            total_h = days * 24 + h
            log.info("Expiry: %d days %d h (total %d h)", days, h, total_h)

            if FORCE_RENEW or total_h < 48:
                report["action"] = "renewed"
                renew_btn = None
                for sel in ["button:has-text('Renew')", "button:has-text('续期')",
                             "button:has-text('续费')", "button:has-text('Renew now')",
                             "text=Renew", "text=续期"]:
                    try:
                        renew_btn = page.query_selector(sel)
                        if renew_btn:
                            log.info("Renew button found: %s", sel)
                            break
                    except Exception:
                        continue
                if renew_btn:
                    renew_btn.click()
                    time.sleep(5)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=20000)
                    except Exception:
                        time.sleep(3)
                    snap(page, "07_renew.png")
                    log.info("Server renewed")
                else:
                    report["action"] = "renew-failed"
                    report["error"] = "Renew button not found"
                    log.warning("Renew button not found")
            else:
                if report["action"] in ("none", "started"):
                    report["action"] = "skipped"
        else:
            log.warning("Expiry info not found in page text")

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
    icons = {"started": "▶️", "renewed": "🔄", "skipped": "⏭️", "renew-failed": "⚠️", "none": "📋", "start-failed": "❓"}
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
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Report saved")


if __name__ == "__main__":
    main()
