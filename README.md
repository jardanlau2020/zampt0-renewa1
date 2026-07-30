# Zampto 自动续期 & 状态监控

基于 **CloakBrowser** 的 Zampto 免费 Minecraft 服务器自动化工具，每天北京时间 08:00 自动运行。

## 功能

- ✅ 自动登录 Zampto（Logto 两步登录）
- ✅ 检测服务器状态（Running / Stopped）
- ✅ 服务器离线自动点 Start 启动
- ✅ 智能续期判断：剩余不足 48 小时自动续期
- ✅ 支持 `FORCE_RENEW` 强制续期
- ✅ 自动绕过 Cloudflare Turnstile 验证
- ✅ WxPusher 推送服务器状态 + 到期时间

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

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 说明 |
|---|---|
| `ZAMPTO_USERNAME` | Zampto 用户名 |
| `ZAMPTO_PASSWORD` | Zampto 密码 |
| `ZAMPTO_SERVER_ID` | 服务器 ID（URL 中的数字） |
| `WXPUSHER_TOKEN` | WxPusher App Token |
| `WXPUSHER_UID` | WxPusher 用户 UID |
| `HY2_CONFIG` | Hysteria2 代理配置（可选） |

### 3. 获取 Server ID

登录 Zampto Dashboard，点进服务器详情，URL 末尾的数字即为 Server ID：
```
https://dash.zampto.net/server?id=XXXX
```

## 推送消息示例

```
🖥️ Zampto Server Report

**Server ID:** `***`
**Status:** 🟢 Running
**Action:** ✅ renewed
**Expiry:** 30 days 0h 0m

_Generated: 2026-07-30T00:00:00Z_
```

## 注意事项

- Turnstile 由 CloakBrowser 自动处理，无需手动验证
- 每次运行保存截图到 `screenshots/`（GitHub Actions artifact 保留 3 天）
- 服务器在线时不会触发 Start，避免干扰正常运行
- 默认仅在剩余不足 48 小时时续期，可通过 `FORCE_RENEW=true` 强制续期
