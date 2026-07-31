# Zampto Auto Renewal ⚡

Automatically check and renew your Zampto Minecraft server daily via GitHub Actions.

**Features:**
- Daily auto-check at UTC 00:00 (Beijing 08:00)
- Auto-start if stopped
- Auto-renew when expiry < 48 hours (configurable)
- Telegram Bot notifications on completion or failure
- Two-phase auth to bypass Cloudflare Turnstile

---

## 🚀 Setup Guide

### Phase 1: Initial Login (Local, One-Time)

Run this **once on your local machine** to authenticate with Zampto:

```bash
python zampto_auto.py
```

A browser window will open. Complete the login normally (including any Turnstile CAPTCHA). After successful login, the script saves `./screenshots/session.json`.

> 💡 The session file contains your authenticated cookies. Do NOT share it publicly.

---

### Phase 2: Configure GitHub Secrets

On your GitHub repository (**weikkadd/zampto**):

1. Go to **Settings → Secrets and variables → Actions**
2. Click **New secret** for each of the following:

| Secret Name | Description | Example Value |
|-------------|-------------|---------------|
| `ZAMPTO_USERNAME` | Your Zampto account email | `user@example.com` |
| `ZAMPTO_PASSWORD` | Your Zampto password | `********` |
| `ZAMPTO_SERVER_ID` | Server ID (e.g., 6578) | `6578` |
| `TG_BOT_TOKEN` | Bot token from @BotFather | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `TG_CHAT_ID` | Your chat ID from @userbotbot | `123456789` |
| `ZAMPTO_SESSION_SECRET` (NEW) | Session JSON encoded as base64 | `{base64-encoded session.json content}` |
| `HY2_CONFIG` (Optional) | Hysteria2 config YAML | *(optional)* |

To generate `ZAMPTO_SESSION_SECRET`:
```python
import json, base64
with open("./screenshots/session.json") as f:
    session = json.load(f)
encoded = base64.b64encode(json.dumps(session).encode()).decode()
print(encoded)  # Copy this into the secret
```

---

### Phase 3: Verify Workflow

The workflow is triggered automatically every day at UTC 00:00. You can also manually trigger it via **Actions → Run workflow**.

---

## 🔒 Security Note

- Never commit `session.json` to Git (it's already in `.gitignore`)
- Use a dedicated GitHub Personal Access Token (classic, repo scope) for git push operations
- Keep `ZAMPTO_SESSION_SECRET` confidential — it provides authenticated access to your server

---

## 🐍 Requirements

```
cloakbrowser[geoip]   # Only needed for Phase 1 (browser login)
requests               # For pure API renewal
```

GitHub Actions installs both automatically via `requirements.txt`.

---

## 🛠 Troubleshooting

- **Login still fails after manual phase?** Clear the old session file and re-run Phase 1.
- **API returns 403/401?** Your session may have expired. Re-run Phase 1 to get a new one and update the `ZAMPTO_SESSION_SECRET`.
- **"Server not found" error?** Verify `ZAMPTO_SERVER_ID` is correct.

---

## 📖 Architecture Overview

```
Phase 1 (local, once):
  Browser → Login page → Solve Turnstile manually → Save session.json

Phase 2 (GitHub Actions, daily):
  ZAMPTO_SESSION_SECRET (base64) → Decode → requests.Session → Direct API calls
  ↓
  /api/server/{id} → Check status
  POST /api/server/{id}/start   → If stopped
  POST /api/server/{id}/renew   → If expiry < 48h
  Telegram Bot → Send report
```

By bypassing the login page entirely in Phase 2, we avoid the Cloudflare Turnstile problem completely.