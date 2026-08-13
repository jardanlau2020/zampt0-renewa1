# Zampto 自动续期 ⚡

通过 GitHub Actions 每日自动检查并续期你的 Zampto Minecraft 服务器。https://zampto.net/

**功能特性：**
- 每日 UTC 00:00（北京时间 08:00）自动检查
- 服务器停止时自动启动
- 到期时间不足 48 小时自动续期（可配置）
- 完成或失败时通过 Telegram Bot 推送通知
- 两阶段认证机制，绕过 Cloudflare Turnstile 验证
- 支持 TUIC 代理（自动解析 v2rayN 节点链接），解决 GitHub Actions IP 被封问题

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
| `TUIC_URI`（可选，推荐） | TUIC 节点链接，绕过 IP 封锁 | `v2rayntuic://UUID:password@host:port?sni=xxx&insecure=0&alpn=h3` |
| `HY2_CONFIG`（可选） | Hysteria2 配置 YAML（旧版兼容） | *（可选）* |

生成 `ZAMPTO_SESSION_SECRET` 的方法：
```python
import json, base64
with open("./screenshots/session.json") as f:
    session = json.load(f)
encoded = base64.b64encode(json.dumps(session).encode()).decode()
print(encoded)  # 复制这段内容到 Secret 中
```

---

### 阶段二（扩展）：配置 TUIC 代理节点

> ⚠️ **如果你在 GitHub Actions 运行时遇到 `403 Access blocked, VPN or proxy detected` 错误**，说明 GitHub 的 IP 被 Zampto 拉黑了，必须配置代理绕过。支持 TUIC v5 协议。

#### 1. 获取 TUIC 节点链接

在 v2rayN / NekoBox / Clash Verge 等客户端中，右键节点 → **分享 / 复制链接**，你会得到类似这样的字符串：

```
v2rayntuic://87c48d07-49f3-483a-8186-64d322f70751:eishare2025@46.59.10.123:443?sni=example.com&insecure=1&alpn=h3
tuic://87c48d07-49f3-483a-8186-64d322f70751:eishare2025@46.59.10.123:443?sni=example.com&insecure=0&alpn=h3
```

两种前缀都支持（`v2rayntuic://` 和 `tuic://`）。

#### 2. 添加到 GitHub Secret

1. 打开 **Settings → Secrets and variables → Actions**
2. 点击 **New secret**
3. **Name:** `TUIC_URI`
4. **Value:** 粘贴完整的 TUIC 链接（包含 `?` 后所有参数）
5. 点击 **Add secret**

#### 3. URI 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `UUID:password@host:port` | ✅ | 节点信息（UUID、密码、服务器、端口） |
| `sni` | ❌ | TLS SNI，缺省时使用 host |
| `insecure` | ❌ | 是否禁用证书校验，`1`=禁用（自签证书用），`0`=校验，缺省 `0` |
| `alpn` | ❌ | ALPN 协议，缺省 `h3` |

#### 4. 工作机制

Workflow 会自动：
1. 下载 `EAimTY/tuic` 客户端
2. 解析 URI 生成 JSON 配置
3. 启动 tuic-client 监听 `127.0.0.1:1080` SOCKS5
4. 设置 `ALL_PROXY=socks5h://127.0.0.1:1080` 给后续步骤
5. Python 脚本自动读取 `ALL_PROXY`，所有 requests 请求（API 调用 + Telegram 推送）走代理

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
- **API 返回 `403 Access blocked, VPN or proxy detected`？** GitHub IP 被 Zampto 拉黑，请配置 `TUIC_URI` Secret（参考阶段二扩展章节）。
- **TUIC 代理启动失败？** 检查 URI 是否完整（特别是 `?` 后的 sni/insecure/alpn 参数）；如果用的是自签证书，确保 `insecure=1`。
- **Telegram 推送失败但 API 成功？** 同样是 IP 问题，配置 `TUIC_URI` 后 Telegram 推送也会自动走代理。

---

## 📖 架构说明

```
阶段一（本地，一次性）：
  浏览器 → 登录页面 → 手动通过 Turnstile → 保存 session.json

阶段二（GitHub Actions，每日执行）：
  +-----------+    +----------------+    +----------------------+
  | TUIC_URI  | -> | tuic-client    | -> | SOCKS5 127.0.0.1:1080|
  +-----------+    +----------------+    +----------------------+
                                                          |
  ZAMPTO_SESSION_SECRET (base64) -> requests.Session <----+ (proxy)
  ↓
  /api/server/{id}      → 检查服务器状态
  POST /api/server/{id}/start   → 若服务器已停止
  POST /api/server/{id}/renew   → 若到期时间 < 48 小时
  Telegram Bot → 推送执行报告（也走代理）
```

阶段二完全跳过登录页面，直接复用 Cookie 调用 API，从而彻底规避 Cloudflare Turnstile 问题。
配置 `TUIC_URI` 后，所有请求走代理，绕过 GitHub IP 封锁。
