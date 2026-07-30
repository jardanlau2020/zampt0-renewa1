# Zampto 自动续期 & 状态监控

基于 CloakBrowser 的 Zampto 免费 Minecraft 服务器自动化工具，每天北京时间 08:00 运行。

## 功能

- ✅ 自动登录 Zampto（Logto 两步登录）
- ✅ 检测服务器状态（Running / Stopped）
- ✅ 服务器离线自动点 Start 启动
- ✅ 自动点击续期，等待 Cloudflare Turnstile 验证自动通过
- ✅ WxPusher 推送服务器状态 + 到期时间

## 部署到 GitHub Actions

### 1. Fork 或新建仓库，上传本项目文件

### 2. 配置 Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 说明 |
|---|---|
| `ZAMPTO_USERNAME` | Zampto 用户名（如 `ssdXXXXXXXXXX`）|
| `ZAMPTO_PASSWORD` | Zampto 密码 |
| `ZAMPTO_SERVER_ID` | 服务器 ID（URL 中的数字，如 `XXXX`）|
| `WXPUSHER_TOKEN` | WxPusher App Token |
| `WXPUSHER_UID` | WxPusher 用户 UID |

### 3. 获取 Server ID

登录 Zampto Dashboard，点进服务器详情，URL 末尾的数字即为 Server ID：
```
https://dash.zampto.net/server?id=XXXX
```

## 推送消息示例

```
🖥️ Zampto 服务器日报
服务器 ID: ***
地址: ***

状态: 🟢 Running

Expiry (Next Renewal): 1 day 23h 53m
Last Renewed: 已自动续期
  → 已自动续期 ✅
```

## 注意事项

- 续期的 CF Turnstile 由 CloakBrowser 自动处理
- 每次运行保存截图+录像（3 天）供调试
- 服务器离线才触发 Start，不影响正在运行的服务器
