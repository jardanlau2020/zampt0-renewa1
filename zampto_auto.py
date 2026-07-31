#!/usr/bin/env python3
"""Zampto Auto Renewal - CloakBrowser-based login with Turnstile handling.

JS source analysis revealed:
- Login endpoint: POST /api/auth/login with JSON body {email, password, turnstile_token}
- Header: x-device-id (UUID from localStorage)
- Turnstile sitekey: 0x4AAAAAAD5hn7QjjDUPXOcK
- Form submit handler calls fetch('/api/auth/login', ...) with turnstile token
- If n.requires_2fa -> redirect to /auth/login/2fa
- Success -> push to '/' and refresh
"""

import os, re, sys, json, time, logging, uuid
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
TURNSTILE_SITEKEY = "0x4AAAAAAD5hn7QjjDUPXOcK"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto")


def push_tg(title, body):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram not configured")
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


def generate_device_id():
    return uuid.uuid4().hex


def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing env vars")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s | Force: %s", SERVER_ID, FORCE_RENEW)
    device_id = generate_device_id()
    log.info("Device ID: %s", device_id)

    report = {
        "server_id": SERVER_ID, "status": "unknown", "action": "none",
        "expiry": None, "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    browser = None
    page = None

    try:
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # --- STEP 1: Navigate to login page ---
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
        snap(page, "01_login.png")

        # --- STEP 2: Inject Turnstile mock + capture onToken callback ---
        # The JS code creates a TurnstileWidget that calls onToken callback
        # when turnstile.render() succeeds. We mock turnstile.render to
        # immediately call the callback with a fake but plausible token.
        inject_turnstile_mock = f"""
        (function() {{
          var device_id = '{device_id}';
          localStorage.setItem('zampto_device_id', device_id);

          // Pre-create Turnstile mock before the app JS loads
          window.turnstile = {{
            render: function(el, opts) {{
              // Call onToken immediately with a fake token
              if (opts && opts.callback) {{
                var token = '0.' + btoa(JSON.stringify({{
                  host: location.hostname,
                  ts: Date.now(),
                  id: device_id
                }})).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=/g, '');
                opts.callback(token);
                return token;
              }}
              return null;
            }},
            reset: function() {{}},
            remove: function() {{}}
          }};
          window.__zampto_injected_turnstile = true;
          console.log('Turnstile mock injected');
        }})();
        """
        try:
            page.evaluate(inject_turnstile_mock)
            log.info("Turnstile mock injected")
        except Exception as e:
            log.warning("Inject mock failed: %s", e)

        # Give page JS time to initialize and render Turnstile
        time.sleep(3)

        # --- STEP 3: Fill form and submit ---
        log.info("Filling login form...")
        # Fill email
        try:
            email_el = page.query_selector("input[type='email'], input[name='email'], input[id='email']")
            if email_el:
                email_el.fill(USERNAME)
                time.sleep(0.3)
                log.info("Email filled")
            else:
                log.warning("Email input not found")
        except Exception as e:
            log.warning("Email fill failed: %s", e)

        # Fill password
        try:
            pwd_el = page.query_selector("input[type='password'], input[name='password']")
            if pwd_el:
                pwd_el.fill(PASSWORD)
                time.sleep(0.3)
                log.info("Password filled")
            else:
                log.warning("Password input not found")
        except Exception as e:
            log.warning("Password fill failed: %s", e)

        # --- STEP 4: Direct API login via page.evaluate (bypasses form submit) ---
        log.info("Attempting direct API login via page.evaluate...")
        api_login_js = f"""
        (function() {{
          var token = '';
          // Try to get turnstile token from various sources
          if (window.turnstile && window.turnstile.token) {{
            token = window.turnstile.token;
          }}
          // If no token, generate one
          if (!token) {{
            var d = new Date().getTime();
            token = '0.' + btoa(d.toString() + Math.random().toString(36)).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=/g,'');
          }}
          return fetch('{DASHBOARD_URL}/api/auth/login', {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              'x-device-id': '{device_id}'
            }},
            body: JSON.stringify({{
              email: '{USERNAME}',
              password: '{PASSWORD}',
              turnstile_token: token
            }})
          }}).then(function(r) {{
            return r.text().then(function(t) {{
              return JSON.stringify({{
                status: r.status,
                location: r.headers.get('location') || '',
                body: t.substring(0, 800)
              }});
            }});
          }}).catch(function(e) {{
            return JSON.stringify({{status: -1, error: e.message}});
          }});
        }})();
        """
        result = page.evaluate(api_login_js)
        log.info("API login result: %s", result[:500])

        login_ok = False
        try:
            data = json.loads(result)
            if data.get("status") == 200:
                log.info(">>> API login SUCCESS!")
                login_ok = True
            elif data.get("status") == 401 or data.get("status") == 400:
                log.warning("API login rejected (status %d): %s", data.get("status"), data.get("body", "")[:200])
            else:
                log.warning("API login unexpected status: %s", data)
        except json.JSONDecodeError:
            log.warning("Could not parse API response: %s", result[:200])

        # --- STEP 5: If API failed, try clicking Login button ---
        if not login_ok:
            log.info("API login failed. Trying button click...")
            time.sleep(2)

            try:
                login_btn = page.query_selector("button[type='submit'], button:has-text('Login'), button:has-text('Logging')")
                if login_btn:
                    login_btn.click()
                    log.info("Clicked Login button")
                else:
                    # Try Enter on password field
                    try:
                        pwd_el = page.query_selector("input[type='password']")
                        if pwd_el:
                            pwd_el.press("Enter")
                            log.info("Pressed Enter on password field")
                    except Exception as e:
                        log.warning("Enter press failed: %s", e)
            except Exception as e:
                log.warning("Login button click failed: %s", e)

            # Poll for navigation away from login page
            for i in range(30):
                time.sleep(1.5)
                url = page.url
                txt = page.inner_text("body")[:150]
                log.info("[%2ds] URL: %s | Text: %s", i + 2, url[:80], txt)
                if "login" not in url.lower() and "auth" not in url and "dash.zampto" in url.lower() and "Welcome" not in txt:
                    login_ok = True
                    log.info(">>> LOGIN SUCCESS at %ds!", i + 2)
                    break

        snap(page, "02_login_result.png")
        log.info("Login final URL: %s", page.url)

        if not login_ok:
            # --- STEP 6: If still not logged in, try one more time with wait ---
            log.info("Login failed. Attempting second login attempt...")
            time.sleep(2)

            # Try to navigate directly to home which should auto-login if session exists
            page.goto(f"{DASHBOARD_URL}/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            url = page.url
            txt = page.inner_text("body")[:200]
            log.info("Home URL: %s | Text: %s", url, txt)

            if "login" not in url.lower() and "auth" not in url and "Welcome" not in txt:
                login_ok = True
                log.info(">>> Session restored on home page!")
            else:
                report["status"] = "unknown"
                report["action"] = "login-failed"
                report["error"] = "Authentication did not succeed after all attempts"
                log.error("Login failed after all attempts")
                snap(page, "06_final.png")
                _finalize_report(report, body=None)
                return

        # --- STEP 7: Navigate to server page ---
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "03_server.png")

        srv_txt = page.inner_text("body")[:1000]
        log.info("Server page text: %s", srv_txt[:500])

        # --- STEP 8: Check server status and start if needed ---
        if "login" in page.url.lower() or "auth" in page.url or "Welcome Back" in srv_txt:
            report["status"] = "unknown"
            report["action"] = "login-failed"
            report["error"] = "Redirected back to login on server page"
            log.error("Still on login page")
        else:
            status_text = ""
            # Try common class-based status indicators
            for cls in ["status-running", "status-stopped", "status-starting", "status-stopping"]:
                try:
                    el = page.query_selector(f".{cls}")
                    if el:
                        status_text = el.inner_text().strip()
                        break
                except Exception:
                    pass

            # Try text-based search
            if not status_text:
                sl = srv_txt.lower()
                if "running" in sl:
                    status_text = "Running"
                elif "stopped" in sl:
                    status_text = "Stopped"
                elif "starting" in sl:
                    status_text = "Starting"

            is_running = "running" in status_text.lower() if status_text else False
            report["status"] = "running" if is_running else "stopped"
            log.info("Status: %s (raw: '%s')", report["status"], status_text)

            if not is_running:
                # Find and click Start button
                start_btn = None
                for sel in ["button:has-text('Start')", "button:has-text('start')",
                            "a:has-text('Start')", "button:has-text('开机')", "a:has-text('开机')"]:
                    try:
                        start_btn = page.query_selector(sel)
                        if start_btn:
                            log.info("Start button found: %s", sel)
                            break
                    except Exception:
                        pass

                if start_btn:
                    start_btn.click()
                    log.info("Clicking Start...")
                    time.sleep(5)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=20000)
                    except Exception:
                        time.sleep(5)
                    snap(page, "04_started.png")
                    report["action"] = "started"
                    log.info("Server started")
                else:
                    report["action"] = "start-failed"
                    report["error"] = "Start button not found on server page"
                    log.warning("Start button not found")
            else:
                report["action"] = "skipped"
                log.info("Server running, no action needed")

            # --- STEP 9: Check expiry and renew if needed ---
            if report["action"] in ("started", "skipped"):
                srv_txt2 = page.inner_text("body")[:2000]
                log.info("Full server page text for expiry check (first 500): %s", srv_txt2[:500])

                expiry_text = ""
                # Search for expiry-related text
                for pat in [r'(\d+\s*天\s*\d+\s*时)', r'(\d+\s*(?:day|d)\s*(?:left|remaining))',
                            r'(\d+\s*d\s*\d+\s*h)', r'(\d+\s*days?\s*\d+\s*hours?)']:
                    m = re.search(pat, srv_txt2, re.IGNORECASE)
                    if m:
                        expiry_text = m.group(1)
                        break

                # Also try selector-based
                if not expiry_text:
                    for sel in ["text=/Expiry|Renew|到期|剩余|过期|续期|Plan|套餐/i"]:
                        try:
                            el = page.query_selector(sel)
                            if el:
                                expiry_text = el.inner_text().strip()
                                break
                        except Exception:
                            continue

                if expiry_text:
                    report["expiry"] = expiry_text
                    log.info("Expiry found: %s", expiry_text)

                    days = h = 0
                    dm2 = re.search(r"(\d+)\s*d\s+(\d+)\s*h", expiry_text, re.IGNORECASE)
                    if dm2:
                        days, h = int(dm2.group(1)), int(dm2.group(2))
                    else:
                        dm = re.search(r"(\d+)\s*(?:天|day|d)", expiry_text, re.IGNORECASE)
                        hm = re.search(r"(\d+)\s*(?:时|h|hour)", expiry_text, re.IGNORECASE)
                        if dm: days = int(dm.group(1))
                        if hm: h = int(hm.group(1))
                    total_h = days * 24 + h
                    log.info("Expiry: %d days %d h = %d h total", days, h, total_h)

                    if FORCE_RENEW or total_h < 48:
                        log.info("Need to renew (total_h=%d, threshold=48, force=%s)", total_h, FORCE_RENEW)
                        renew_btn = None
                        for sel in ["button:has-text('Renew')", "button:has-text('renew')",
                                    "button:has-text('续期')", "a:has-text('Renew')", "a:has-text('续期')"]:
                            try:
                                renew_btn = page.query_selector(sel)
                                if renew_btn:
                                    log.info("Renew button found: %s", sel)
                                    break
                            except Exception:
                                pass
                        if renew_btn:
                            renew_btn.click()
                            log.info("Clicking Renew...")
                            time.sleep(8)
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=20000)
                            except Exception:
                                time.sleep(5)
                            snap(page, "05_renew.png")
                            report["action"] = "renewed"
                            log.info("Server renewed")
                        else:
                            report["action"] = "renew-failed"
                            report["error"] = "Renew button not found"
                            log.warning("Renew button not found")
                    else:
                        log.info("No renewal needed (expiry: %d days)", days)
                        if report["action"] in ("none",):
                            report["action"] = "skipped"
                else:
                    log.warning("Expiry text not found on server page")
                    if report["action"] == "none":
                        report["action"] = "skipped"

        snap(page, "06_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    # --- Build and send report ---
    status_icon = "\U0001F7E2" if report["status"] == "running" else "\U0001F534"
    icons = {
        "started": "\u25B6\ufe0f", "renewed": "\U0001F504", "skipped": "\u23ED\ufe0f",
        "renew-failed": "\u26A0\ufe0f", "none": "\U0001F4CB",
        "start-failed": "\u2753", "login-failed": "\U0001F512",
    }
    body = (
        f"\U0001F5A5\ufe0f **Zampto Server Report**\n\n"
        f"**Server ID:** `{SERVER_ID}`\n"
        f"**Status:** {status_icon} {report['status'].title()}\n"
        f"**Action:** {icons.get(report['action'], '\u2753')} {report['action']}"
    )
    if report.get("expiry"):
        body += f"\n**Expiry:** {report['expiry']}"
    if report.get("error"):
        body += f"\n**\u26A0\ufe0f Error:** {report['error']}"
    body += f"\n\n_Generated: {report['timestamp']}_"

    log.info("--- Report ---\n%s", body)
    push_tg("\U0001F5A5\ufe0f Zampto Server Report", body)

    os.makedirs("./screenshots", exist_ok=True)
    with open("./screenshots/report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Report saved")


def _finalize_report(report, body=None):
    if body is None:
        status_icon = "\U0001F534"
        body = (
            f"\U0001F5A5\ufe0f **Zampto Server Report**\n\n"
            f"**Server ID:** `{report['server_id']}`\n"
            f"**Status:** {status_icon} {report['status'].title()}\n"
            f"**Action:** \U0001F512 login-failed"
        )
        if report.get("error"):
            body += f"\n**\u26A0\ufe0f Error:** {report['error']}"
        body += f"\n\n_Generated: {report['timestamp']}_"
    log.info("--- Report ---\n%s", body)
    push_tg("\U0001F5A5\ufe0f Zampto Server Report", body)
    os.makedirs("./screenshots", exist_ok=True)
    with open("./screenshots/report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Report saved")


if __name__ == "__main__":
    main()
