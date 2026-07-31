// ==UserScript==
// @name         Zampto Dashboard Helper - GitHub Actions Edition
// @namespace    https://zampto.net/
// @version      2.0
// @description  Auto-renew via button click (adapted for headless browser)
// @match        https://dash.zampto.net/*
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // Polyfill GM_notification for non-Tampermonkey environments
    if (typeof GM_notification === 'undefined') {
        window.GM_notification = function (opts) {
            console.log('[GM_notification]', typeof opts === 'string' ? opts : opts.text || opts.title);
        };
    }

    const CONFIG = {
        retryIntervalMs: 2000,
        maxRetries: 30,                          // Try for ~60 seconds
        renewalCooldown: 5000,
    };

    console.log('[Zampto Helper] Script v2.0 loaded at', window.location.href);

    let attempts = 0;
    let renewalInProgress = false;
    let lastRenewalTime = 0;

    function log(text) {
        console.log(`[Zampto Helper] ${new Date().toISOString()} ${text}`);
    }

    function clickRenewServer() {
        const now = Date.now();
        if (now - lastRenewalTime < CONFIG.renewalCooldown) {
            log(`Cooldown active, skipping`);
            return false;
        }
        if (renewalInProgress) {
            log(`Renewal in progress, waiting`);
            return false;
        }

        // Find Renew Server button
        const allButtons = [...document.querySelectorAll("button")];
        log(`Found ${allButtons.length} buttons, scanning for Renew...`);

        let renewBtn = allButtons.find(el => el.innerText.trim() === "Renew Server");
        if (!renewBtn) {
            renewBtn = allButtons.find(el =>
                el.innerText.includes("Renew") && el.innerText.includes("Server")
            );
        }

        if (!renewBtn) {
            // Also try links (in case it's an <a>)
            const allLinks = [...document.querySelectorAll("a")];
            renewBtn = allLinks.find(el => el.innerText.includes("Renew"));
            if (renewBtn) log(`Found Renew link (not button)`);
        }

        if (!renewBtn) {
            log(`No Renew button found yet (attempt ${attempts + 1}/${CONFIG.maxRetries})`);
            return false;
        }

        log(`Found Renew button: "${renewBtn.innerText.trim()}" - clicking...`);
        renewalInProgress = true;
        lastRenewalTime = Date.now();

        renewBtn.click();
        log(`Clicked Renew button`);

        // Set a flag on window so the Python script can detect success
        window.__zamptoRenewalClicked = true;
        window.__zamptoRenewalTime = new Date().toISOString();

        setTimeout(() => {
            renewalInProgress = false;
        }, CONFIG.renewalCooldown);

        return true;
    }

    function tryRenew() {
        attempts++;
        if (attempts > CONFIG.maxRetries) {
            log(`Max retries reached, giving up`);
            window.__zamptoRenewalFailed = true;
            return;
        }

        const success = clickRenewServer();
        if (success) {
            log(`Renewal triggered successfully after ${attempts} attempts`);
            return;
        }

        setTimeout(tryRenew, CONFIG.retryIntervalMs);
    }

    // Start trying after a short delay (let page render)
    setTimeout(() => {
        log(`Starting renewal attempts (max ${CONFIG.maxRetries})`);
        tryRenew();
    }, 3000);

})();
