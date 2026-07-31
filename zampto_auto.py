#!/usr/bin/env python3
"""Zampto Auto Renewal - CloakBrowser + Turnstile API approach."""

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

# ── Collect network requests ──
captured = {"login_post": None, "responses": []}


def capture_requests(browser):
    """Use CDP to capture network requests during login."""
    for ctx in browser.contexts:
        for page in ctx.pages:
            page.expose_function("capture", lambda *args: captured.update(args[0] if args else {}))
            page.add_init_script("""
window.__capture = {};
const origFetch = window.fetch;
window.fetch = function(url, opts) {
    window.__capture.lastUrl = url;
    window.__capture.lastOpts = opts;
    return origFetch.apply(this, arguments).then(r => {
        window.__capture.lastResponse = {ok: r.ok, status: r.status, url: r.url};
        return r;
    });
}
""")


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


def snap(page, name, path="./screenshots"):
    os.makedirs(path, exist_ok=True)
    fp = os.path.join(path, name)
    page.screenshot(path=fp)
    log.info("Screenshot: %s", fp)
    return fp


def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing env vars: ZAMPTO_USERNAME, ZAMPTO_PASSWORD, ZAMPTO_SERVER_ID")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s  |  Force: %s", SERVER_ID, FORCE_RENEW)

    report = {
        "server_id": SERVER_ID, "status": "unknown", "action": "none",
        "expiry": None, "error": None, "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    browser = None

    try:
        # ── 1. Launch CloakBrowser ──
        log.info("Launching CloakBrowser (headless)")
        proxy = "socks5://127.0.0.1:1080" if os.getenv("HY2_CONFIG", "") else None
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()
        capture_requests(browser)

        # ── 2. Navigate to login ──
        log.info("Navigating to %s/auth/login", DASHBOARD_URL)
        page.goto(f"{DASHBOARD_URL}/auth/login", wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)  # Let Turnstile load fully
        snap(page, "01_login.png")

        # ── 3. Inject monitoring script ──
        page.add_init_script("""
(function(){
    window.__loginUrl = null;
    window.__loginBody = null;
    window.__loginHeaders = {};
    const origFetch = window.fetch;
    window.fetch = function(url, opts) {
        if (url && typeof url === 'string') {
            window.__loginUrl = url;
            window.__loginBody = opts && opts.body;
            window.__loginHeaders = {};
            if (opts && opts.headers) {
                if (opts.headers instanceof Headers) {
                    opts.headers.forEach((v,k) => window.__loginHeaders[k] = v);
                } else {
                    for (const [k,v] of Object.entries(opts.headers)) {
                        window.__loginHeaders[k] = v;
                    }
                }
            }
        }
        return origFetch.apply(this, arguments);
    };

    // Also capture form submissions
    document.addEventListener('submit', function(e) {
        const form = e.target;
        if (!form) return;
        const formData = new FormData(form);
        const entries = {};
        for (const [k,v] of formData.entries()) entries[k] = v;
        window.__formSubmitted = {url: form.action, data: entries};
    }, true);
})();
""")

        # ── 4. Fill login form ──
        log.info("Filling login form...")
        # Email
        email_el = page.query_selector("input[id='email'], input[type='email']")
        if email_el:
            email_el.fill(USERNAME)
            log.info("Email filled")
            time.sleep(1)

        # Password
        pwd_el = page.query_selector("input[id='password'], input[type='password']")
        if pwd_el:
            pwd_el.fill(PASSWORD)
            log.info("Password filled")
            time.sleep(1)

        # ── 5. Click Login ──
        log.info("Clicking Login button")
        login_btn = page.query_selector("button[type='submit']")
        if login_btn:
            login_btn.click()
        else:
            pwd_el.press("Enter")

        # ── 6. Wait for Turnstile + capture ──
        log.info("Waiting for Turnstile to load...")
        time.sleep(8)

        # Try to find Turnstile iframe
        for attempt in range(3):
            cf_iframe = page.query_selector("iframe[src*='challenges.cloudflare.com']")
            if cf_iframe:
                log.info("Turnstile iframe found (attempt %d)", attempt + 1)
                try:
                    cf_frame = cf_iframe.content_frame()
                    if cf_frame:
                        # Click checkbox
                        checkbox = cf_frame.query_selector("[role='checkbox']")
                        if not checkbox:
                            checkbox = cf_frame.query_selector("[class*='checkbox'], [class*='challenge']")
                        if checkbox:
                            log.info(">>> Clicking Turnstile checkbox")
                            checkbox.evaluate("node => node.click()")
                            time.sleep(15)  # Wait for Cloudflare to verify
                            log.info("Turnstile checkbox clicked, waiting for verification...")
                            break
                        else:
                            log.info("No checkbox found in Turnstile frame")
                except Exception as e:
                    log.warning("Turnstile iframe access failed: %s", e)
            else:
                log.info("Turnstile iframe not found (attempt %d)", attempt + 1)
                time.sleep(3)

        # ── 7. Try clicking Login again ──
        log.info("Clicking Login again")
        lb2 = page.query_selector("button[type='submit']")
        if lb2:
            lb2.click()
        else:
            # Try clicking any Login button
            page.evaluate("() => { const btns = document.querySelectorAll('button'); for (const b of btns) { if (b.textContent.includes('Login')) { b.click(); break; } } }")

        # ── 8. Wait and check captured data ──
        time.sleep(5)
        page.wait_for_load_state("domcontentloaded", timeout=20000)

        # Check what API call was made
        capture_result = page.evaluate("() => ({url: window.__loginUrl, body: window.__loginBody, headers: window.__loginHeaders, formSubmitted: window.__formSubmitted}))")
        log.info("Captured request: %s", json.dumps(capture_result, default=str)[:500])

        snap(page, "02_post_login.png")
        url_now = page.url
        txt_now = page.inner_text("body")[:300]
        log.info("Post-login URL: %s", url_now)
        log.info("Post-login text: %s", txt_now)

        login_ok = ("login" not in url_now.lower() and "Welcome Back" not in txt_now and "security verification" not in txt_now)

        if login_ok:
            log.info(">>> LOGIN SUCCESS! <<<")
            snap(page, "02_login_success.png")
        else:
            log.warning("Login FAILED — still on login page")
            # Try one more approach: click Login with longer wait after Turnstile
            log.info("Trying alternative: waiting longer for Turnstile auto-solve...")
            time.sleep(15)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            snap(page, "02_retry.png")
            url2 = page.url
            txt2 = page.inner_text("body")[:200]
            log.info("Retry URL: %s | Text: %s", url2, txt2)
            login_ok = ("login" not in url2.lower() and "Welcome Back" not in txt2)

        if not login_ok:
            log.warning("Login still failed after retry")

        # ── 9. Go to server detail page ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Navigating to server: %s", server_url)
        page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(2)
        snap(page, "03_server.png")

        # ── 10. Check server status ──
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
            log.info("Server stopped, trying to Start")
            page_text = page.inner_text("body")[:500]
            log.info("Page text: %s", page_text)
            start_btn = None
            for sel in ["button:has-text('Start')", "button:has-text('start')",
                         "a:has-text('Start')", "div:has-text('Start')", "text=Start"]:
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
                snap(page, "04_started.png")
                report["action"] = "started"
            else:
                report["action"] = "start-failed"
                report["error"] = "Start button not found"

        # ── 12. Check expiry & renew ──
        expiry_el = None
        for sel in ["text=/Expiry|Renew|到期|剩余/i", "text=/Expire|过期/i",
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
            days = h = m = 0
            dm = re.search(r"(\d+)\s*day", expiry_text)
            hm = re.search(r"(\d+)\s*h", expiry_text)
            mm = re.search(r"(\d+)\s*m", expiry_text)
            if dm: days = int(dm.group(1))
            if hm: h = int(hm.group(1))
            if mm: m = int(mm.group(1))
            total_h = days * 24 + h
            log.info("Expiry: %d days %d h (total %d h)", days, h, total_h)

            if FORCE_RENEW or total_h < 48:
                log.info("Initiating renewal")
                report["action"] = "renewed"
                renew_btn = None
                for sel in ["button:has-text('Renew')", "button:has-text('续期')",
                             "button:has-text('续费')", "text=Renew", "text=续期"]:
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
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    snap(page, "05_renew.png")
                else:
                    report["action"] = "renew-failed"
                    report["error"] = "Renew button not found"
            else:
                log.info("No renewal needed (total_h=%d)", total_h)
                report["action"] = "skipped"
        else:
            report["error"] = "Expiry element not found"
            log.info("Page text (800): %s", page.inner_text("body")[:800])

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
