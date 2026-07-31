# Zampto 自动续期 ⚡

通过 GitHub Actions 每日自动检查并续期你的 Zampto Minecraft 服务器。

**功能特性：**
- 每日 UTC 00:00（北京时间 08:00）自动检查
- 服务器停止时自动启动
- 到期时间不足 48 小时自动续期（可配置）
- 完成或失败时通过 Telegram Bot 推送通知
- 两阶段认证机制，绕过 Cloudflare Turnstile 验证

---

## 🚀 配置指南

### 阶段一：首次登录（本地，一次性操作）

在你的**本地机器**上运行一次，完成 Zampto 身份验证：

```bash
python zampto_auto.py
```

会自动打开一个浏览器窗口，请按正常流程完成登录（包括可能出现的 Turnstile 验证码）。登录成功后，脚本会将认证信息保存到 `./screenshots/session.json`。

> 💡 该 session 文件包含你的登录 Cookie，切勿公开分享。

---

### 阶段二：配置 GitHub Secrets

在你的 GitHub 仓库（**weikkadd/zampto**）中：

1. 进入 **Settings → Secrets and variables → Actions**
2. 点击 **New secret**，依次添加以下变量：

| Secret 名称 | 说明 | 示例值 |
|-------------|------|--------|
| `ZAMPTO_USERNAME` | Zampto 账户邮箱 | `user@example.com` |
| `ZAMPTO_PASSWORD` | Zampto 账户密码 | `********` |
| `ZAMPTO_SERVER_ID` | 服务器 ID（例如 6578） | `6578` |
| `TG_BOT_TOKEN` | 来自 @BotFather 的 Bot Token | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `TG_CHAT_ID` | 来自 @userbotbot 的 Chat ID | `123456789` |
| `ZAMPTO_SESSION_SECRET`（新增） | session.json 的 base64 编码字符串 | `{base64 编码后的 session.json 内容}` |
| `HY2_CONFIG`（可选） | Hysteria2 配置 YAML | *（可选）* |

生成 `ZAMPTO_SESSION_SECRET` 的方法：
```python
import json, base64
with open("./screenshots/session.json") as f:
    session = json.load(f)
encoded = base64.b64encode(json.dumps(session).encode()).decode()
print(encoded)  # 复制这段内容到 Secret 中
```

---

### 阶段三：验证工作流

工作流会在每天 UTC 00:00 自动触发。你也可以通过 **Actions → Run workflow** 手动触发一次进行验证。

---

## 🔒 安全提示

- 切勿将 `session.json` 提交到 Git 仓库（已在 `.gitignore` 中排除）
- 推送代码时建议使用专门的 GitHub Personal Access Token（Classic 类型，仅需 repo 权限）
- 妥善保管 `ZAMPTO_SESSION_SECRET`——它等同于你服务器的登录凭证

---

## 🐍 依赖说明

```
cloakbrowser[geoip]   # 仅阶段一（本地浏览器登录）需要
requests               # 用于纯 API 续期
```

GitHub Actions 会通过 `requirements.txt` 自动安装以上依赖。

---

## 🛠 常见问题排查

- **完成阶段一后仍然登录失败？** 删除旧的 session 文件，重新执行阶段一。
- **API 返回 403 / 401？** Session 可能已过期，重新执行阶段一获取新的 session，并更新 `ZAMPTO_SESSION_SECRET`。
- **出现 "Server not found" 错误？** 请检查 `ZAMPTO_SERVER_ID` 是否正确。

---

## 📖 架构说明

```
阶段一（本地，一次性）：
  浏览器 → 登录页面 → 手动通过 Turnstile → 保存 session.json

阶段二（GitHub Actions，每日执行）：
  ZAMPTO_SESSION_SECRET (base64) → 解码 → requests.Session → 直接调用 API
  ↓
  /api/server/{id}      → 检查服务器状态
  POST /api/server/{id}/start   → 若服务器已停止
  POST /api/server/{id}/renew   → 若到期时间 < 48 小时
  Telegram Bot → 推送执行报告
```

阶段二完全跳过登录页面，直接复用 Cookie 调用 API，从而彻底规避 Cloudflare Turnstile 问题。
