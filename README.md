# Zampto 自动续期 ⚡

Zampto Minecraft 面板的自动续期：每 8 小时检查一次，剩余时间不足阈值就续，
结果推 Telegram。https://zampto.net/

> **2026-10-02 已迁移到 [renew-kit](../../renew-kit)**（composite action `@v0.5.3`）。
> 通知排版、结果分类、退出码、演练开关全部归 kit，本仓库只留业务逻辑。

---

## 每轮做什么

```
1. 准备出口      scripts/setup_proxy.sh：起 sing-box，探针验出口是否被 Zampto 标记
                 被标记就换下一个候选，最后才考虑直连；都不行则中止
2. 注入会话      ZAMPTO_SESSION_SECRET（session.json 的 base64）解出 cookies
3. 浏览器续期    Playwright 打开面板 → 必要时过 Turnstile → 点 Renew
                 由页面 JS 发请求，自动携带正确的 CSRF
4. 页内读到期    在页面上下文里 fetch /api/servers 拿 renewal（页面已鉴权，
                 必然读得到）；顺带判「要不要续」和「续上没续上」
5. 兜底 API 路径 浏览器没成时，用 requests 直接打 /api/server/renew
                 （CSRF 三种编码形态 × 两种 header 逐一试）
6. 出报告        classify() -> Outcome -> RenewReport.finish()（打印 + 发 TG + 退出码）
```

### `renewal` 字段：语义与两个坑

**`renewal` 是「本次 48h 窗口的起点」，不是到期时间** —— 到期 = renewal + 48h。
直接拿它当到期时间会渲染出一个已经过去的日期，这是这个面板最容易踩的坑，
现在集中收在 `renewal_to_expiry()` 里算一次。

实测确认的两件事（run #88 / #89）：

- **面板的 `renewal` 比真实 UTC 慢约 2 小时。** 点击发生在 07:52:5x，
  写回的却是 `2026-10-02T05:52:51.000Z`，差 2:00:08。所以
  `到期 = renewal + 48h` 比真实到期早约 2h —— **保守方向**，无害，
  但报告里的「到期」会比面板上显示的早两小时，别以为是 bug。
- **`renewal` 每次续期都会推进**（05:47:09 → 05:52:51），
  所以它可以当「这次到底续上没有」的判据。

### 到期时间从哪来：页内读数，不是 requests

`GET /api/servers` 在**页面上下文里**回 200 且带着完整服务器对象；同一个
URL 用我们手写的 `requests` 打，**一律 401 `{"error":"Unauthorized"}`**。
差别在 cookie 集合：`ZAMPTO_SESSION_SECRET` 里只有 `zampto_session`，
`XSRF-TOKEN` 是页面加载后由服务端下发的，secret 里没有它，而 API 守卫要。

所以（run #87 页内探测定论之后）：

- **主路径**：`read_renewal_in_page()` —— 在页面里 `fetch`，用浏览器
  已经建立好的鉴权。`_PAGE_STATE` 保存快照 cookie 与读到的时间。
- **兜底**：`_query_expiry()` —— requests 查询，只在页面读数失败时用，
  并把浏览器快照的 cookie 传过去（不再是 secret 里那份）。

### 怎么判「续期成功」：看 renewal 变没变，不看 HTTP 状态

面板**有可能**对一次没有实际延长窗口的请求回 HTTP 200，所以只看状态码
是不可靠的。判定统一走 `_verify_renewed()`：

| 情况 | 结论 |
|---|---|
| renewal 变了 | `renewed` ✅ 真续上 |
| 读到了但没变 | `skipped` 🟢 面板认为还没到窗口，按「状态良好」报 |
| 读不到 + 有 2xx | `renewed` + ⚠️ 日志说明「只能按 HTTP 状态判（旧行为）」 |
| 读不到 + 无 2xx | `failed` 🚨 |

### `RENEW_THRESHOLD_HOURS` 默认 24（不是 48）

窗口固定 48h，刚续完剩约 45h（还叠了上面那个 2h 偏移）。阈值取 48 时
`45 > 48` 为假 → 闸门永远不跳，每 8 小时都白点一次 Renew + 过一次
Turnstile。取 24 表示「过半再续」，即使某次失败也还有 3 个 cron 周期的
缓冲。想恢复旧行为设 `RENEW_THRESHOLD_HOURS=48`。

注意：闸门需要页面才能读数，所以**每轮仍会起浏览器**；阈值省掉的是
点击和 Turnstile，不是运行时长。

---

## 结果分类

`classify()` 是唯一裁决口，优先级：**显式 transient 标记 > 出口被风控 > 业务动作 > 错误文本特征**。

| Outcome | 触发条件 | 渲染 | 退出码 |
|---|---|---|---|
| `renewed` | renewal 确实推进了 | ✅ 成功续期至 … | 0 |
| `skipped` | 未到窗口且无错误 | 🟢 状态良好（剩 N 天） | 0 |
| `transient` | 上游 5xx / 超时 / 连不上 | 🌐 上游暂不可用 | **0（不标红）** |
| `failed` | 验证码挡路 / CSRF 打不通 / 出口被标记 | 🚨 续期未完成 | 1 |


三条容易搞错的地方：

- **上游故障不标红。** 502/503/504/520-524、timeout、connection reset → `transient`，
  退出码 0，等下次排程。旧版一律算失败，于是面板抖一下 job 就红一次。
- **出口被风控要标红。** `403 {"error":"Access blocked","reason":"VPN or proxy detected"}`
  不是上游故障，是**要换节点**，属于人工处理 → `failed`。
- **验证码挡路要标红。** 续期没发生就该让 workflow 报 failure —— 静默 green 会让人
  以为还在自动续期，实际服务器已经到期了。

---

## 配置

### Secrets（Settings → Secrets and variables → Actions）

| 名称 | 必填 | 说明 |
|---|---|---|
| `ZAMPTO_SESSION_SECRET` | ✅ | `screenshots/session.json` 的 **base64 全文**，见下方生成方法 |
| `ZAMPTO_USERNAME` | ✅ | 账户邮箱（仅本地交互登录用，CI 路径不读） |
| `ZAMPTO_PASSWORD` | ✅ | 账户密码（同上） |
| `ZAMPTO_SERVER_ID` | ✅ | 服务器 ID，如 `15629` |
| `PROXY_URI` | ✅ | 代理节点链接，多格式自动解析，见下 |
| `TUIC_URI` | ➖ | `PROXY_URI` 的兜底候选；两个都配时先试 `PROXY_URI` |
| `TG_BOT_TOKEN` | ➖ | 不配则只打日志，不推送 |
| `TG_CHAT_ID` | ➖ | 同上 |

`.github/secrets_chunks.json` 已删除 —— git 里不再有任何凭据。

> 旧注释说「REST API PUT /actions/secrets 有 7 字符硬上限」已被证伪：
> 用 PyNaCl **SealedBox**（libsodium sealed box）加密后 PUT 完全正常，182 字的
> `PROXY_URI` 就是那么写进去的。之前 422 的原因是用错 `Box` 而非 `SealedBox`。

### 变量（workflow_dispatch 输入）

| 输入 | 默认 | 说明 |
|---|---|---|
| `force_renew` | `false` | 无视剩余时间强制续期 |
| `enable_recording` | `false` | 录屏排障，产物只留 3 天 |
| `dry_run` | `false` | **演练**：照跑一遍，跳过 Telegram 通知，消息原文打进日志 |

`dry_run` 的闸门在 `renewkit.notify.send()` 里（v0.5.2 起）—— 所以它对
`RenewReport.finish()` 也生效，且一个字节都不会发出去。workflow 把它映射成
环境变量 `DRY_RUN=1`（不勾选时是空串，空串 = 关）。

### 生成 `ZAMPTO_SESSION_SECRET`

```python
import json, base64
with open("./screenshots/session.json") as f:
    print(base64.b64encode(json.dumps(json.load(f)).encode()).decode())
```

`scripts/phase1_setup.bat`（Windows）把这一整套本地登录流程包成了一次双击：
建 venv → 装依赖 → 打开浏览器人工登录 → 打印 base64。

### 代理节点

`PROXY_URI` 支持五种前缀，workflow 自动识别：

| 协议 | 示例 |
|---|---|
| hysteria2（推荐） | `hysteria2://密码@host:port?sni=example.com&insecure=1` |
| hy2 | `hy2://密码@host:port?sni=example.com&insecure=1` |
| tuic | `tuic://UUID:密码@host:port?sni=example.com&insecure=0&alpn=h3` |
| vless | `vless://UUID@host:port?security=tls&sni=example.com` |
| vmess | `vmess://base64的JSON…` |

**为什么要代理**：Zampto 于 2026-09-26 上线反 VPN/代理侦测，被标记的出口 IP 对所有
`/api/*` 回 403 `Access blocked`。GitHub runner 的裸出口就在名单里。

**探针怎么判断**（未登录即可区分两种 403）：

```
干净出口 → {"success":false,"message":"Unauthorized"}  + 页面 307 → /auth/login
被标记   → "...Access blocked..."                       + 页面 307 → /blocked
```

`scripts/setup_proxy.sh` 按 `PROXY_URI` → `TUIC_URI` 顺序逐个试，探针不过就换下一个，
全都不行才考虑直连；直连也不行就 `exit 1`（续期必失败，早死早超生，别白跑 20 分钟）。

> 节点 URI 本身（含凭据）**任何情况下都不打印**，只打印出口 IP。

---

## 怎么跑

workflow 每 8 小时跑一次（UTC 00/08/16），也可以在 Actions → Run workflow 手动触发。
本地跑：

```bash
pip install -r requirements.txt
python -m playwright install chromium

# 阶段一：本地交互登录，产出 ./screenshots/session.json
python zampto_auto.py
```

本地跑不设 `CI`，脚本会走 `load_session()` 读 `./screenshots/session.json`。

---

## 离线验证

```bash
python .verify/verify_zampto.py
```

不需要浏览器、不需要网络、不需要任何 secret。四组：

| 组 | 覆盖 |
|---|---|
| `[A]` | 纯函数：`renewal_to_expiry` / `_fmt_remaining` / `_new_report` / `find_csrf_cookie` / `mask_headers` / `_human_summary` |
| `[B]` | 场景矩阵：`classify()` 18 种组合 + `main()` 各 outcome 下的退出码与**通知条数** |
| `[C]` | 静态接线：迁移不变量、workflow 钉的版本、scripts 的不变量、README/`.gitignore` |
| `[D]` | 真子进程：`py_compile` / `import` / `main()` 退出码 / `bash -n` / YAML 解析 |

`[B]` 里有一条专门钉「**失败只发一条通知**」——旧版 `phase_api_renewal()` 失败时自己
推一条 🚨，`main()` 拿到 False 之后又推一条，一次失败收两条告警。

---

## 迁移到 renew-kit 时改了什么

1. **配置读取收口到 `renewkit.env`**；`TG_BOT_TOKEN`/`TG_CHAT_ID` 的模块级全局、
   `push_tg()`、`now_local()` 全部删除。
   顺带修掉一个真 bug：`push_tg()` 里手搓的 `requests.post` 会跟随 `ALL_PROXY`，
   而 `renewkit.notify` 用 urllib 直连 —— **TG 走代理本来就不必要**（代理是给面板用的），
   而且代理一挂连告警都发不出去。
2. **结果分类收口到 `Outcome`。** 旧版有三套并存的判定：`_report()` 里按 action 字符串
   分支、`phase_api_renewal()` 末尾再判一次 `_action in ("renewed","skipped")`、
   `main()` 里又按 status 字符串走一遍 —— 同一件事三个口径。
3. **修掉「失败会收到两条告警」**（见上）。
4. **上游 5xx / 连不上 / 超时 → `transient`（不标红）**，旧版一律算失败。
5. **`os._exit()` 保留。** `sys.exit` 抛 `SystemExit`，在 Playwright 的事件循环里
   可能被改写成非 0 退出码（续期明明成功、job 却标红），所以退出码现在由 `Outcome`
   算出来、仍然用 `os._exit` 落地。

同时删掉了 `.github/workflows/api-key-test.yml` —— 它读的是早已删除的
`.github/secrets_chunks.json`，跑起来只会报文件不存在。

---

## 排障

- **红灯先看是哪一类**：日志里 `🔑 使用 Cookie 注入认证` 那行下面如果是
  `Access blocked` → 换节点；如果是 `307 /auth/login` → session 失效，重贴 cookie。
- **`[ERROR] 代理候选与直连都被 Zampto 标记/不可用`** → 换一条非 WARP/VPN 段的节点
  写进 `PROXY_URI`，或人手去面板续期。
- **`续期未完成（剩 N 天）` + `Captcha required`** → Turnstile 没自动过。脚本在 CI 里
  有 90 秒等待窗口；超时就报 failure 让你去面板点一下，而不是假装成功。
- **`API 续期未真正完成`** → 点了 Renew 但 renewal 字段没变，脚本会重新查询确认，
  所以不会出现「假成功」。
- **Telegram 收不到** → 先确认 `TG_BOT_TOKEN`/`TG_CHAT_ID` 都配了；没配只打日志。
  TG 是直连的，跟代理没关系。

<details>
<summary>历史：为什么用「两阶段认证」（点开）</summary>

阶段一在本地跑一次，人工过 Turnstile，把 cookies 存成 `session.json`；
阶段二在 CI 里直接复用 cookie 打 API，完全跳过登录页，从而绕开 Cloudflare Turnstile。

历史上还试过 `cloakbrowser[geoip]` 顶替浏览器，但它的上游 binary v146 在
ubuntu-24.04 runner 上 `ensure_binary` 一跑就 segfault（exit 139），
2026-09-16 起改用 Playwright chromium，`launch()` 保持 API 兼容位。

`requirements.txt` 现在是 `playwright` / `requests` / `PySocks`。

</details>
