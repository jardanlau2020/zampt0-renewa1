#!/usr/bin/env python3
"""Zampto Browser-based Renewal - uses cloakbrowser + userscript to bypass Turnstile.

This module is invoked when the pure-API approach fails due to captcha requirement.
It launches a real browser via cloakbrowser, injects the userscript (which clicks
the Renew Server button), and waits for the renewal to complete.

The browser running on a residential-IP-like environment (via TUIC proxy) should
pass Cloudflare Turnstile's automated checks.
"""

import os
import sys
import json
import time
import base64
import logging
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("requests not installed")
    raise

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto-browser")

USERNAME = os.getenv("ZAMPTO_USERNAME", "")
PASSWORD = os.getenv("ZAMPTO_PASSWORD", "")
SERVER_ID = os.getenv("ZAMPTO_SERVER_ID", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
DASHBOARD_URL = "https://dash.zampto.net"
USERSCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zampto_userscript.js")


def push_tg(title, body):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        proxy_url = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": f"{title}\n\n{body}", "parse_mode": "Markdown"},
            timeout=15,
            proxies=proxies,
        )
        r.raise_for_status()
        log.info("Telegram sent OK")
    except Exception as e:
        log.error("Telegram failed: %s", e)


def load_cookies_from_secret():
    """Decode ZAMPTO_SESSION_SECRET and return cookies list."""
    secret = os.getenv("ZAMPTO_SESSION_SECRET")
    if not secret:
        log.error("ZAMPTO_SESSION_SECRET not set")
        return None
    try:
        decoded = base64.b64decode(secret).decode("utf-8")
        session_data = json.loads(decoded)
        cookies = session_data.get("cookies", [])
        log.info("Loaded %d cookies from secret", len(cookies))
        return cookies
    except Exception as e:
        log.error("Failed to decode secret: %s", e)
        return None


def browser_renew():
    """Launch cloakbrowser, inject userscript, wait for renewal."""
    log.info("=== BROWSER-BASED RENEWAL MODE ===")

    try:
        from cloakbrowser import launch
    except ImportError:
        log.error("cloakbrowser not available")
        return False

    cookies = load_cookies_from_secret()
    if not cookies:
        return False

    # Read userscript content
    if not os.path.exists(USERSCRIPT_PATH):
        log.error("Userscript not found at %s", USERSCRIPT_PATH)
        return False
    with open(USERSCRIPT_PATH, "r", encoding="utf-8") as f:
        userscript = f.read()
    log.info("Userscript loaded (%d bytes)", len(userscript))

    # Configure proxy if set
    proxy = None
    proxy_url = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy_url:
        # Convert socks5h:// to socks5:// for playwright compatibility
        pw_proxy_url = proxy_url.replace("socks5h://", "socks5://")
        proxy = {"server": pw_proxy_url}
        log.info("Using proxy: %s", pw_proxy_url)

    # Launch browser - headless=False on GitHub Actions (Xvfb provides display)
    # headless=True might trigger Cloudflare bot detection
    is_github = bool(os.getenv("GITHUB_ACTION") or os.getenv("CI"))
    headless = False if is_github else False  # Always non-headless for better stealth
    log.info("Launching cloakbrowser (headless=%s, github=%s)", headless, is_github)

    try:
        browser = launch(headless=headless, proxy=proxy)
    except Exception as e:
        log.error("Failed to launch browser: %s", e)
        return False

    try:
        # Create context with cookies pre-set
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )

        # Add cookies to context
        # Convert cookie format from session.json to playwright format
        pw_cookies = []
        for c in cookies:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".zampto.net"),
                "path": c.get("path", "/"),
            }
            if c.get("expires"):
                cookie["expires"] = c["expires"]
            pw_cookies.append(cookie)

        context.add_cookies(pw_cookies)
        log.info("Added %d cookies to browser context", len(pw_cookies))

        # Inject userscript BEFORE any page navigation
        # add_init_script runs on every page load, before page scripts
        context.add_init_script(userscript)
        log.info("Injected userscript via add_init_script")

        page = context.new_page()

        # Collect console logs
        page.on("console", lambda msg: log.info(f"[browser console] {msg.type} {msg.text}"))

        # Navigate to the server dashboard page
        # The URL pattern in the userscript uses ?id= query param
        # Try multiple URL patterns
        target_urls = [
            f"{DASHBOARD_URL}/server/{SERVER_ID}",
            f"{DASHBOARD_URL}/server?id={SERVER_ID}",
            f"{DASHBOARD_URL}/servers?id={SERVER_ID}",
            f"{DASHBOARD_URL}/dashboard",
            f"{DASHBOARD_URL}/",
        ]

        page_loaded = False
        for url in target_urls:
            log.info("Trying URL: %s", url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Wait a bit for page to render
                time.sleep(3)
                final_url = page.url
                log.info("Landed on: %s (title: %s)", final_url, page.title())

                # Check if we got redirected to /blocked or /auth/login
                if "/blocked" in final_url:
                    log.warning("Redirected to /blocked - VPN/IP still detected")
                    continue
                if "/auth/login" in final_url:
                    log.warning("Redirected to login - cookies may be expired")
                    continue

                page_loaded = True
                break
            except Exception as e:
                log.warning("Failed to load %s: %s", url, e)
                continue

        if not page_loaded:
            log.error("Could not load any dashboard page")
            return False

        # Wait for userscript to do its job
        # The script tries every 2 seconds for up to 60 seconds
        log.info("Waiting for userscript to trigger renewal (max 90 seconds)...")
        start_time = time.time()
        max_wait = 90

        while time.time() - start_time < max_wait:
            time.sleep(2)
            try:
                clicked = page.evaluate("() => window.__zamptoRenewalClicked || false")
                failed = page.evaluate("() => window.__zamptoRenewalFailed || false")
                if clicked:
                    clicked_time = page.evaluate("() => window.__zamptoRenewalTime || ''")
                    log.info("✓ Renewal button clicked at: %s", clicked_time)
                    # Wait additional time for renewal to complete server-side
                    time.sleep(10)
                    return True
                if failed:
                    log.error("Userscript reported failure (max retries reached)")
                    return False
            except Exception as e:
                log.warning("Error checking renewal status: %s", e)

        log.error("Timeout waiting for userscript to click renew button")
        return False

    except Exception as e:
        log.error("Browser operation failed: %s", e)
        return False
    finally:
        try:
            browser.close()
            log.info("Browser closed")
        except Exception:
            pass


def main():
    log.info("=== Zampto Browser Renewal v1.0 ===")
    log.info("Server ID: %s", SERVER_ID)

    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing required env vars")
        return False

    success = browser_renew()

    if success:
        log.info("✓ Browser-based renewal completed")
        push_tg("✅ Zampto Renewal Success",
                f"Browser-based renewal completed for server `{SERVER_ID}`\n\n"
                f"_Time: {datetime.now(timezone.utc).isoformat()}_")
    else:
        log.error("✗ Browser-based renewal failed")
        push_tg("❌ Zampto Renewal Failed",
                f"Browser-based renewal failed for server `{SERVER_ID}`\n"
                f"Check workflow logs for details.\n\n"
                f"_Time: {datetime.now(timezone.utc).isoformat()}_")

    return success


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
