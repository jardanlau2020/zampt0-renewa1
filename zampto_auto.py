#!/usr/bin/env python3
"""Zampto Auto Renewal - requests-based login with CSRF + fake Turnstile.

Discovered API endpoints from previous runs:
- POST /api/login → 403 "Invalid CSRF token" (but endpoint EXISTS)
- POST /api/auth/login → 400 "Security verification failed"
- POST /auth/login → 200 (just re-renders login page)

Strategy:
1. Use CloakBrowser to load login page and get initial cookies + CSRF token
2. Use requests.Session (sharing cookies from browser) to POST /api/login
   with form-encoded data including csrf_token + fake cf-turnstile-response
3. If that fails, POST /api/auth/login with Turnstile token
4. If both fail, fall back to browser form click
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


def extract_csrf_from_html(html):
    """Extract CSRF token from page HTML."""
    for pat in [
        r'name=["\']_csrf["\']\s*value=["\']([^"\']+)',
        r'name=["\']csrf_token["\']\s*value=["\']([^"\']+)',
        r'"csrfToken"\s*:\s*"([^"]+)"',
        r'csrfToken["\']\s*:\s*["\']([^"\']+)',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            return m.group(1)
    return ""


def extract_csrf_from_cookies(cookies):
    """Extract CSRF token from cookie list."""
    for c in cookies:
        if "csrf" in c.get("name", "").lower():
            return c["value"]
    return ""


def make_turnstile_token():
    """Generate a plausible-looking Cloudflare Turnstile response token."""
    import base64
    random_data = os.urandom(48).hex()
    token_body = base64.urlsafe_b64encode(
        f"fake::{random_data}::{time.time()}".encode()
    ).decode().rstrip("=")
    return f"0.{token_body}"


def sync_browser_cookies_to_session(page, session):
    """Copy cookies from Playwright browser context to requests.Session."""
    try:
        pw_cookies = page.context.cookies()
        session.cookies.clear()
        for c in pw_cookies:
            session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                secure=c.get("secure", False),
                expires=c.get("expires"),
            )
        log.info("Synced %d cookies from browser to requests.Session", len(pw_cookies))
    except Exception as e:
        log.warning("Failed to sync cookies: %s", e)


def try_api_login(session, csrf_token):
    """Try POST to /api/login with CSRF + fake Turnstile token."""
    turnstile = make_turnstile_token()

    # Try form-encoded (matching what the browser form sends)
    form_data = {
        "email": USERNAME,
        "password": PASSWORD,
        "csrf_token": csrf_token,
        "cf-turnstile-response": turnstile,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{DASHBOARD_URL}/auth/login",
        "Origin": DASHBOARD_URL,
    }

    log.info("Trying POST %s/api/login (form-encoded)", DASHBOARD_URL)
    try:
        r = session.post(f"{DASHBOARD_URL}/api/login", data=form_data, headers=headers, timeout=15)
        log.info("Status: %d | Body: %s", r.status_code, r.text[:300])
        # Sync cookies back
        for c in r.cookies:
            session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)

        if r.status_code == 200:
            body = r.text.lower()
            if "success" in body or "welcome" in body or "dashboard" in body:
                return True, "api/login 200"
            # Check if it's JSON with error
            try:
                data = r.json()
                if "error" in data:
                    log.warning("/api/login error: %s", data["error"])
                else:
                    return True, "api/login 200 (no error)"
            except Exception:
                return True, "api/login 200 (HTML)"
        elif r.status_code == 403:
            log.warning("/api/login 403 - CSRF/turnstile rejected")
        elif r.status_code == 400:
            log.warning("/api/login 400 - bad request")
    except Exception as e:
        log.warning("/api/login failed: %s", e)

    # Try JSON variant
    headers_json = {
        "User-Agent": headers["User-Agent"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{DASHBOARD_URL}/auth/login",
        "Origin": DASHBOARD_URL,
    }
    headers_json["Content-Type"] = "application/json"
    log.info("Trying POST %s/api/login (JSON)", DASHBOARD_URL)
    try:
        r = session.post(f"{DASHBOARD_URL}/api/login", json=form_data, headers=headers_json, timeout=15)
        log.info("Status: %d | Body: %s", r.status_code, r.text[:300])
        for c in r.cookies:
            session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)

        if r.status_code == 200:
            return True, "api/login JSON 200"
        elif r.status_code == 403:
            log.warning("/api/login JSON 403")
        else:
            log.warning("/api/login JSON %d", r.status_code)
    except Exception as e:
        log.warning("/api/login JSON failed: %s", e)

    # Try /api/auth/login
    log.info("Trying POST %s/api/auth/login", DASHBOARD_URL)
    try:
        r = session.post(f"{DASHBOARD_URL}/api/auth/login", data=form_data, headers=headers, timeout=15)
        log.info("Status: %d | Body: %s", r.status_code, r.text[:300])
        for c in r.cookies:
            session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)

        if r.status_code == 200:
            return True, "api/auth/login 200"
        elif r.status_code == 400:
            log.warning("/api/auth/login 400: %s", r.text[:200])
    except Exception as e:
        log.warning("/api/auth/login failed: %s", e)

    return False, "all API endpoints failed"


def try_fetch_login(page):
    """Use browser's own fetch API to submit login with fake Turnstile token."""
    turnstile = make_turnstile_token()

    js = f"""
    (function() {{
      return new Promise((resolve) => {{
        var token = '{turnstile}';
        var formData = new FormData();
        formData.append('email', '{USERNAME}');
        formData.append('password', '{PASSWORD}');

        // Find CSRF token from hidden input or cookie
        var csrf = '';
        var hiddenInputs = document.querySelectorAll('input[type="hidden"]');
        hiddenInputs.forEach(function(inp) {{
          if (inp.name && inp.name.toLowerCase().includes('csrf')) {{
            csrf = inp.value;
          }}
        }});

        if (csrf) formData.append('csrf_token', csrf);
        formData.append('cf-turnstile-response', token);

        // Try API endpoint
        fetch('{DASHBOARD_URL}/api/login', {{
          method: 'POST',
          body: formData,
          credentials: 'include',
        }}).then(function(r) {{
          return r.text().then(function(t) {{
            console.log('API/login status:', r.status);
            console.log('API/login body:', t.substring(0, 500));
            resolve(JSON.stringify({{status: r.status, body: t.substring(0, 500),
              location: r.headers.get('location') || ''}}));
          }});
        }}).catch(function(e) {{
          console.log('API/login error:', e.message);
          resolve(JSON.stringify({{status: -1, error: e.message}}));
        }});
      }});
    }})();
    """
    try:
        result = page.evaluate(js)
        log.info("Fetch login result: %s", result)
        return result
    except Exception as e:
        log.warning("Fetch login failed: %s", e)
        return ""


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
    page = None

    try:
        # ── 1. Launch CloakBrowser ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── 2. Load login page to get cookies + CSRF ──
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
        snap(page, "01_login.png")

        # Extract CSRF
        csrf = ""
        html = page.content()
        csrf = extract_csrf_from_html(html)
        if not csrf:
            csrf = extract_csrf_from_cookies(page.context.cookies())
        log.info("CSRF token: %s", csrf[:30] if csrf else "(not found)")

        # ── 3. Sync cookies and try API login ──
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/125.0.0.0 Safari/537.36",
        })
        sync_browser_cookies_to_session(page, session)

        login_ok = False
        if csrf:
            ok, msg = try_api_login(session, csrf)
            log.info("API login result: %s (%s)", "SUCCESS" if ok else "FAILED", msg)
            login_ok = ok

        # ── 4. If API failed, try browser fetch ──
        if not login_ok:
            log.info("Trying browser-based fetch login...")
            fetch_result = try_fetch_login(page)
            if fetch_result:
                try:
                    data = json.loads(fetch_result)
                    status = data.get("status", -1)
                    if status == 200:
                        log.info(">>> Browser fetch login SUCCESS!")
                        login_ok = True
                except json.JSONDecodeError:
                    pass

        # ── 5. If still failed, try browser form click ──
        if not login_ok:
            log.info("API + fetch failed. Trying browser form click...")

            # Fill form
            email_el = page.query_selector("input[id='email'], input[type='email']")
            pwd_el = page.query_selector("input[id='password'], input[type='password']")
            if email_el:
                email_el.fill(USERNAME)
                time.sleep(0.3)
            if pwd_el:
                pwd_el.fill(PASSWORD)
                time.sleep(0.3)

            # Inject Turnstile bypass
            bypass_js = """
            (function() {
              function setTurnstile() {
                var token = '0.' + btoa(Math.random().toString(36) + Date.now());
                var inputs = document.querySelectorAll('input[name*="turnstile"], input[name*="cf-turnstile"]');
                inputs.forEach(function(i) {
                  i.value = token;
                  i.dispatchEvent(new Event('change', {bubbles:true}));
                });
                var divs = document.querySelectorAll('[data-turnstile], [class*="cf-turnstile"]');
                divs.forEach(function(d) { d.setAttribute('data-turnstile-response', token); });
              }
              setTurnstile();
              setInterval(setTurnstile, 500);
            })();
            """
            try:
                page.evaluate(bypass_js)
                log.info("Turnstile bypass JS injected")
            except Exception as e:
                log.warning("Bypass JS failed: %s", e)
            time.sleep(2)

            # Click Login
            login_btn = page.query_selector("button[type='submit'], button:has-text('Login')")
            if login_btn:
                login_btn.click()
            elif pwd_el:
                pwd_el.press("Enter")

            # Poll for redirect
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

        snap(page, "02_login_result.png")
        log.info("Login final URL: %s", page.url)

        # ── 6. Navigate to server page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        snap(page, "03_server.png")

        srv_txt = page.inner_text("body")[:800]
        log.info("Server page text: %s", srv_txt)

        # Check if redirected back to login
        if "login" in page.url.lower() or "auth" in page.url or "Welcome Back" in srv_txt:
            report["status"] = "unknown"
            report["action"] = "login-failed"
            report["error"] = "Redirected back to login - authentication did not succeed"
            log.error("Login failed - server page is still login page")
        else:
            # ── 7. Status ──
            status_text = ""
            for cls in ["status-running", "status-stopped", "status-starting", "status-stopping"]:
                el = page.query_selector(f".{cls}")
                if el:
                    status_text = el.inner_text().strip()
                    break
            if not status_text:
                try:
                    el = page.query_selector("text=/Running|Stopped|Starting|Stopping/i")
                    if el:
                        status_text = el.inner_text().strip()
                except Exception:
                    pass
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
                start_btn = None
                for sel in [
                    "button:has-text('Start')", "button:has-text('start')",
                    "a:has-text('Start')", "a:has-text('start')",
                ]:
                    start_btn = page.query_selector(sel)
                    if start_btn:
                        log.info("Start button: %s", sel)
                        break
                if start_btn:
                    start_btn.click()
                    time.sleep(3)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=20000)
                    except Exception:
                        time.sleep(3)
                    snap(page, "04_started.png")
                    report["action"] = "started"
                else:
                    report["action"] = "start-failed"
                    report["error"] = "Start button not found"
                    log.warning("Start button not found")
            else:
                report["action"] = "skipped"
                log.info("Server running, no action needed")

            # ── 8. Expiry ──
            if report["action"] == "started":
                page.goto(server_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)

            srv_txt2 = page.inner_text("body")[:1000]
            expiry_text = ""
            for sel in [
                "text=/Expiry|Renew|到期|剩余|过期|续期|Plan|套餐/i",
                "text=/\\d+\\s*days?/i",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        expiry_text = el.inner_text().strip()
                        log.info("Expiry: %s", expiry_text)
                        break
                except Exception:
                    continue

            if not expiry_text:
                for pat in [
                    r'(\d+\s*(?:day|d|天)\s*(?:left|remaining|剩余|到期))',
                    r'(\d+\s*d\s+\d+\s*h)',
                ]:
                    m = re.search(pat, srv_txt2, re.IGNORECASE)
                    if m:
                        expiry_text = m.group(1)
                        break

            if expiry_text:
                report["expiry"] = expiry_text
                days = h = 0
                dm2 = re.search(r"(\d+)\s*d\s+(\d+)\s*h", expiry_text)
                if dm2:
                    days, h = int(dm2.group(1)), int(dm2.group(2))
                else:
                    dm = re.search(r"(\d+)\s*(?:day|d|天)", expiry_text)
                    hm = re.search(r"(\d+)\s*(?:h|小时)", expiry_text)
                    if dm: days = int(dm.group(1))
                    if hm: h = int(hm.group(1))
                total_h = days * 24 + h
                log.info("Expiry: %d days %d h (%d h total)", days, h, total_h)

                if FORCE_RENEW or total_h < 48:
                    report["action"] = "renewed"
                    renew_btn = None
                    for sel in ["button:has-text('Renew')", "button:has-text('续期')", "text=Renew"]:
                        renew_btn = page.query_selector(sel)
                        if renew_btn:
                            log.info("Renew button: %s", sel)
                            break
                    if renew_btn:
                        renew_btn.click()
                        time.sleep(5)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=20000)
                        except Exception:
                            time.sleep(3)
                        snap(page, "05_renew.png")
                    else:
                        report["action"] = "renew-failed"
                        report["error"] = "Renew button not found"
                else:
                    if report["action"] in ("none", "started"):
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

    # ── Report ──
    status_icon = "🟢" if report["status"] == "running" else "🔴"
    icons = {
        "started": "▶️", "renewed": "🔄", "skipped": "⏭️",
        "renew-failed": "⚠️", "none": "📋",
        "start-failed": "❓", "login-failed": "🔒",
    }
    body = (
        f"🖥️ **Zampto Server Report**\n\n"
        f"**Server ID:** `{SERVER_ID}`\n"
        f"**Status:** {status_icon} {report['status'].title()}\n"
        f"**Action:** {icons.get(report['action'], '❓')} {report['action']}"
    )
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
