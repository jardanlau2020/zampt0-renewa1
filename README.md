# Zampto 自动续期 & 状态监控

基于 **CloakBrowser** 的 Zampto 免费 Minecraft 服务器自动化工具，每天北京时间 08:00 自动运行。

## 功能

- ✅ 自动登录 Zampto（Logto 两步登录）
- ✅ 检测服务器状态（Running / Stopped）
- ✅ 服务器离线自动点 Start 启动
- ✅ 智能续期判断：剩余不足 48 小时自动续期
- ✅ 支持 `FORCE_RENEW` 强制续期
- ✅ 自动绕过 Cloudflare Turnstile 验证
- ✅ Telegram Bot 推送服务器状态 + 到期时间

## 项目结构

```
zampto/
├── .github/
│   └── workflows/
│       └── zampto.yml      # GitHub Actions 工作流
├── zampto_auto.py          # CloakBrowser 自动化脚本
├── requirements.txt        # Python 依赖
├── screenshots/            # 截图输出目录（运行时创建）
└── README.md
```

## 部署到 GitHub Actions

### 1. Fork 或新建仓库，上传本项目文件

### 2. 配置 Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，逐一创建以下 Secret：

| Secret 名称 | 说明 |
|---|---|
| `ZAMPTO_USERNAME` | Zampto 用户名（登录 Dashboard 用的账号） |
| `ZAMPTO_PASSWORD` | Zampto 密码 |
| `ZAMPTO_SERVER_ID` | 服务器 ID（见下方第 3 步） |
| `TG_BOT_TOKEN` | Telegram Bot Token（见下方第 4 步） |
| `TG_CHAT_ID` | 接收消息的 Chat ID（见下方第 4 步） |
| `HY2_CONFIG` | Hysteria2 代理配置（可选，见下方第 6 步） |

### 3. 推送消息示例

```
🖥️ Zampto Server Report

**Server ID:** `***`
**Status:** 🟢 Running
**Action:** ✅ renewed
**Expiry:** 30 days 0h 0m

_Generated: 2026-07-30T00:00:00Z_
```

### 4. 配置 Telegram Bot 推送（可选，推荐）

1. 在 Telegram 中打开 **@BotFather**，发送 `/newbot`，按提示创建 Bot
2. 获得 **Bot Token**，填入 `TG_BOT_TOKEN`（格式如 `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`）
3. 向你的用户或群组发送一条消息给 Bot
4. 在浏览器访问 `https://api.telegram.org/bot你的TOKEN/getUpdates`
5. 在返回的 JSON 中找到 `"chat": {"id": 123456789}`，将 `123456789` 填入 `TG_CHAT_ID`

> 如果不需要推送通知，这两个 Secret 可以不填，脚本会跳过通知。

### 5. 获取 Server ID

登录 Zampto Dashboard → 点进你的服务器 → 看浏览器地址栏：
```
https://dash.zampto.net/server?id=123456
```
URL 末尾的数字（如 `123456`）就是 Server ID，填入 `ZAMPTO_SERVER_ID`。

### 6. 配置 HY2 代理（可选）

> 如果不需要代理访问 Zampto，**可跳过此步骤，不填 HY2_CONFIG**。

在 `HY2_CONFIG` 中填入 YAML 格式的 Hysteria2 客户端配置：

```yaml
listen: 0.0.0.0:1080
server: 你的HY2节点地址:端口
up_mbps: 100
down_mbps: 100
obfs: salamander
obfs-password: 你的混淆密码
auth:
  - 用户名:密码
tls:
  sni: 域名
  insecure: true
fastopen: true
```

## 注意事项

- Turnstile 由 CloakBrowser 自动处理，无需手动验证
- 每次运行保存截图到 `screenshots/`（GitHub Actions artifact 保留 3 天）
- 服务器在线时不会触发 Start，避免干扰正常运行
- 默认仅在剩余不足 48 小时时续期，可通过 `FORCE_RENEW=true` 强制续期
