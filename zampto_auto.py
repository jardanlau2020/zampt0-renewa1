#!/usr/bin/env python3
"""Zampto Auto Renewal - v4: Intercept Turnstile script, force valid token.

JS source confirmed:
- POST /api/auth/login with JSON {email, password, turnstile_token}
- Header: x-device-id (UUID from localStorage)
- Turnstile sitekey: 0x4AAAAAAD5hn7QjjDUPXOcK
- Form submit handler reads turnstile token from Z.current (set by onToken callback)

Strategy: Use page.route() to intercept Turnstile API script loading,
and replace it with a mock that immediately calls onToken callback.
This bypasses Cloudflare's Turnstile server-side validation by making
the browser think the widget completed successfully.
"""

import os, re, sys, json, time, logging, uuid, requests
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


def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing env vars")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s | Force: %s", SERVER_ID, FORCE_RENEW)

    report = {
        "server_id": SERVER_ID, "status": "unknown", "action": "none",
        "expiry": None, "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    browser = None

    try:
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # --- STEP 1: Intercept Turnstile API script with mock ---
        # When the page tries to load challenges.cloudflare.com/turnstile/v0/api.js,
        # serve our mock instead. The mock sets window.turnstile.render() to
        # immediately call the onToken callback with a fake token.
        turnstile_mock_js = """
        'use strict';
        (function() {
          function genToken() {
            var d = new Date().getTime();
            var id = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
              var r = Math.random() * 16 | 0;
              var v = c === 'x' ? r : (r & 0x3 | 0x8);
              return v.toString(16);
            });
            localStorage.setItem('zampto_device_id', id);
            var raw = JSON.stringify({host: location.hostname, ts: d, id: id});
            return '0.' + btoa(raw).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=/g, '');
          }

          var token = genToken();

          window.turnstile = {
            render: function(container, options) {
              console.log('[ZAMPTO MOCK] Turnstile render called, calling callback with token');
              if (options && typeof options.callback === 'function') {
                options.callback(token);
              }
              return token;
            },
            reset: function(widgetId) {
              console.log('[ZAMPTO MOCK] Turnstile reset');
            },
            remove: function(widgetId) {
              console.log('[ZAMPTO MOCK] Turnstile remove');
            }
          };
          window.__zampto_token = token;
          console.log('[ZAMPTO MOCK] Turnstile mock loaded. Token: ' + token.slice(0, 30) + '...');
        })();
        """

        def handle_turnstile_request(route, request):
            url = request.url
            if 'turnstile' in url.lower() and 'challenges.cloudflare.com' in url.lower():
                log.info("Intercepting Turnstile API: %s", url)
                # Abandon the real request and serve our mock
                route.fulfill(
                    status=200,
                    content=turnstile_mock_js,
                    headers={'Content-Type': 'application/javascript; charset=utf-8'},
                )
            else:
                route.continue_()

        page.route(r'.*turnstile.*', handle_turnstile_request)
        log.info("Turnstile request interception registered")

        # --- STEP 2: Navigate to login page ---
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "01_login.png")

        # --- STEP 3: Check if Turnstile mock was applied ---
        mock_check = page.evaluate("""
        (function() {
          var token = window.__zampto_token || '';
          var ts = typeof window.turnstile === 'object' ? 'yes' : 'no';
          var render = typeof window.turnstile === 'object' && typeof window.turnstile.render === 'function' ? 'yes' : 'no';
          return JSON.stringify({token: token ? token.slice(0,30)+'...' : 'none', turnstile: ts, render: render});
        })()
        """)
        log.info("Turnstile mock status: %s", mock_check)

        # --- STEP 4: Fill form fields ---
        log.info("Filling login form...")
        try:
            email_el = page.query_selector("input[type='email']")
            if email_el:
                email_el.fill(USERNAME)
                log.info("Email filled")
            pwd_el = page.query_selector("input[type='password']")
            if pwd_el:
                pwd_el.fill(PASSWORD)
                log.info("Password filled")
        except Exception as e:
            log.warning("Form fill error: %s", e)

        time.sleep(1)

        # --- STEP 5: Direct API login via page.evaluate ---
        log.info("=== Attempting API login ===")
        api_js = f"""
        (function() {{
          var token = window.__zampto_token || '';
          if (!token) {{
            var d = new Date().getTime();
            token = '0.' + btoa(d.toString()).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=/g,'');
          }}
          var deviceId = localStorage.getItem('zampto_device_id') || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {{
            var r = Math.random() * 16 | 0;
            var v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
          }});
          console.log('[API] Sending login with token: ' + token.slice(0,20) + '...');
          return fetch('{DASHBOARD_URL}/api/auth/login', {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              'x-device-id': deviceId
            }},
            body: JSON.stringify({{
              email: '{USERNAME}',
              password: '{PASSWORD}',
              turnstile_token: token
            }})
          }}).then(function(r) {{
            return r.text().then(function(t) {{
              return JSON.stringify({{status: r.status, body: t.substring(0, 800)}});
            }});
          }}).catch(function(e) {{
            return JSON.stringify({{status: -1, error: e.message}});
          }});
        }})();
        """
        result = page.evaluate(api_js)
        log.info("API response: %s", result[:500])

        login_ok = False
        try:
            data = json.loads(result)
            if data.get("status") == 200:
                login_ok = True
                log.info(">>> API login SUCCESS!")
            else:
                log.warning("API status %d: %s", data.get("status"), data.get("body", "")[:200])
        except json.JSONDecodeError:
            log.warning("Parse error: %s", result[:200])

        # --- STEP 6: Try button click if API failed ---
        if not login_ok:
            log.info("API failed, trying button click...")
            time.sleep(2)
            try:
                btn = page.query_selector("button[type='submit']")
                if btn:
                    btn.click()
                    log.info("Clicked submit button")
            except Exception as e:
                log.warning("Button click error: %s", e)

            for i in range(20):
                time.sleep(1.5)
                url = page.url
                txt = page.inner_text("body")[:150]
                log.info("[%2ds] URL: %s | Text: %s", i + 2, url[:80], txt)
                if "login" not in url.lower() and "auth" not in url:
                    login_ok = True
                    log.info(">>> Login success!")
                    break

        snap(page, "02_login_result.png")

        if not login_ok:
            report["status"] = "unknown"
            report["action"] = "login-failed"
            report["error"] = "Cloudflare Turnstile CAPTCHA cannot be bypassed in headless mode"
            log.error("Login failed - Turnstile token rejected")
            snap(page, "06_final.png")
            _report(report)
            return

        # --- STEP 7: Navigate to server page ---
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to server: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "03_server.png")

        srv_txt = page.inner_text("body")[:1000]
        log.info("Server page: %s", srv_txt[:400])

        if "Welcome Back" in srv_txt or "login" in page.url.lower():
            report["status"] = "unknown"
            report["action"] = "login-failed"
            report["error"] = "Redirected to login on server page"
            snap(page, "06_final.png")
            _report(report)
            return

        # Check status
        is_running = "running" in srv_txt.lower()
        report["status"] = "running" if is_running else "stopped"
        log.info("Server status: %s", report["status"])

        if not is_running:
            for sel in ["button:has-text('Start')", "button:has-text('start')", "button:has-text('开机')"]:
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        log.info("Clicked Start")
                        time.sleep(5)
                        report["action"] = "started"
                        snap(page, "04_started.png")
                        break
                except Exception:
                    continue
            if report["action"] == "none":
                report["action"] = "start-failed"
                report["error"] = "Start button not found"
        else:
            report["action"] = "skipped"

        # Check expiry
        if report["action"] in ("started", "skipped"):
            for pat in [r'(\d+\s*天\s*\d+\s*时)', r'(\d+\s*d\s*\d+\s*h)', r'(\d+\s*days?\s*\d+\s*hours?)']:
                m = re.search(pat, srv_txt, re.IGNORECASE)
                if m:
                    report["expiry"] = m.group(1)
                    break

            if report["expiry"]:
                dm = re.search(r"(\d+)\s*(?:天|day|d)", report["expiry"], re.IGNORECASE)
                hm = re.search(r"(\d+)\s*(?:时|h|hour)", report["expiry"], re.IGNORECASE)
                days = int(dm.group(1)) if dm else 0
                h = int(hm.group(1)) if hm else 0
                total_h = days * 24 + h
                log.info("Expiry: %s = %d days %d h", report["expiry"], days, h)

                if FORCE_RENEW or total_h < 48:
                    for sel in ["button:has-text('Renew')", "button:has-text('renew')", "button:has-text('续期')"]:
                        try:
                            btn = page.query_selector(sel)
                            if btn:
                                btn.click()
                                log.info("Clicked Renew")
                                time.sleep(8)
                                report["action"] = "renewed"
                                snap(page, "05_renew.png")
                                break
                        except Exception:
                            continue
                    if report["action"] not in ("renewed",):
                        report["action"] = "renew-failed"
                        report["error"] = "Renew button not found"
                elif report["action"] == "skipped":
                    pass  # stays skipped

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

    _report(report)


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
