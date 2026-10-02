#!/usr/bin/env bash
# Zampto 出口准备：把 runner 的裸出口换成「没被 Zampto 标记」的节点。
#
# 背景（2026-09-26 起）：Zampto 上线反 VPN/代理侦测。被标记的出口 IP 对所有
#   /api/* 回 403 {"error":"Access blocked","reason":"VPN or proxy detected"}，
#   页面也会被重定向到 /blocked（实测旧 WARP 型节点 104.28.x.x 全段被封）。
# 未登录探针即可区分两种 403：
#   干净出口 -> {"success":false,"message":"Unauthorized"} + 页面 307 -> /auth/login
#   被标记   -> "...Access blocked..."                  + 页面 307 -> /blocked
# 所以这里启动 sing-box 后先探针验出口，不合格就换下一个候选，最后才考虑直连。
#
# 注意：任何情况下都不打印节点 URI 本身（含凭据），只打印出口 IP。
#
# 成功时把下面几个变量写进 $GITHUB_ENV 供后续步骤继承：
#   PROXY_OK=0/1, ALL_PROXY / HTTP_PROXY / HTTPS_PROXY = socks5h://127.0.0.1:1080
# 失败（候选与直连都被标记）时 exit 1 —— 续期必失败，早死早超生。
#
# 依赖：curl unzip tar file python3（ubuntu-latest 自带）+ /tmp 可写。
#
# 从 .github/workflows/zampto.yml 的 run: | 块原样搬出来 —— 换到 renew-kit 的
# composite action 之后，这段只能以 setup-command 的形式挂上去，塞在 YAML 里
# 既没法 bash -n 也没法单独跑。搬出来之后它能被 bash -n / shellcheck 检查。
set -e

# 兼容两个 secret 名: PROXY_URI 优先, TUIC_URI 兜底
NODE_LINK="${PROXY_URI:-$TUIC_URI}"
if [ -z "$NODE_LINK" ] && [ -z "$TUIC_URI" ]; then
  echo "[ERROR] 代理节点未配置! GitHub 裸 IP 会被 Cloudflare 风控(403), 续期必失败"
  echo "请到 Settings -> Secrets 添加 PROXY_URI (或兼容的 TUIC_URI), 支持格式:"
  echo "  hysteria2://password@host:port?sni=...&insecure=1"
  echo "  hy2://password@host:port?..."
  echo "  tuic://uuid:password@host:port?..."
  echo "  vless://uuid@host:port?..."
  exit 1
fi

echo "[1/4] 写出节点解析器 /tmp/parse_node.py ..."
cat > /tmp/parse_node.py << 'PYEOF'
import urllib.parse, json, os, sys, base64

link = os.environ.get("NODE_LINK", "").strip()
if not link:
    sys.exit(1)

proto = "hysteria2"
for prefix in ["hysteria2://", "hy2://", "tuic://", "vless://", "vmess://"]:
    if link.startswith(prefix):
        link = link[len(prefix):]
        proto = {"tuic://": "tuic", "vless://": "vless", "vmess://": "vmess"}.get(prefix, "hysteria2")
        break
link = link.split("#", 1)[0]
if "?" in link:
    main, query = link.split("?", 1)
else:
    main, query = link, ""
params = urllib.parse.parse_qs(query)
sni = params.get("sni", [""])[0]
insecure = any(params.get(k, ["0"])[0] in ("1", "true", "yes")
               for k in ("insecure", "allowInsecure", "allow_insecure"))
alpn = params.get("alpn", [""])[0]
security = params.get("security", [""])[0]
flow = params.get("flow", [""])[0]
public_key = params.get("pbk", [""])[0]
fingerprint = params.get("fp", ["chrome"])[0]
short_id = params.get("sid", [""])[0]
spider_x = urllib.parse.unquote(params.get("spx", [""])[0])

out = {"type": proto, "tag": "proxy-out"}
if proto == "vmess":
    b64 = main + "=" * ((4 - len(main) % 4) % 4)
    vm = json.loads(base64.b64decode(b64).decode("utf-8", "ignore"))
    out["server"] = vm.get("add", "")
    out["server_port"] = int(vm.get("port", 0))
    out["uuid"] = vm.get("id", "")
    out["alter_id"] = int(vm.get("aid", "0"))
    out["security"] = vm.get("scy", "auto") or "auto"
    vm_host = vm.get("host", "") or vm.get("sni", "") or out["server"]
    insecure = insecure or vm.get("insecure", "0") in ("1", "true", "yes")
    out["tls"] = {"enabled": False, "server_name": sni or vm_host, "insecure": insecure}
    if vm.get("tls") == "tls" or vm.get("sni") or vm.get("host"):
        out["tls"]["enabled"] = True
        out["tls"]["server_name"] = sni or vm_host
        # uTLS 指纹: 优先 vmess JSON 里的 fp, 再退回到链接 query 的 fp
        vm_fp = vm.get("fp", "") or fingerprint
        if vm_fp:
            out["tls"]["utls"] = {"enabled": True, "fingerprint": vm_fp}
        vm_alpn = vm.get("alpn", "")
        if vm_alpn:
            out["tls"]["alpn"] = [x.strip() for x in vm_alpn.split(",") if x.strip()]
    # 传输层: ws / grpc / http / tcp
    net = (vm.get("net", "tcp") or "tcp").lower()
    if net == "ws":
        # sing-box 1.10.7 拒绝带 query string 的 ws path (会回 404),
        # 实测节点服务器对裸 path 同样接受, 因此这里剥掉 "?..."
        out["transport"] = {
            "type": "ws",
            "path": vm.get("path", "/").split("?", 1)[0] or "/",
            "headers": {"Host": vm_host},
        }
    elif net == "grpc":
        out["transport"] = {"type": "grpc", "service_name": vm.get("path", "")}
    elif net == "http":
        out["transport"] = {"type": "http", "path": vm.get("path", "/"), "host": vm_host}
else:
    password_raw, server_part = main.rsplit("@", 1)
    password = urllib.parse.unquote(password_raw)
    server, port_str = server_part.rsplit(":", 1)
    out["server"] = server
    out["server_port"] = int(port_str)
    if proto == "tuic" or proto == "vless":
        # userinfo = uuid 或 uuid:password
        if ":" in password:
            out["uuid"], out["password"] = password.split(":", 1)
        else:
            out["uuid"] = password
    else:  # hysteria2 / hy2
        out["password"] = password
    out["tls"] = {"enabled": True, "server_name": sni or server, "insecure": insecure}
    if alpn:
        out["tls"]["alpn"] = [alpn]
    if proto == "vless":
        if flow:
            out["flow"] = flow
        if security == "reality":
            if not public_key:
                raise ValueError("VLESS Reality link is missing pbk")
            out["tls"]["reality"] = {
                "enabled": True,
                "public_key": public_key,
                "short_id": short_id
            }
            if fingerprint:
                out["tls"]["utls"] = {"enabled": True, "fingerprint": fingerprint}
            if spider_x:
                out["tls"]["reality"]["spider_x"] = spider_x
        vnet = (params.get("type", ["tcp"])[0] or "tcp").lower()
        vpath = urllib.parse.unquote(params.get("path", ["/"])[0]) or "/"
        if vnet == "ws":
            out["transport"] = {
                "type": "ws",
                "path": vpath.split("?", 1)[0] or "/",
                "headers": {"Host": sni or server},
            }
        elif vnet in ("grpc", "gun"):
            out["transport"] = {"type": "grpc", "service_name": vpath.lstrip("/")}
    elif proto == "tuic":
        out["congestion_control"] = params.get("congestion_control", ["bbr"])[0] or "bbr"
        out["udp_relay_mode"] = params.get("udp_relay_mode", ["native"])[0] or "native"
        if any(params.get(k, ["0"])[0] in ("1", "true", "yes") for k in ("zero_rtt_handshake", "zero_rtt")):
            out["zero_rtt_handshake"] = True
    elif proto == "hysteria2":
        out["up_mbps"] = 100
        out["down_mbps"] = 100

cfg = {
    "log": {"level": "info", "timestamp": True},
    "inbounds": [{"type": "socks", "tag": "socks-in",
                  "listen": "127.0.0.1", "listen_port": 1080}],
    "outbounds": [out],
}
with open("/tmp/sb_config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("Protocol:", proto)
print("Server:", out.get("server", "?"), ":", out.get("server_port", "?"))
print("SNI:", out.get("tls", {}).get("server_name", ""))
print("Insecure:", insecure)
PYEOF

echo "[2/4] Downloading sing-box ..."
SB_VERSION="1.10.7"
SB_URL="https://github.com/SagerNet/sing-box/releases/download/v${SB_VERSION}/sing-box-${SB_VERSION}-linux-amd64.tar.gz"
echo "  URL: $SB_URL"
curl -fSL "$SB_URL" -o /tmp/sb.tar.gz
tar -xzf /tmp/sb.tar.gz -C /tmp/
mv /tmp/sing-box-${SB_VERSION}-linux-amd64/sing-box /tmp/sing-box
chmod +x /tmp/sing-box

FILE_TYPE=$(file /tmp/sing-box 2>/dev/null || echo "unknown")
echo "  File type: $FILE_TYPE"
if ! echo "$FILE_TYPE" | grep -q "ELF"; then
  echo "[ERROR] sing-box binary is not valid ELF"
  exit 1
fi
/tmp/sing-box version

# ── 探针: 出口是否被 Zampto 标记 ─────────────────────────────────────────────
probe_zampto() {   # 用法: probe_zampto [curl 代理参数...]；0=出口干净 1=被标记 2=无响应
  local body page
  body=$(curl -s --max-time 20 "$@" -X POST https://dash.zampto.net/api/server/renew \
           -H 'content-type: application/json' -H 'x-requested-with: XMLHttpRequest' \
           --data '{"server_id":1}' 2>/dev/null || true)
  page=$(curl -s --max-time 20 "$@" -o /dev/null -w '%{redirect_url}' \
           https://dash.zampto.net/ 2>/dev/null || true)
  echo "      探针 API : ${body:0:110}"
  echo "      探针 页面: ${page:-<无跳转>}"
  case "$body $page" in
    *"Access blocked"*|*"/blocked"*) return 1 ;;
  esac
  case "$body" in
    "") return 2 ;;
  esac
  return 0
}

start_node() {   # $1 = 节点 URI；起 sing-box 并验证代理本身可通
  pkill -f '/tmp/sing-box run' 2>/dev/null || true
  sleep 1
  if ! NODE_LINK="$1" python3 /tmp/parse_node.py > /dev/null 2>&1; then
    echo "      ✗ 解析节点失败"
    return 1
  fi
  if ! /tmp/sing-box check -c /tmp/sb_config.json > /dev/null 2>&1; then
    echo "      ✗ sing-box 配置校验不过"
    return 1
  fi
  nohup /tmp/sing-box run -c /tmp/sb_config.json > /tmp/tuic.log 2>&1 &
  sleep 8
  if curl -s --max-time 12 --socks5-hostname 127.0.0.1:1080 https://www.google.com -o /dev/null 2>/dev/null; then
    return 0
  fi
  echo "      ✗ 代理连不通 (sing-box log 尾部):"
  tail -5 /tmp/tuic.log 2>/dev/null || true
  return 1
}

echo "[3/4] 依次尝试候选出口 (PROXY_URI -> TUIC_URI) ..."
USE_PROXY=0
CHOSEN_IP=""
for cand in "primary" "alt"; do
  if [ "$cand" = "primary" ]; then URI="$PROXY_URI"; else URI="$TUIC_URI"; fi
  [ -n "$URI" ] || continue
  [ "$cand" = "alt" ] && [ "$TUIC_URI" = "$PROXY_URI" ] && continue
  echo "  [try] 候选出口: $cand"
  if ! start_node "$URI"; then
    echo "      ✗ 候选 $cand 不可用, 换下一个"
    continue
  fi
  EXIT_IP=$(curl -s --max-time 15 --socks5 127.0.0.1:1080 https://ifconfig.me || echo 'fail')
  echo "      Proxy IP: $EXIT_IP"
  probe_zampto --socks5-hostname 127.0.0.1:1080 && probe_rc=0 || probe_rc=$?
  if [ "$probe_rc" = "0" ]; then
    USE_PROXY=1
    CHOSEN_IP="$EXIT_IP"
    echo "      ✅ 出口未被 Zampto 标记, 采用候选 $cand"
    break
  elif [ "$probe_rc" = "1" ]; then
    echo "      ✗ 出口被 Zampto 标记 (VPN or proxy detected), 换下一个候选"
  else
    echo "      ⚠️ 探针无响应（网络层不确定），仍沿用本候选"
    USE_PROXY=1
    CHOSEN_IP="$EXIT_IP"
    break
  fi
done

if [ "$USE_PROXY" = "1" ]; then
  echo "PROXY_OK=1" >> $GITHUB_ENV
  echo "ALL_PROXY=socks5h://127.0.0.1:1080" >> $GITHUB_ENV
  echo "HTTP_PROXY=socks5h://127.0.0.1:1080" >> $GITHUB_ENV
  echo "HTTPS_PROXY=socks5h://127.0.0.1:1080" >> $GITHUB_ENV
  echo "[OK] Proxy is ready (Zampto 探针通过, 出口 $CHOSEN_IP)"
else
  echo "[WARN] 所有代理候选都被标记/不可用, 试直连 (GitHub 出口 IP)..."
  probe_zampto && direct_rc=0 || direct_rc=$?
  if [ "$direct_rc" = "0" ]; then
    echo "[OK] 直连出口未被标记 — 本次不使用代理"
    echo "PROXY_OK=0" >> $GITHUB_ENV
    CHOSEN_IP="direct"
  else
    echo "[ERROR] 代理候选与直连都被 Zampto 标记/不可用 — 续期必失败。"
    echo "        对策: 换一条非 WARP/VPN 段的节点写进 PROXY_URI secret, 或人手去面板续期。"
    echo "PROXY_OK=0" >> $GITHUB_ENV
    exit 1
  fi
fi

# ── 诊断矩阵（只印 HTTP 状态码，绝不打印响应体 / cookie）─────────────────────
if [ "${ZAMPTO_DIAG:-1}" = "1" ]; then
  echo "── Zampto 四格诊断矩阵 (状态码; AUTH = 带 session cookie) ──"
  AUTH_COOKIE=""
  if [ -n "$ZAMPTO_SESSION_SECRET" ]; then
    AUTH_COOKIE=$(python3 - << 'PYEOF'
import os, base64, json
try:
    d = json.loads(base64.b64decode(os.environ["ZAMPTO_SESSION_SECRET"]).decode())
    out = []
    for c in d.get("cookies", []):
        if isinstance(c, dict) and c.get("name"):
            out.append(f"{c['name']}={c.get('value','')}")
    print("; ".join(out))
except Exception:
    print("")
PYEOF
)
  fi
  if [ "$USE_PROXY" = "1" ]; then PROXY_ARGS="--socks5-hostname 127.0.0.1:1080"; else PROXY_ARGS=""; fi
  for mode in proxy direct; do
    [ "$mode" = "proxy" ] && [ "$USE_PROXY" != "1" ] && continue
    if [ "$mode" = "proxy" ]; then PA="--socks5-hostname 127.0.0.1:1080"; else PA=""; fi
    for auth in auth noauth; do
      if [ "$auth" = "auth" ] && [ -z "$AUTH_COOKIE" ]; then echo "  $mode/$auth: 跳过(无 cookie)"; continue; fi
      if [ "$auth" = "auth" ]; then CA=(-H "cookie: $AUTH_COOKIE"); else CA=(); fi
      cd_=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 $PA "${CA[@]}" \
              -X POST https://dash.zampto.net/api/server/renew \
              -H 'content-type: application/json' -H 'x-requested-with: XMLHttpRequest' \
              --data '{"server_id":1}' 2>/dev/null || echo ERR)
      pg_=$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 20 $PA "${CA[@]}" https://dash.zampto.net/ 2>/dev/null || echo ERR)
      echo "  $mode/$auth: renew=$cd_  page→${pg_##*/}"
    done
  done
  echo "── Cloudflare 眼中嘅出口 (ip/loc/warp) ──"
  if [ "$USE_PROXY" = "1" ]; then
    echo "  经代理: $(curl -s --max-time 15 --socks5-hostname 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -E '^(ip|loc|warp)=' | tr '\n' ' ')"
  fi
  echo "  直连  : $(curl -s --max-time 15 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -E '^(ip|loc|warp)=' | tr '\n' ' ')"
fi

{
  echo "### Zampto 出口探针"
  echo "- 采用出口: $CHOSEN_IP (代理=$USE_PROXY)"
  echo "- 探针语义: 干净出口回 Unauthorized / 307->/auth/login; 被标记回 Access blocked / 307->/blocked"
} >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || true

echo "[4/4] 代理步骤完成"
