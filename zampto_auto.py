#!/usr/bin/env python3
"""
Zampto Auto Renewal — CloakBrowser-based automation.

Logs in via Logto, checks server status, starts if stopped,
clicks renewal, waits for Cloudflare Turnstile, then pushes
results via Telegram Bot.
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone

import requests
from cloakbrowser import launch

# ── Config ──────────────────────────────────────────────────────────────

USERNAME = os.getenv("ZAMPTO_USERNAME", "")
PASSWORD = os.getenv("ZAMPTO_PASSWORD", "")
SERVER_ID = os.getenv("ZAMPTO_SERVER_ID", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FORCE_RENEW = os.getenv("FORCE_RENEW", "false").lower() == "true"
DASHBOARD_URL = "https://dash.zampto.net"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto")


# ── Telegram Bot ────────────────────────────────────────────────────────

def push_tg(title: str, body: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram Bot not configured, skipping notification")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": f"{title}\n\n{body}",
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log.info("Telegram message sent successfully")
    except Exception as e:
        log.error("Telegram push failed: %s", e)


# ── Helpers ─────────────────────────────────────────────────────────────

def wait_for(page, selector: str, timeout: float = 30.0, label: str = "element"):
    try:
        page.wait_for_selector(selector, timeout=timeout * 1000)
        log.info("Found %s: %s", label, selector)
        return True
    except Exception:
        log.warning("Timeout waiting for %s: %s", label, selector)
        return False


def screenshot(page, name: str, path: str = "./screenshots"):
    os.makedirs(path, exist_ok=True)
    filepath = os.path.join(path, name)
    page.screenshot(path=filepath)
    log.info("Screenshot saved: %s", filepath)
    return filepath


def parse_expiry(text: str):
    """Extract remaining time from a string like '1 day 23h 53m'."""
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
    total_hours = days * 24 + h
    log.info("Parsed expiry: %d days %d hours %d min", days, h, m)
    return days, h, m, total_hours


# ── Main automation ─────────────────────────────────────────────────────

def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing required env vars: ZAMPTO_USERNAME, ZAMPTO_PASSWORD, ZAMPTO_SERVER_ID")
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
        # ── 1. Launch CloakBrowser ──────────────────────────────────────
        log.info("Launching CloakBrowser (headless)")
        proxy = None
        hy2_config = os.getenv("HY2_CONFIG", "")
        if hy2_config:
            log.info("HY2_CONFIG detected, using SOCKS5 proxy via 127.0.0.1:1080")
            proxy = "socks5://127.0.0.1:1080"
        browser = launch(headless=True, proxy=proxy)
        page = browser.new_page()

        # ── 2. Navigate to Zampto Dashboard ─────────────────────────────
        log.info("Navigating to %s", DASHBOARD_URL)
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(3)
        screenshot(page, "01_dashboard.png")

        # ── 3. Detect login page & handle Logto ─────────────────────────
        # ── 3. Detect login page & handle Zampto login ─────────────────
        if "login" in page.url.lower():
            log.info("Login page detected, current URL: %s", page.url)
            screenshot(page, "02_login.png")

            # ── Step A: Fill email ─────────────────────────────────────
            if wait_for(page, "input[name='email'], input[type='email'], input[name='username']",
                        15, "email input"):
                email_input = page.query_selector(
                    "input[name='email'], input[type='email'], input[name='username']")
                email_input.fill(USERNAME)
                log.info("Email filled: %s", USERNAME)
                time.sleep(1)

            # ── Step B: Fill password ──────────────────────────────────
            if wait_for(page, "input[type='password']", 15, "password input"):
                pwd_input = page.query_selector("input[type='password']")
                pwd_input.fill(PASSWORD)
                log.info("Password filled")
                time.sleep(1)

            # ── Step C: Find Login button ──────────────────────────────
            login_selectors = [
                "button:has-text('Login')",
                "button:has-text('login')",
                "input[type='submit']:has-text('Login')",
                "button[type='submit']",
                "text=Login",
                "text=登录",
            ]
            login_btn = None
            for sel in login_selectors:
                try:
                    login_btn = page.query_selector(sel)
                    if login_btn:
                        log.info("Found Login button with selector: %s", sel)
                        break
                except Exception:
                    continue

            # ── Step D: Click Login & wait for Turnstile ───────────────
            if login_btn:
                log.info("Clicking Login button")
                login_btn.click()
            else:
                log.info("Login button not found, pressing Enter on password field")
                pwd_input.press("Enter")

            # ── Step E: Find & solve Turnstile inside iframe ──────────
            log.info("Searching for Turnstile iframe...")
            time.sleep(2)

            # Find the Cloudflare Turnstile iframe
            turnstile_frame = None
            all_frames = page.frames
            for f in all_frames:
                url = f.url or ""
                log.info("  frame url=%s", url)
                if "turnstile" in url.lower() or "cloudflare" in url.lower() or "cf-turnstile" in url.lower():
                    log.info("  >>> Turnstile frame found: url=%s", url)
                    turnstile_frame = f
                    break

            # If no Turnstile frame found, try clicking the iframe element directly
            if not turnstile_frame:
                log.info("No Turnstile frame detected, trying to interact with Turnstile via page...")
                # Try clicking the Turnstile checkbox area
                turnstile_checkbox = page.query_selector(
                    "[class*='challenge-container'], [class*='cloudflare'], "
                    "[class*='cf-turnstile'], .cf-turnstile, "
                    "[data-turnstile], .turnstile-badge, "
                    "[class*='security-verify'], [class*='verification']")
                if turnstile_checkbox:
                    log.info("Found potential Turnstile element, clicking...")
                    turnstile_checkbox.click()
                    time.sleep(5)

                # Alternative: try clicking the iframe itself
                cf_iframe = page.query_selector("iframe[src=''], iframe[srcdoc]")
                if cf_iframe:
                    log.info("Found empty-src iframe, attempting frame access...")
                    try:
                        cf_frame = cf_iframe.content_frame()
                        if cf_frame:
                            log.info("Got content_frame from empty-src iframe")
                            # Try to find and click the checkbox
                            checkbox = cf_frame.query_selector(
                                "[class*='checkbox'], [role='checkbox'], "
                                "input[type='checkbox'], .cf-turnstile-box, "
                                "[class*='challenge']")
                            if checkbox:
                                log.info("Found Turnstile checkbox in iframe, clicking...")
                                checkbox.click()
                                time.sleep(10)
                            else:
                                log.info("No checkbox found in iframe, dumping frame content...")
                                log.info("  frame text: %s", cf_frame.text_content()[:200])
                    except Exception as e:
                        log.warning("Failed to access Turnstile iframe: %s", e)

            # ── Step F: Check result ───────────────────────────────────
            time.sleep(5)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            screenshot(page, "03_post_login.png")

            post_login_url = page.url
            post_login_text = page.inner_text("body")[:300]
            log.info("Post-login URL: %s", post_login_url)
            log.info("Post-login text: %s", post_login_text)

            # Dump full HTML for debugging
            page_html = page.content()
            log.info("[DEBUG] Full HTML length: %d chars", len(page_html))

            if "login" in post_login_url.lower() or "Welcome Back" in post_login_text or "security verification" in post_login_text:
                log.warning("Login failed — still on login page.")
                screenshot(page, "03_login_failed.png")

                # Check if Turnstile token appeared (solved)
                solved = "solved" in page_html.lower() or "cf_turnstile" in page_html.lower()
                log.info("Turnstile solved indicator in HTML: %s", solved)

                # Check for specific error messages
                lower_text = post_login_text.lower()
                if "invalid" in lower_text or "incorrect" in lower_text:
                    log.error("Credentials rejected: %s", post_login_text[:200])
                elif "security verification" in lower_text:
                    log.warning("Turnstile still pending")
                    # Last resort: try clicking the iframe with longer waits
                    for attempt in range(3):
                        log.info("Turnstile retry attempt %d...", attempt + 1)
                        cf_iframe = page.query_selector("iframe[src=''], iframe[srcdoc]")
                        if cf_iframe:
                            try:
                                cf_frame = cf_iframe.content_frame()
                                if cf_frame:
                                    checkbox = cf_frame.query_selector(
                                        "[class*='checkbox'], [role='checkbox'], "
                                        "input[type='checkbox'], .cf-turnstile-box")
                                    if checkbox:
                                        checkbox.click()
                                        time.sleep(8)
                                        login_btn.click()
                                        time.sleep(8)
                                        page.wait_for_load_state("domcontentloaded", timeout=20000)
                                        screenshot(page, f"03_retry_{attempt + 1}.png")
                                        url = page.url
                                        text = page.inner_text("body")[:200]
                                        log.info("  URL: %s | Text: %s", url, text)
                                        if "login" not in url.lower() and "Welcome Back" not in text:
                                            log.info("Login SUCCESS after Turnstile retry!")
                                            screenshot(page, "03_login_success.png")
                                            break
                                    else:
                                        log.info("  No checkbox in iframe")
                            except Exception as e:
                                log.warning("  Retry %d failed: %s", attempt + 1, e)
                        else:
                            log.info("  No Turnstile iframe found")
                        time.sleep(3)

                    # Final check
                    post_login_url2 = page.url
                    post_login_text2 = page.inner_text("body")[:300]
                    if "login" in post_login_url2.lower() or "Welcome Back" in post_login_text2:
                        log.error("Login FAILED after all Turnstile attempts.")
                        report["error"] = "Login failed — Turnstile in srcdoc iframe cannot be solved"
                        screenshot(page, "07_final.png")
                        try:
                            browser.close()
                        except Exception:
                            pass
                        push_tg("🖥️ Zampto Server Report", (
                            f"🖥️ **Zampto Server Report**\n\n"
                            f"**Server ID:** `{SERVER_ID}`\n"
                            f"**Status:** 🔴 Failed\n"
                            f"**Action:** ❌ login-failed\n"
                            f"**⚠️ Error:** Turnstile (Cloudflare) in srcdoc iframe cannot be solved in headless mode.\n"
                            f"Suggestion: add a Turnstile bypass or switch to API-based login.\n\n"
                            f"_Generated: {datetime.now(timezone.utc).isoformat()}_"))
                        return
            else:
                log.info("Login SUCCESS! Dashboard loaded.")
                screenshot(page, "03_login_success.png")

            server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
            page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
            time.sleep(2)
            screenshot(page, "04_server_detail.png")
        else:
            server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
            page.goto(server_url, wait_until="domcontentloaded", timeout=90000)
            time.sleep(2)
            screenshot(page, "04_server_detail.png")

        # ── 5. Determine server status ─────────────────────────────────
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

        # ── 6. Start server if stopped ──────────────────────────────────
        if not is_running:
            log.info("Server is stopped — clicking Start")
            log.info("[DEBUG] Page visible text (first 500 chars):")
            page_text = page.inner_text("body")
            log.info(page_text[:500])

            start_selectors = [
                "button:has-text('Start')",
                "button:has-text('start')",
                "a:has-text('Start')",
                "div:has-text('Start')",
                ".btn-start",
                "button:has-text('启动')",
                "a:has-text('启动')",
                "button:has-text('重启')",
                "a:has-text('重启')",
                "button:has-text('开始')",
                "a:has-text('开始')",
                "text=Start",
                "text=启动",
            ]
            start_btn = None
            for sel in start_selectors:
                try:
                    start_btn = page.query_selector(sel)
                    if start_btn:
                        log.info("Found Start button with selector: %s", sel)
                        break
                except Exception:
                    continue

            if start_btn:
                start_btn.click()
                time.sleep(3)
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                screenshot(page, "05_server_started.png")
                report["action"] = "started"
                log.info("Server start clicked")
            else:
                log.warning("Start button not found with any known selector")
                report["action"] = "start-failed"
                report["error"] = "Start button not found — check screenshots for page structure"

        # ── 7. Check expiry & handle renewal ────────────────────────────
        expiry_selectors = [
            "text=/Expiry|Renew|到期|剩余/i",
            "text=/Expire|过期|有效期/i",
            "text=/Plan|套餐|版本/i",
            "text=/Expiring|即将到期/i",
            "text=/days|h/m/i",
            "text=/Remaining|余额/i",
            "div:has-text('到期')",
            "div:has-text('Expiry')",
            "span:has-text('到期')",
            "span:has-text('Expiry')",
        ]
        expiry_el = None
        for sel in expiry_selectors:
            try:
                expiry_el = page.query_selector(sel)
                if expiry_el:
                    log.info("Found expiry element with selector: %s", sel)
                    break
            except Exception:
                continue

        if expiry_el:
            expiry_text = expiry_el.inner_text()
            report["expiry"] = expiry_text
            log.info("Expiry info: %s", expiry_text)

            days, hours, mins, total_h = parse_expiry(expiry_text)
            should_renew = FORCE_RENEW or total_h < 48
            if should_renew:
                log.info("Initiating renewal (days=%d, hours=%d, force=%s)",
                         days, hours, FORCE_RENEW)
                report["action"] = "renewed"

                renew_selectors = [
                    "button:has-text('Renew')",
                    "button:has-text('Renewal')",
                    "button:has-text('续期')",
                    "button:has-text('续费')",
                    "a:has-text('Renew')",
                    "a:has-text('续期')",
                    "a:has-text('续费')",
                    ".renew-btn",
                    ".btn-renew",
                    "text=Renew",
                    "text=续期",
                    "text=续费",
                ]
                renew_btn = None
                for sel in renew_selectors:
                    try:
                        renew_btn = page.query_selector(sel)
                        if renew_btn:
                            log.info("Found renew button with selector: %s", sel)
                            break
                    except Exception:
                        continue

                if renew_btn:
                    renew_btn.click()
                    time.sleep(2)

                    log.info("Waiting for Cloudflare Turnstile...")
                    wait_for(page, "[data-sitekey], .cf-turnstile", 30, "turnstile")
                    time.sleep(8)

                    confirm = page.query_selector(
                        "button:has-text('Confirm'), button:has-text('OK'), button:has-text('确定')")
                    if confirm:
                        confirm.click()
                        time.sleep(3)

                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    screenshot(page, "06_after_renew.png")

                    expiry_el2 = page.query_selector("text=/Expiry|到期/i")
                    if expiry_el2:
                        new_expiry = expiry_el2.inner_text()
                        log.info("New expiry: %s", new_expiry)
                        report["expiry"] = new_expiry
                else:
                    log.warning("Renew button not found with any known selector")
                    report["action"] = "renew-failed"
                    report["error"] = "Renew button not found on page"
            else:
                log.info("No renewal needed (total_hours=%d)", total_h)
                report["action"] = "skipped"
        else:
            log.warning("Expiry element not found with any known selector")
            log.info("[DEBUG] Full page text (first 800 chars) for expiry search:")
            page_text = page.inner_text("body")
            log.info(page_text[:800])
            report["error"] = "Expiry element not found — check screenshots for page structure"

        screenshot(page, "07_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass  # Event loop may already be stopped

    # ── 8. Build & send notification ────────────────────────────────────
    status_icon = "🟢" if report["status"] == "running" else "🔴"
    action_icons = {
        "started": "▶️",
        "renewed": "🔄",
        "skipped": "⏭️",
        "renew-failed": "❌",
        "none": "⚪",
    }
    action_icon = action_icons.get(report["action"], "❓")

    body_lines = [
        f"🖥️ **Zampto Server Report**",
        "",
        f"**Server ID:** `{SERVER_ID}`",
        f"**Status:** {status_icon} {report['status'].title()}",
        f"**Action:** {action_icon} {report['action']}",
    ]
    if report.get("expiry"):
        body_lines.append(f"**Expiry:** {report['expiry']}")
    if report.get("error"):
        body_lines.append(f"**⚠️ Error:** {report['error']}")
    body_lines.append("")
    body_lines.append(f"_Generated: {report['timestamp']}_")

    body = "\n".join(body_lines)
    log.info("--- Report ---\n%s", body)
    push_tg("🖥️ Zampto Server Report", body)

    os.makedirs("./screenshots", exist_ok=True)
    with open("./screenshots/report.json", "w") as f:
        json.dump(report, f, indent=2)
    log.info("Report saved to ./screenshots/report.json")


if __name__ == "__main__":
    main()
