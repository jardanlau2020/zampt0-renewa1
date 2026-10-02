#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zampt0-renewa1 的离线验证器 —— 不需要浏览器、不需要网络、不需要 secrets。

四组：
    [A] 纯函数          renewal_to_expiry / _fmt_remaining / _new_report /
                        find_csrf_cookie / _human_summary / mask_headers
    [B] 场景矩阵        main() 在各种 outcome 下的退出码与通知条数
    [C] 静态与接线      zampto_auto.py 的迁移不变量 + workflow + README + scripts
    [D] 真子进程        py_compile / import / main() 退出码 / bash -n / YAML 解析

跑法（仓库根）：
    python .verify/verify_zampto.py

CI 里 renewkit 是 pip 装的；本机跑时脚本会自己去 _sync/../renew-kit 和 _deps 找。
找不到就报清楚的一行，而不是丢一个 ModuleNotFoundError 出来。
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                  # 仓库根
WORKSPACE = ROOT.parent.parent                      # 工作区（本机才有）
APP = ROOT / "zampto_auto.py"
WORKFLOW = ROOT / ".github" / "workflows" / "zampto.yml"
README = ROOT / "README.md"
SETUP_PROXY = ROOT / "scripts" / "setup_proxy.sh"
RUN_SH = ROOT / "scripts" / "run_zampto.sh"

for _cand in (ROOT, WORKSPACE / "renew-kit", WORKSPACE / "_deps"):
    if Path(_cand).is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

APP_SRC = APP.read_text(encoding="utf-8")
WF_SRC = WORKFLOW.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════ 基础设施

_pass = 0
_fail: list[tuple[str, str]] = []
_group = ""


def check(label: str, cond, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
    else:
        _fail.append((_group, label + (f"  [{detail}]" if detail else "")))
        print(f"  \u274c {label}" + (f"  [{detail}]" if detail else ""))


def eq(label: str, got, want) -> None:
    check(label, got == want, f"got={got!r} want={want!r}")


def section(name: str) -> None:
    global _group
    _group = name
    print(f"\n{name}")


def code_only(src: str) -> str:
    """把注释与字符串字面量就地挖空，保留行列。

    为什么必须保留行列：本文件里大量断言靠「某行出现某符号」定位，位置一挪
    断言就失效了。挖空之后 `'parse_mode="HTML"'` 这类含引号的断言永远不可能
    命中 —— 那种断言要打在 APP_SRC 上，不是 CODE 上。
    """
    import tokenize

    lines = src.splitlines(keepends=True)
    out = [list(line) for line in lines]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (sr, sc), (er, ec) = tok.start, tok.end
            if sr == er:
                for i in range(sc, ec):
                    if out[sr - 1][i] not in "\r\n":
                        out[sr - 1][i] = " "
            else:
                for i in range(sc, len(out[sr - 1])):
                    if out[sr - 1][i] not in "\r\n":
                        out[sr - 1][i] = " "
                for r in range(sr, er - 1):
                    for i in range(len(out[r])):
                        if out[r][i] not in "\r\n":
                            out[r][i] = " "
                for i in range(0, ec):
                    if out[er - 1][i] not in "\r\n":
                        out[er - 1][i] = " "
    except Exception:
        pass
    return "".join("".join(r) for r in out)


CODE = code_only(APP_SRC)
WF_CODE = code_only(WF_SRC)


@contextlib.contextmanager
def env_patch(**envs):
    """临时改 os.environ（None 表示删除），退出时整体还原。"""
    old = dict(os.environ)
    try:
        for k, v in envs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


@contextlib.contextmanager
def stdout():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def fresh_app(**envs):
    """按给定环境重新导入 zampto_auto。

    模块级 globals（USERNAME / SERVER_ID / RENEW_THRESHOLD_HOURS）是 import
    时读的，所以每个场景都得重载，不然第二个场景还在用第一个的凭据。
    """
    envs.setdefault("ZAMPTO_USERNAME", "u@example.com")
    envs.setdefault("ZAMPTO_PASSWORD", "pw")
    envs.setdefault("ZAMPTO_SERVER_ID", "15629")
    envs.setdefault("TG_BOT_TOKEN", None)
    envs.setdefault("TG_CHAT_ID", None)
    with env_patch(**envs):
        if "zampto_auto" in sys.modules:
            mod = importlib.reload(sys.modules["zampto_auto"])
        else:
            mod = importlib.import_module("zampto_auto")
    return mod


class NotifySpy:
    """替换 renewkit.notify.send：只记账，不发网络。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, text, **kw):
        self.calls.append((text, kw))
        return True

    @property
    def texts(self) -> str:
        return "\n\n".join(t for t, _ in self.calls)


@contextlib.contextmanager
def notify_spy():
    from renewkit import notify

    spy = NotifySpy()
    old = notify.send
    notify.send = spy
    try:
        yield spy
    finally:
        notify.send = old


@contextlib.contextmanager
def forbid_network():
    """任何真实 Telegram 请求都算失败。"""
    from renewkit import notify

    def boom(req, timeout=None):
        raise AssertionError(f"竟然发了真请求: {req.full_url}")

    old = notify.urllib.request.urlopen
    notify.urllib.request.urlopen = boom
    try:
        yield
    finally:
        notify.urllib.request.urlopen = old


def session_secret(cookies=None) -> str:
    payload = {"cookies": cookies if cookies is not None else [
        {"name": "zampto_session", "value": "x" * 40, "domain": "dash.zampto.net"},
    ]}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def which_bash() -> str | None:
    """取 bash 的绝对路径。

    Windows 上不能直接用裸名 "bash"：本机 PATH 里有个 safe-bin shim 会先接住
    它，返回一个 rc=1 + 五个 '?' 的东西（既不是语法错误也不是成功），
    看起来像「脚本有问题」其实是没跑起来。
    """
    return os.environ.get("ZAMPTO_BASH") or shutil.which("bash")


# ═══════════════════════════════════════════════════════════ [A] 纯函数

def group_a() -> None:
    section("[A] 纯函数")
    app = fresh_app()

    # ---- renewal_to_expiry：Zampto 的 renewal 是「上次续期时间」不是到期时间
    eq("A1  renewal_to_expiry(None) -> (None, None)", app.renewal_to_expiry(None), (None, None))
    eq("A2  renewal_to_expiry('') -> (None, None)", app.renewal_to_expiry(""), (None, None))
    eq("A3  renewal_to_expiry(乱码) -> (None, None)",
       app.renewal_to_expiry("not-a-date"), (None, None))
    eq("A4  renewal_to_expiry(日期格式但非法) -> (None, None)",
       app.renewal_to_expiry("2026-13-45T99:99:99"), (None, None))

    raw = "2026-07-30T12:28:33.000Z"
    iso, hours = app.renewal_to_expiry(raw)
    want_exp = datetime(2026, 7, 30, 12, 28, 33, tzinfo=timezone.utc) + timedelta(hours=48)
    check("A5  renewal_to_expiry 把 renewal 当起点 +48h",
          iso == want_exp.isoformat(), f"got={iso!r}")
    check("A6  renewal_to_expiry 返回的到期时间比 renewal 晚 48h",
          datetime.fromisoformat(iso) - datetime(2026, 7, 30, 12, 28, 33, tzinfo=timezone.utc)
          == timedelta(hours=48))
    check("A7  renewal_to_expiry hours 是 int", isinstance(hours, int), f"got={type(hours)}")

    # 一个「刚续期」的 renewal：到期应该还有 ~48h，而不是「已过期 3 个月」
    fresh_raw = datetime.now(timezone.utc).isoformat()
    _, fresh_hours = app.renewal_to_expiry(fresh_raw)
    check("A8  刚续期 -> 剩余 ~48h（不是负的）", 47 <= fresh_hours <= 48, f"got={fresh_hours}")
    stale_raw = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    _, stale_hours = app.renewal_to_expiry(stale_raw)
    check("A9  100h 前续期 -> 剩余为负（已过期）", stale_hours < 0, f"got={stale_hours}")

    # ---- _fmt_remaining
    #
    # 这里不逐字比对：_fmt_remaining 内部自己取 datetime.now()，和构造输入时的
    # now() 差几微秒，int() 一截断就常常少 1 分钟。比秒数、给 90s 容差。
    def rem_seconds(s: str):
        if not s or "min" not in s:
            return None
        m = re.match(r"^(?:(\d+)d )?(?:(\d+)h )?(\d+)min$", s)
        if not m:
            return None
        d, h, mi = (int(x or 0) for x in m.groups())
        return d * 86400 + h * 3600 + mi * 60

    def near(got, want_s, label, tol=90):
        g = rem_seconds(got)
        check(label, g is not None and abs(g - want_s) <= tol, f"got={got!r} want≈{want_s}s")

    eq("A10 _fmt_remaining('') -> ''", app._fmt_remaining(""), "")
    eq("A11 _fmt_remaining(None) -> ''", app._fmt_remaining(None), "")
    eq("A12 _fmt_remaining(垃圾) -> ''", app._fmt_remaining("nope"), "")
    future = (datetime.now(timezone.utc) + timedelta(days=1, hours=12, minutes=30)).isoformat()
    near(app._fmt_remaining(future), 131400, "A13 _fmt_remaining(1d12h30m)")
    check("A13b 1d12h30m 三个单位都在",
          app._fmt_remaining(future).startswith("1d 12h"), app._fmt_remaining(future))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    eq("A14 _fmt_remaining(过去) -> 已到期", app._fmt_remaining(past), "已到期")
    soon = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
    near(app._fmt_remaining(soon), 2700, "A15 _fmt_remaining(45min)")
    check("A15b 45min 不带 d/h 前缀",
          "d" not in app._fmt_remaining(soon) and "h" not in app._fmt_remaining(soon),
          app._fmt_remaining(soon))
    naive = (datetime.now(timezone.utc) + timedelta(hours=2, minutes=5)).replace(tzinfo=None)
    near(app._fmt_remaining(naive.isoformat()), 7500, "A16 _fmt_remaining(naive ISO)")
    check("A16b naive ISO 按 UTC 而不是本地时区",
          app._fmt_remaining(naive.isoformat()).startswith("2h"), app._fmt_remaining(naive.isoformat()))

    # ---- _new_report：字段齐全且默认 transient=False
    r = app._new_report()
    for field in ("server_id", "status", "action", "expiry", "expire_at",
                  "hours_left", "error", "transient", "timestamp"):
        check(f"A17 _new_report 含字段 {field}", field in r, f"keys={sorted(r)}")
    eq("A18 _new_report 默认 action='none'", r["action"], "none")
    eq("A19 _new_report 默认 transient=False", r["transient"], False)
    eq("A20 _new_report 默认 error=None", r["error"], None)
    eq("A21 _new_report(**over) 覆盖默认值", app._new_report(action="renewed")["action"], "renewed")
    eq("A22 _new_report 不污染下一次调用", app._new_report()["action"], "none")
    eq("A23 _now_iso 带 UTC 时区", app._now_iso()[-6:], "+00:00")

    # ---- find_csrf_cookie
    eq("A24 find_csrf_cookie 空列表 -> None", app.find_csrf_cookie([]), None)
    eq("A25 find_csrf_cookie 无 csrf -> None",
       app.find_csrf_cookie([{"name": "a", "value": "1"}]), None)
    eq("A26 find_csrf_cookie 命中 zampto_csrf",
       app.find_csrf_cookie([{"name": "zampto_csrf", "value": "TOK"}]), "TOK")
    eq("A27 find_csrf_cookie 大小写不敏感",
       app.find_csrf_cookie([{"name": "CSRF_TOKEN", "value": "T2"}]), "T2")
    eq("A28 find_csrf_cookie 取第一个命中",
       app.find_csrf_cookie([{"name": "csrf_a", "value": "1"},
                             {"name": "csrf_b", "value": "2"}]), "1")
    eq("A28b find_csrf_cookie 不吃 XSRF（名字里没有 csrf）",
       app.find_csrf_cookie([{"name": "XSRF-TOKEN", "value": "T3"}]), None)

    # ---- mask_headers：public repo 的 log 谁都能看
    masked = app.mask_headers({"X-CSRF-Token": "s" * 129, "Accept": "application/json"})
    check("A29 mask_headers 遮住 csrf 值", "s" * 129 not in json.dumps(masked), f"got={masked}")
    check("A30 mask_headers 保留 csrf 长度", "len=129" in masked["X-CSRF-Token"])
    eq("A31 mask_headers 不动普通 header", masked["Accept"], "application/json")
    eq("A32 mask_headers(None) 原样返回", app.mask_headers(None), None)
    for h in ("authorization", "cookie", "set-cookie"):
        m = app.mask_headers({h: "SECRETVALUE"})
        check(f"A33 mask_headers 遮住 {h}", "SECRETVALUE" not in json.dumps(m))

    # ---- _human_summary：给人看的诊断块
    rep = app._new_report(action="renewed", status="running", server_id="15629",
                          expire_at="2026-10-04T12:00:00+00:00", hours_left=47)
    txt = app._human_summary(rep, app.Outcome.RENEWED, 47)
    check("A34 _human_summary 含服务器 ID", "15629" in txt, txt[:120])
    check("A35 _human_summary 含结果", "renewed" in txt)
    check("A36 _human_summary 含到期小时数", "47h" in txt)
    rep2 = app._new_report(status="stopped", error="Captcha required: xxx")
    txt2 = app._human_summary(rep2, app.Outcome.FAILED, None)
    check("A37 _human_summary 状态 stopped 说人话", "已停止" in txt2)
    check("A38 _human_summary captcha 时提示手动续期", "请手动续期" in txt2)


# ═══════════════════════════════════════════════════════════ [B] 场景矩阵

def group_b() -> None:
    section("[B] 分类矩阵与 main() 场景")
    app = fresh_app()

    # ---- classify：单一裁决口
    M = [
        # (transient, action, error, 期望, 说明)
        (True, "none", "", app.Outcome.TRANSIENT, "显式 transient 标记最优先"),
        (True, "none", "VPN or proxy detected", app.Outcome.TRANSIENT, "transient 先于 blocked"),
        (True, "renewed", "", app.Outcome.TRANSIENT, "transient 先于 action"),
        (False, "renewed", "", app.Outcome.RENEWED, "续期成功"),
        (False, "renewed", "noise", app.Outcome.RENEWED, "action 压过无关错误文本"),
        (False, "skipped", "", app.Outcome.SKIPPED, "未到窗口"),
        (False, "skipped", "502 Bad Gateway", app.Outcome.TRANSIENT, "上游 502"),
        (False, "skipped", "boom", app.Outcome.FAILED, "有错就不是 skipped"),
        (False, "none", "VPN or proxy detected", app.Outcome.FAILED, "出口被标记 -> 要换节点"),
        (False, "none", "Access blocked", app.Outcome.FAILED, "Access blocked"),
        (False, "none", "/blocked", app.Outcome.FAILED, "被重定向到 /blocked"),
        (False, "none", "502 Bad Gateway", app.Outcome.TRANSIENT, "bad gateway"),
        (False, "none", "connection reset by peer", app.Outcome.TRANSIENT, "连接被重置"),
        (False, "none", "Read timed out", app.Outcome.TRANSIENT, "超时"),
        (False, "none", "Captcha required: x", app.Outcome.FAILED, "验证码挡路 -> 人工"),
        (False, "none", "", app.Outcome.FAILED, "什么都不知道就是失败"),
        (False, "started", "", app.Outcome.FAILED, "只启动了、没续上"),
        (False, "skipped", "面板 API 全部探测失败（5xx=0 风控=1 网络=0）",
         app.Outcome.FAILED, "全是风控 -> 失败"),
    ]
    for i, (transient, action, err, want, why) in enumerate(M, 1):
        got = app.classify({"transient": transient, "action": action, "error": err})
        check(f"B1.{i:<2} classify({action!r}, err={err[:22]!r}) -> {want.value}", got is want, f"got={got.value} · {why}")

    # 403 风控 vs 上游 5xx 的 transient 计算规则（phase_api_renewal 的探针出口）
    for fivexx, blocked, net, want in [(3, 0, 0, True), (0, 0, 2, True), (0, 1, 0, False),
                                       (2, 1, 0, False), (0, 0, 0, False)]:
        got = (fivexx + net) > 0 and blocked == 0
        check(f"B2.2 探针 5xx={fivexx} 风控={blocked} 网络={net} -> transient={want}", got is want)

    # ---- _to_renew_report：渲染
    rr = app._to_renew_report(app._new_report(action="renewed", status="running",
                                              expire_at="2026-10-04T12:00:00+00:00",
                                              hours_left=47))
    text = rr.render()
    check("B3.1 续期成功渲染带 ✅", "✅" in text, text)
    check("B3.2 续期成功退出码 0", rr.exit_code == 0)
    check("B3.3 续期成功含服务名", "Zampto" in text)

    rr = app._to_renew_report(app._new_report(action="skipped", status="running",
                                              expire_at="2026-10-04T12:00:00+00:00",
                                              hours_left=40))
    check("B3.4 跳过渲染带 🟢", "🟢" in rr.render(), rr.render())
    check("B3.5 跳过退出码 0", rr.exit_code == 0)

    rr = app._to_renew_report(app._new_report(error="Captcha required: x"))
    check("B3.6 失败渲染带 🚨", "🚨" in rr.render(), rr.render())
    check("B3.7 失败退出码 1", rr.exit_code == 1)
    check("B3.8 失败提示登录面板", "手动处理" in rr.render())

    rr = app._to_renew_report(app._new_report(error="502 Bad Gateway"))
    check("B3.9 上游故障渲染带 🌐", "🌐" in rr.render(), rr.render())
    check("B3.10 上游故障退出码 0（不标红）", rr.exit_code == 0)

    rr = app._to_renew_report(app._new_report(error="VPN or proxy detected"))
    check("B3.11 出口被标记 -> 🚨 且退出码 1",
          "🚨" in rr.render() and rr.exit_code == 1, rr.render())

    # 报告目标名带 server id
    rr = app._to_renew_report(app._new_report(action="renewed", status="running",
                                              server_id="15629"))
    check("B3.12 目标名含 server_id", "15629" in rr.render(), rr.render())

    # 长错误被 shorten 截断（报告不撑爆通知）
    rr = app._to_renew_report(app._new_report(error="E" * 400))
    check("B3.13 超长 error 被截断", "E" * 200 not in rr.render())

    # ---- main()：退出码 + 通知条数
    #
    # main() 会把 report.json 写到 ./screenshots —— 整段切到临时目录，别污染仓库。
    # （后面的 [C]/[D] 全用绝对路径，不依赖 cwd。）
    scratch = Path(tempfile.mkdtemp(prefix="zampto-b-"))
    atexit.register(shutil.rmtree, scratch, True)
    os.chdir(scratch)

    def run_main(app, *, browser="failed", api_report=None, secret=None, extra=None):
        """跑一次 main()，返回 (退出码, stdout)。

        输出由 run_main 自己捕获后回传，而不是让调用方再套一层
        redirect_stdout —— 嵌套 redirect 会让外层那个永远读到空串。
        """
        app.phase_browser_renewal = lambda cookies=None: browser
        if api_report is not None:
            app.phase_api_renewal = lambda use_cookies=None: app._to_renew_report(api_report)
        app._query_expiry = lambda cookies, wait_for_fresh: (
            "2026-10-04T12:00:00+00:00", 47, "1d 23h 0min")
        env = {"CI": "true", "GITHUB_ACTION": "1",
               "ZAMPTO_SESSION_SECRET": secret if secret is not None else session_secret()}
        if extra:
            env.update(extra)
        with env_patch(**env):
            with stdout() as buf:
                code = app.main()
        return code, buf.getvalue()

    # B4 浏览器成功
    with notify_spy() as spy:
        code, _ = run_main(app, browser="renewed")
    eq("B4.1 浏览器 renewed -> exit 0", code, 0)
    eq("B4.2 浏览器 renewed -> 恰好 1 条通知", len(spy.calls), 1)
    check("B4.3 通知说续期成功", "成功续期" in spy.texts, spy.texts[:200])

    # B5 浏览器跳过
    with notify_spy() as spy:
        code, _ = run_main(app, browser="skipped")
    eq("B5.1 浏览器 skipped -> exit 0", code, 0)
    eq("B5.2 浏览器 skipped -> 恰好 1 条通知", len(spy.calls), 1)
    check("B5.3 通知说状态良好", "状态良好" in spy.texts, spy.texts[:200])

    # B6 浏览器失败 -> API 成功
    with notify_spy() as spy:
        code, _ = run_main(app, browser="failed",
                           api_report=app._new_report(action="renewed", status="running",
                                                      hours_left=47))
    eq("B6.1 浏览器失败+API 成功 -> exit 0", code, 0)
    eq("B6.2 只发 1 条（旧版会发 2 条）", len(spy.calls), 1)
    check("B6.3 内容是成功", "成功续期" in spy.texts, spy.texts[:200])

    # B7 浏览器失败 -> API 上游故障
    with notify_spy() as spy:
        code, _ = run_main(app, browser="failed",
                           api_report=app._new_report(error="502 Bad Gateway"))
    eq("B7.1 上游故障 -> exit 0（不标红）", code, 0)
    eq("B7.2 上游故障 -> 恰好 1 条通知", len(spy.calls), 1)
    check("B7.3 通知说上游暂不可用", "上游暂不可用" in spy.texts, spy.texts[:200])

    # B8 浏览器失败 -> API 真失败
    with notify_spy() as spy:
        code, _ = run_main(app, browser="failed",
                           api_report=app._new_report(error="Captcha required: x"))
    eq("B8.1 真失败 -> exit 1", code, 1)
    eq("B8.2 真失败 -> 恰好 1 条通知（旧版 2 条）", len(spy.calls), 1)
    check("B8.3 通知说续期未完成", "续期未完成" in spy.texts, spy.texts[:200])
    check("B8.4 通知带需要人工", "手动处理" in spy.texts)

    # B9 缺凭据。注意 fresh_app 是 importlib.reload —— 它改的是同一个模块对象，
    # 所以跑完必须重新 fresh_app() 拿回有凭据的那个 app，否则后面全场景都会
    # 卡在凭据校验上（第一版就踩了这个坑）。
    app = fresh_app(ZAMPTO_USERNAME="", ZAMPTO_PASSWORD="", ZAMPTO_SERVER_ID="")
    with notify_spy() as spy:
        code, _ = run_main(app)
    eq("B9.1 缺凭据 -> exit 1", code, 1)
    eq("B9.2 缺凭据 -> 恰好 1 条通知", len(spy.calls), 1)
    check("B9.3 提示缺哪些 secret", "ZAMPTO_SERVER_ID" in spy.texts, spy.texts[:200])
    app = fresh_app()

    # B10 session secret 解碼失敗
    with notify_spy() as spy:
        code, _ = run_main(app, secret="!!!not-base64!!!")
    eq("B10.1 坏 secret -> exit 1", code, 1)
    eq("B10.2 坏 secret -> 恰好 1 条通知", len(spy.calls), 1)
    check("B10.3 提示 secret 格式", "解碼失敗" in spy.texts, spy.texts[:200])

    # B11 secret 是合法 base64 但没有 cookies
    with notify_spy() as spy:
        code, _ = run_main(app, secret=session_secret(cookies=[]))
    eq("B11.1 空 cookies -> exit 1", code, 1)
    eq("B11.2 空 cookies -> 恰好 1 条通知", len(spy.calls), 1)

    # B12 DRY_RUN：不碰网络（这里不替换 notify.send，测的是真闸门）
    with forbid_network():
        code, out = run_main(app, browser="failed",
                             api_report=app._new_report(error="Captcha required: x"),
                             extra={"DRY_RUN": "1"})
    eq("B12.1 演练下业务失败仍 exit 1", code, 1)
    check("B12.2 演练打出了预览", "DRY_RUN" in out, out[:200])
    check("B12.3 演练预览含报告原文", "续期未完成" in out)
    eq("B12.4 演练下报告只出现一次", out.count("续期未完成"), 1)

    # B13 report.json 落盘
    with notify_spy():
        run_main(app, browser="renewed")
    p = scratch / "screenshots" / "report.json"
    check("B13.1 report.json 落盘", p.exists(), str(p))
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        check("B13.2 report.json 含 action", data.get("action") == "renewed", str(data)[:120])
        check("B13.3 report.json 含 server_id", data.get("server_id") == "15629", str(data)[:120])


# ═══════════════════════════════════════════════════════════ [C] 静态与接线

def group_c() -> None:
    section("[C] 静态与接线")

    # ---- 迁移不变量
    eq("C1  CODE 里没有 os.getenv(", CODE.count("os.getenv("), 0)
    eq("C2  CODE 里没有 os.environ.get(", CODE.count("os.environ.get("), 0)
    check("C3  有 from renewkit import env, notify, timeutil",
          "from renewkit import env, notify, timeutil" in APP_SRC)
    check("C4  有 from renewkit.outcome import Outcome",
          "from renewkit.outcome import Outcome" in APP_SRC)
    check("C5  有 from renewkit.report import RenewReport, shorten",
          "from renewkit.report import RenewReport, shorten" in APP_SRC)
    check("C6  CODE 里没有 def push_tg", "def push_tg" not in CODE)
    check("C7  CODE 里没有 def now_local", "def now_local" not in CODE)
    eq("C8  CODE 里没有 sys.exit(", CODE.count("sys.exit("), 0)
    check("C9  收尾用 os._exit(main())", "os._exit(main())" in CODE)
    check("C10 main() 标注 -> int", "def main() -> int" in CODE)
    check("C11 phase_api_renewal 标注 -> RenewReport",
          "def phase_api_renewal(use_cookies=None) -> RenewReport" in CODE)
    eq("C12 RenewReport( 只在 _to_renew_report 里构造一次", CODE.count("RenewReport("), 1)
    eq("C13 CODE 里没有 notify.send（通知出口唯一）", CODE.count("notify.send("), 0)
    check("C14 _to_renew_report 走 classify()", "classify(report)" in CODE)
    eq("C15 classify( 定义一次", CODE.count("def classify(report)"), 1)
    check("C16 保留了 os._exit 的理由注释", "SystemExit" in APP_SRC and "os._exit" in APP_SRC)
    check("C17 迁移说明写在模块 docstring 里", "迁移到 renew-kit" in APP_SRC)
    check("C18 双告警的修复有注释", "两条 🚨" in APP_SRC or "两条告警" in APP_SRC)
    check("C19 RENEW_THRESHOLD_HOURS 走 env.get_int",
          'env.get_int("RENEW_THRESHOLD_HOURS"' in APP_SRC)
    check("C20 SERVICE 常量存在", 'SERVICE = "Zampto"' in APP_SRC)
    check("C21 有 TRANSIENT_HINTS", "TRANSIENT_HINTS = (" in CODE)
    check("C22 有 BLOCKED_HINTS", "BLOCKED_HINTS = (" in CODE)
    check("C23 风控信号含 vpn or proxy detected",
          "vpn or proxy detected" in APP_SRC.lower())

    # ---- workflow
    check("C30 workflow 引用 renew-kit composite action",
          "jardanlau2020/renew-kit/.github/actions/renew@v0.5.3" in WF_SRC)
    check("C31 workflow renewkit-ref 钉在 v0.5.3", "renewkit-ref: v0.5.3" in WF_SRC)
    eq("C32 不再有裸 pip install -r requirements.txt",
       "pip install -r requirements.txt" in WF_SRC, False)
    eq("C33 不再直接跑 python zampto_auto.py", "python zampto_auto.py" in WF_SRC, False)
    check("C34 走 command: bash scripts/run_zampto.sh",
          "command: bash scripts/run_zampto.sh" in WF_SRC)
    check("C35 setup-command 调 setup_proxy.sh", "bash scripts/setup_proxy.sh" in WF_SRC)
    check("C36 setup-command 装 chromium", "playwright install chromium" in WF_SRC)
    check("C37 setup-continue-on-error 为 false",
          "setup-continue-on-error: 'false'" in WF_SRC)
    check("C38 保留兜底通知", "notify-on-failure: 'true'" in WF_SRC)
    check("C39 失败产物路径含 screenshots", "./screenshots/*.png" in WF_SRC)
    check("C40 三个 dispatch 输入齐全",
          all(k in WF_SRC for k in ("force_renew:", "enable_recording:", "dry_run:")))
    check("C41 dry_run 映射到 DRY_RUN", "DRY_RUN: ${{ inputs.dry_run && '1' || '' }}" in WF_SRC)
    check("C42 保留 cron 每 8 小时", "cron: '0 */8 * * *'" in WF_SRC)
    check("C43 job 超时 20 分钟", "timeout-minutes: 20" in WF_SRC)
    check("C44 保留 Xvfb 相关 env",
          all(k in WF_SRC for k in ('DISPLAY: ":99"', "LIBGL_ALWAYS_SOFTWARE", "GALLIUM_DRIVER")))
    check("C45 校验步不硬要 PROXY_URI（有 TUIC_URI 兜底）",
          'elif [ -n "$TUIC_URI" ]' in WF_SRC)
    check("C46 校验步含 TG 警告分支", "[WARN] Telegram 未配置" in WF_SRC)
    check("C47 录屏产物单独上传", "recording-${{ github.run_number }}" in WF_SRC)
    check("C48 录屏上传清空代理",
          "NO_PROXY: '*'" in WF_SRC)
    check("C49 清理进程步骤保留 sing-box", "pkill -f sing-box" in WF_SRC)
    check("C50 历史背景注释保留", "2026-09-28" in WF_SRC)
    check("C51 不引用已删除的 secrets_chunks.json 作数据源",
          "open(\".github/secrets_chunks.json\")" not in WF_SRC)

    # ---- scripts
    check("C60 setup_proxy.sh 存在", SETUP_PROXY.exists())
    if SETUP_PROXY.exists():
        sp = SETUP_PROXY.read_text(encoding="utf-8")
        check("C61 setup_proxy 写 PROXY_OK", "PROXY_OK=" in sp)
        check("C62 setup_proxy 写 GITHUB_ENV", "$GITHUB_ENV" in sp)
        check("C63 setup_proxy 有 PROXY_URI/TUIC_URI 两级候选",
              "${PROXY_URI:-$TUIC_URI}" in sp)
        check("C64 setup_proxy 不打印节点 URI", "echo \"$URI\"" not in sp and "echo $NODE_LINK" not in sp)
        check("C65 setup_proxy 失败时 exit 1", "exit 1" in sp)
        check("C66 setup_proxy 探针区分被标记与干净",
              "Access blocked" in sp and "/blocked" in sp)
        check("C67 setup_proxy 用 set -e", sp.count("\nset -e") == 1)
    check("C70 run_zampto.sh 存在", RUN_SH.exists())
    if RUN_SH.exists():
        rs = RUN_SH.read_text(encoding="utf-8")
        check("C71 run_zampto 起 Xvfb", "Xvfb" in rs)
        check("C72 run_zampto 清空 ALL_PROXY", "ALL_PROXY=" in rs)
        check("C73 run_zampto 透传退出码", 'exit "$PYTHON_EXIT"' in rs)
        check("C74 run_zampto 支持录屏开关", "ENABLE_RECORDING" in rs)

    # ---- 换行符（run #82 的教训）
    # setup_proxy.sh 是用 Python 从旧 YAML 的 run 块抽出来再 open("w") 写的，
    # Windows 上默认把 \n 翻成 \r\n；推上去之后 runner 的 bash 在
    # `set -e\r` 处炸成 "set: -: invalid option"。Cygwin 的 bash -n 对此
    # 相当宽容，D7 根本抓不到 —— 所以必须在字节层面钉死。
    for f in (SETUP_PROXY, RUN_SH):
        if not f.exists():
            continue
        raw = f.read_bytes()
        eq(f"C90 {f.name} 不含 CR（LF 结尾）", b"\r" in raw, False)
        check(f"C91 {f.name} 首行是 shebang", raw.startswith(b"#!"), raw[:20])
        # 第一行的换行必须是裸 \n，否则就是 CRLF 没转干净
        first_nl = raw.find(b"\n")
        check(f"C92 {f.name} 首行换行为 LF",
              first_nl > 0 and raw[first_nl - 1] != 0x0D, repr(raw[max(0, first_nl - 3):first_nl + 1]))

    # 顺带扫一遍仓库里其它会被 shell / runner 读到的文本文件
    _cr_offenders = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if any(x in p.parts for x in (".git", "screenshots", "__pycache__", "results")):
            continue
        if p.suffix not in (".sh", ".yml", ".yaml", ".bat", ".ps1"):
            continue
        if b"\r" in p.read_bytes():
            _cr_offenders.append(str(p.relative_to(ROOT)))
    eq("C93 仓库内无任何 CRLF 的脚本/配置", _cr_offenders, [])

    # ---- README / .gitignore / 死文件
    if README.exists():
        rd = README.read_text(encoding="utf-8")
        for name in ("ZAMPTO_SESSION_SECRET", "ZAMPTO_USERNAME", "ZAMPTO_PASSWORD",
                     "ZAMPTO_SERVER_ID", "PROXY_URI", "TUIC_URI", "DRY_RUN"):
            check(f"C80 README 记录 {name}", name in rd)
        check("C81 README 提到 renew-kit", "renew-kit" in rd)
        check("C82 README 有结果分类表", "续期未完成" in rd or "failed" in rd.lower())
    gi = ROOT / ".gitignore"
    if gi.exists():
        g = gi.read_text(encoding="utf-8")
        for pat in ("screenshots/", "*.mp4", "results/", ".env"):
            check(f"C83 .gitignore 含 {pat}", pat in g)
    check("C84 死 workflow api-key-test.yml 已删",
          not (ROOT / ".github" / "workflows" / "api-key-test.yml").exists())


# ═══════════════════════════════════════════════════════════ [D] 真子进程

def group_d() -> None:
    section("[D] 真子进程")
    bash = which_bash()
    check("D1  本机有可用的 bash（绝对路径）", bash is not None, str(bash))
    if bash is None:
        return

    # 探测：连 echo ok 都解析不了就说明这个 bash 是坏的（Windows 上很常见）
    probe = subprocess.run([bash, "-n"], input="echo ok\n", text=True,
                           capture_output=True, timeout=30)
    check("D1b 探测脚本能被这个 bash 解析", probe.returncode == 0, probe.stderr[:80])
    if probe.returncode != 0:
        return

    # D2 py_compile
    r = subprocess.run([sys.executable, "-m", "py_compile", str(APP)],
                       text=True, capture_output=True, timeout=120)
    check("D2  py_compile 通过", r.returncode == 0, r.stderr[-300:])

    # D3 真 import（需要 requests + renewkit）
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(WORKSPACE / "renew-kit"), str(WORKSPACE / "_deps")]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.update({"ZAMPTO_USERNAME": "u@e.com", "ZAMPTO_PASSWORD": "pw",
                "ZAMPTO_SERVER_ID": "15629"})
    r = subprocess.run(
        [sys.executable, "-c",
         "import zampto_auto as a; print(a.SERVICE, a.RENEW_THRESHOLD_HOURS)"],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=180)
    check("D3  真子进程能 import 模块", r.returncode == 0, r.stderr[-400:])
    check("D4  import 输出 SERVICE 与阈值",
          "Zampto 48" in r.stdout, r.stdout.strip()[:120])

    # D5 缺凭据时 main() 退 1（端到端，含 renewkit 渲染）
    r = subprocess.run(
        [sys.executable, "-c",
         "import os, zampto_auto as a; os.environ.pop('ZAMPTO_SERVER_ID', None);"
         "import importlib; importlib.reload(a);"
         "a.phase_browser_renewal = lambda cookies=None: 'failed';"
         "raise SystemExit(a.main())"],
        cwd=str(ROOT), env={**env, "ZAMPTO_SERVER_ID": ""}, text=True,
        capture_output=True, timeout=180)
    check("D5  缺凭据 -> 子进程退出码 1", r.returncode == 1, f"rc={r.returncode} {r.stderr[-200:]}")
    check("D6  子进程输出含报告", "续期未完成" in r.stdout or "缺少" in r.stdout,
          r.stdout[-200:])

    # D7 bash -n 两个脚本
    for f in (SETUP_PROXY, RUN_SH):
        r = subprocess.run([bash, "-n", str(f)], text=True, capture_output=True, timeout=60)
        check(f"D7  bash -n {f.name} 通过", r.returncode == 0, r.stderr[-200:])

    # D8 workflow 的每个 run 块渲染占位符后语法合法
    import re
    ph = re.compile(r"\$\{\{[^}]*\}\}")
    lines = WF_SRC.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        if re.match(r"^\s*run:\s*\|-?\s*$", lines[i]):
            indent = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            body = []
            while i < len(lines):
                ln = lines[i]
                if not ln.strip():
                    body.append("")
                    i += 1
                    continue
                if len(ln) - len(ln.lstrip()) <= indent:
                    break
                body.append(ln)
                i += 1
            blocks.append("\n".join(body))
            continue
        i += 1
    check("D8  workflow 里抠到 2 个 run 块", len(blocks) == 2, f"got={len(blocks)}")
    for n, blk in enumerate(blocks, 1):
        for value in ("", "xvfb-run python x.py"):
            rendered = ph.sub(value, blk)
            r = subprocess.run([bash, "-n"], input=rendered, text=True,
                               capture_output=True, timeout=60)
            check(f"D9  workflow run 块 #{n} 语法合法（占位符={value[:12]!r}）",
                  r.returncode == 0, r.stderr.strip()[:200])

    # D10 workflow 能被 YAML 解析，且 on: 解析成 True（YAML 1.1）
    try:
        import yaml
        d = yaml.safe_load(WF_SRC)
        check("D10 workflow YAML 可解析", isinstance(d, dict))
        on = d.get(True) or d.get("on")
        check("D11 workflow 有 schedule 与 workflow_dispatch",
              on and "schedule" in on and "workflow_dispatch" in on, str(list(on or {})))
        steps = d["jobs"]["zampto-auto"]["steps"]
        uses = [s.get("uses") for s in steps if s.get("uses")]
        check("D12 用到 renew-kit composite action",
              any("renew-kit" in (u or "") for u in uses), str(uses))
    except ImportError:
        print("  （本机没装 PyYAML，跳过 D10-D12）")


# ═══════════════════════════════════════════════════════════ main

def main() -> int:
    print(f"zampto 离线验证 | 仓库 {ROOT}")
    for g in (group_a, group_b, group_c, group_d):
        try:
            g()
        except BaseException:
            _fail.append((_group, "整组异常中断"))
            print(traceback.format_exc())

    print("\n" + "=" * 64)
    if _fail:
        print(f"\u274c 失败 {len(_fail)} / 共 {_pass + len(_fail)}")
        for grp, label in _fail:
            print(f"   · [{grp}] {label}")
        return 1
    print(f"\u2705 全部通过（{_pass} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
