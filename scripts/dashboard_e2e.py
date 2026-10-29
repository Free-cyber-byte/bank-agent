#!/usr/bin/env python3
"""ZeroStep 专区聚合接口（/api/dashboard、/api/analytics）端到端测试（v0.7.0 引入）。

覆盖「仪表盘 + 分析」只读聚合层：
- /api/dashboard 的结构与口径：window 解析（7d/30d/90d/180d/6m/1y，非法值回落 30d）、
  六个 KPI（净资产 / 可用 / 本月支出 / 本月收入 / 净流入 / 信用卡待还）、环比与 14 点 sparkline、
  近 6 个月趋势、6 条预算、支出构成、待办与风险（含审计链徽章）、最近流水 8 行；
- 数值自洽：净资产 = 活期 + 储蓄 + 持仓；可用 = 活期 + 储蓄；信用卡待还 = -信用卡余额；
  趋势各月净额 = 收入 − 支出；构成占比合计 100%；KPI 与 /api/accounts 口径一致；
- /api/analytics 的结构与口径：区间 / 粒度（day|week|month）、序列桶铺满（空月补零）、
  类别排名、商户 TOP10、异常、订阅倒计时、储蓄建议、明细 100 笔上限；
- 时间窗语义：不同 window 的 from/to 长度正确；自定义区间越界（start>end、跨度>400 天）→ 400；
  粒度非法 → 400；区间过滤生效（把 from/to 收窄到单日，桶数变 1、笔数变少）；
- 只读性：调用前后 /api/audit 的 chain.checked 不变、valid 仍为 True，且不新增 transactions。

隔离策略：把 backend/、skills/、frontend/、VERSION 复制到 .eval_tmp/dashboard_e2e/app/，
使用独立数据库与独立日志，不碰仓库里的 backend/data/bank.db（演示/并行开发可能在用）。

用法：
    python scripts/dashboard_e2e.py
    python scripts/dashboard_e2e.py -v      # 失败时打印主服务日志尾部
    python scripts/dashboard_e2e.py --json  # 追加打印机器可读结果

端口：主服务 8052（被占用时自动换空闲端口）。
退出码：0 全部通过；1 有失败；2 环境不可用。
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
WORK = os.path.join(ROOT, ".eval_tmp", "dashboard_e2e")
APP = os.path.join(WORK, "app")
LOGS = os.path.join(WORK, "logs")
LOG_MAIN = os.path.join(LOGS, "main_server.log")

USER = "u001"
RE_YUAN = re.compile(r"^¥\s?-?\d[\d,]*\.\d{2}$")

CHECKS = []
PROCS = []


# ---------------- 基础工具 ----------------
def _port_in_use(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _pick_port(preferred):
    return preferred if not _port_in_use(preferred) else _free_port()


def _wait_port_free(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.2)
    return not _port_in_use(port)


def _check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": detail})
    print("%s  %s  %-62s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


def _prepare_app():
    """复制运行所需代码到隔离目录，得到全新的数据库与日志。"""
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(LOGS, exist_ok=True)

    def ignore(_dir, names):
        return [n for n in names if n in ("__pycache__", "data", "messages.jsonl", "sessions.json")]

    for name in ("backend", "skills", "frontend"):
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(APP, name), ignore=ignore)
    version_src = os.path.join(ROOT, "VERSION")
    if os.path.exists(version_src):
        shutil.copy2(version_src, os.path.join(APP, "VERSION"))
    os.makedirs(os.path.join(APP, "backend", "data"), exist_ok=True)


def _base_env():
    env = dict(os.environ)
    env.update({
        "DEMO_MODE": "1",
        "ZEROSTEP_DISABLE_LLM": "1",
        "MCP_ENABLED": "0",
        "CHANNEL_ENABLED": "0",
    })
    return env


def _start_main(port):
    env = _base_env()
    env["PORT"] = str(port)
    log = open(LOG_MAIN, "wb")
    proc = subprocess.Popen([PY, os.path.join("backend", "server.py")], cwd=APP, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    PROCS.append((proc, log))
    return proc


def _stop_procs():
    while PROCS:
        proc, log = PROCS.pop()
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass


def _wait_http(url, timeout=25.0, expect=None):
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if expect is None or expect(body):
                return body
            last_error = "返回不符合预期：%s" % (body,)
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败属正常
            last_error = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(0.25)
    raise RuntimeError("等待 %s 超时：%s" % (url, last_error))


def _wait_main(port):
    return _wait_http("http://127.0.0.1:%d/api/health" % port,
                      expect=lambda b: b.get("status") == "ok")


def _post(port, path, payload, timeout=20.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _get(port, path, timeout=15.0):
    """GET JSON，返回 (HTTP 状态码, 响应 dict)；4xx/5xx 返回响应体而不抛异常。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _err_code(body):
    """错误响应形如 {"error": {"code": "BAD_RANGE", "message": ...}}。"""
    err = (body or {}).get("error")
    if isinstance(err, dict):
        return err.get("code")
    return (body or {}).get("code")


def _audit(port):
    data = _get(port, "/api/audit?limit=400")[1]
    return data.get("entries") or [], data.get("chain") or {}


def _tail(path, lines=25):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return "".join(fp.readlines()[-lines:])
    except OSError:
        return "(无日志)"


def _yuan(cents):
    return "¥%s" % format(int(cents) / 100.0, ",.2f")


def _cents(text):
    """把「¥1,234.56」还原成 123456 分。"""
    if not isinstance(text, str):
        return None
    digits = re.sub(r"[^\d.-]", "", text)
    try:
        return int(round(float(digits) * 100))
    except ValueError:
        return None


# ---------------- 断言辅助 ----------------
def _keys_ok(obj, keys):
    missing = [k for k in keys if k not in obj]
    return (not missing, "缺字段：" + ",".join(missing) if missing else "")


def _sum_cents(rows, key):
    return sum(int(r.get(key) or 0) for r in rows)


# ---------------- 主流程 ----------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 专区聚合接口 /api/dashboard、/api/analytics 端到端测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印主服务日志尾部")
    parser.add_argument("--json", action="store_true", help="追加打印机器可读结果")
    args = parser.parse_args(argv)

    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8-sig") as fp:
            version_text = fp.read().strip()
    except OSError:
        version_text = "?"

    port = _pick_port(8052)
    print("ZeroStep v%s 专区聚合接口 E2E（主服务:%d）" % (version_text, port))
    print("-" * 88)

    try:
        _prepare_app()
        if not os.path.exists(os.path.join(APP, "backend", "app", "analytics.py")):
            print("未找到 backend/app/analytics.py，专区聚合层尚未交付。")
            return 2

        _start_main(port)
        health = _wait_main(port)
        _check("dash-0", "健康检查 200 且 version 与 VERSION 一致", health.get("version") == version_text,
               "version=%s" % health.get("version"))

        st, sess = _post(port, "/api/session", {})
        sid = str(sess.get("session_id") or "")
        _check("dash-1", "创建会话返回 session_id", st == 200 and sid.startswith("s_"), "http=%s id=%s" % (st, sid))

        entries0, chain0 = _audit(port)
        txns_before = len(_get(port, "/api/transactions?limit=200")[1].get("transactions") or [])

        # ---------- /api/dashboard 结构 ----------
        st, dash = _get(port, "/api/dashboard?session_id=%s&window=30d" % sid)
        top_ok, top_detail = _keys_ok(dash, ("as_of", "window", "kpis", "trend", "budget", "composition", "todo", "recent"))
        _check("dash-2", "仪表盘顶层字段齐全（as_of/window/kpis/trend/budget/composition/todo/recent）",
               st == 200 and top_ok, "http=%s %s" % (st, top_detail))

        kpis = dash.get("kpis") or {}
        kpi_ok, kpi_detail = _keys_ok(kpis, (
            "net_worth_cents", "available_cents", "holdings_cents", "credit_due_cents",
            "month_out_cents", "month_in_cents", "net_flow_cents",
            "net_worth_display", "available_display", "credit_due_display",
            "deltas", "prev", "sparklines"))
        _check("dash-3", "KPI 六项 + 环比 + sparklines 字段齐全", kpi_ok, kpi_detail)

        sparks = kpis.get("sparklines") or {}
        _check("dash-4", "sparkline 为 14 点序列（out/in 各 14）",
               len(sparks.get("out_cents") or []) == 14 and len(sparks.get("in_cents") or []) == 14,
               "out=%d in=%d" % (len(sparks.get("out_cents") or []), len(sparks.get("in_cents") or [])))

        # 与 /api/accounts 口径一致（持仓没有独立接口，直接取 KPI 里的 holdings_cents）
        accs = _get(port, "/api/accounts?session_id=%s" % sid)[1].get("accounts") or []
        non_credit = [a for a in accs if a.get("kind") != "credit"]
        credit = [a for a in accs if a.get("kind") == "credit"]
        exp_available = _sum_cents(non_credit, "balance_cents")
        exp_holdings = int(kpis.get("holdings_cents") or 0)
        exp_credit_due = -_sum_cents(credit, "balance_cents")
        exp_net = exp_available + exp_holdings - exp_credit_due
        ok_net = int(kpis.get("net_worth_cents") or 0) == exp_net
        ok_avail = int(kpis.get("available_cents") or 0) == exp_available
        ok_credit = int(kpis.get("credit_due_cents") or 0) == exp_credit_due
        _check("dash-5", "净资产 = 活期+储蓄+持仓−信用卡待还；可用 = 活期+储蓄；信用卡待还 = -卡片余额",
               ok_net and ok_avail and ok_credit,
               "net=%s/%s avail=%s/%s credit=%s/%s" % (kpis.get("net_worth_cents"), exp_net,
                                                       kpis.get("available_cents"), exp_available,
                                                       kpis.get("credit_due_cents"), exp_credit_due))

        _check("dash-6", "净流入 = 本月收入 − 本月支出",
               int(kpis.get("net_flow_cents") or 0) == int(kpis.get("month_in_cents") or 0) - int(kpis.get("month_out_cents") or 0),
               "%s" % kpis.get("net_flow_cents"))

        _check("dash-7", "金额字段 display 形如 ¥1,234.56",
               all(RE_YUAN.match(str(kpis.get(k) or "")) for k in ("net_worth_display", "available_display", "credit_due_display")),
               "%s / %s" % (kpis.get("net_worth_display"), kpis.get("credit_due_display")))

        # 趋势与预算
        months = (dash.get("trend") or {}).get("months") or []
        trend_ok = len(months) == 6 and all(
            int(m.get("net_cents") or 0) == int(m.get("in_cents") or 0) - int(m.get("out_cents") or 0) for m in months)
        _check("dash-8", "近 6 个月趋势：6 个桶且每月净额 = 收入 − 支出", trend_ok,
               "months=%s" % ",".join(str(m.get("month")) for m in months))

        budgets = dash.get("budget") or []
        budget_ok = len(budgets) == 6 and all(b.get("state") in ("ok", "warn", "over") for b in budgets)
        over_ok = all(b.get("state") == "over" for b in budgets if int(b.get("pct") or 0) > 100)
        _check("dash-9", "预算 6 条且 state ∈ ok/warn/over，超 100% 一律 over", budget_ok and over_ok,
               "over=%s" % [b.get("category") for b in budgets if b.get("state") == "over"])

        comp = dash.get("composition") or {}
        by_cat = comp.get("by_category") or []
        pct_sum = sum(float(c.get("pct") or 0) for c in by_cat)
        # 仪表盘只取前 6 类构成，占比合计落在 (0, 100]，且总额不小于分项之和
        _check("dash-10", "支出构成：最多 6 类、占比合计 ≤ 100、总额 ≥ 分项之和",
               1 <= len(by_cat) <= 6 and 0 < pct_sum <= 101.5
               and int(comp.get("total_out_cents") or 0) >= _sum_cents(by_cat, "cents"),
               "cats=%d pct_sum=%.2f total=%s" % (len(by_cat), pct_sum, comp.get("total_out_cents")))

        todo = dash.get("todo") or {}
        chain = todo.get("chain") or {}
        _check("dash-11", "待办与风险：rows 非空、审计链徽章 valid=True 且 checked 为非负整数",
               bool(todo.get("rows")) and chain.get("valid") is True
               and isinstance(chain.get("checked"), int) and chain.get("checked") >= 0,
               "rows=%d chain=%s/%s" % (len(todo.get("rows") or []), chain.get("valid"), chain.get("checked")))

        recent = dash.get("recent") or []
        recent_sorted = [str(r.get("ts")) for r in recent] == sorted((str(r.get("ts")) for r in recent), reverse=True)
        _check("dash-12", "最近流水 8 行且按时间倒序", len(recent) == 8 and recent_sorted, "n=%d" % len(recent))

        # ---------- window 语义 ----------
        windows = {}
        for kind, days in (("7d", 7), ("30d", 30), ("90d", 90), ("180d", 180)):
            _, body = _get(port, "/api/dashboard?session_id=%s&window=%s" % (sid, kind))
            win = body.get("window") or {}
            span = (date.fromisoformat(str(win.get("to"))) - date.fromisoformat(str(win.get("from")))).days + 1
            windows[kind] = (win.get("kind"), win.get("days"), span)
        win_ok = all(windows[k] == (k, d, d) for k, d in (("7d", 7), ("30d", 30), ("90d", 90), ("180d", 180)))
        _check("dash-13", "window 解析：7d/30d/90d/180d 的 days 与 from/to 跨度一致", win_ok, str(windows))

        _, body = _get(port, "/api/dashboard?session_id=%s&window=999d" % sid)
        _check("dash-14", "非法 window 回落 30d（不报错）",
               (body.get("window") or {}).get("kind") == "30d", "%s" % (body.get("window") or {}).get("kind"))

        # ---------- /api/analytics 结构 ----------
        today = date.today()
        frm = (today - timedelta(days=179)).isoformat()
        to = today.isoformat()
        st, ana = _get(port, "/api/analytics?session_id=%s&from=%s&to=%s&granularity=month" % (sid, frm, to))
        ana_ok, ana_detail = _keys_ok(ana, ("as_of", "range", "totals", "series", "categories", "merchants",
                                            "transactions", "anomalies", "subscriptions", "savings"))
        _check("ana-1", "分析页顶层字段齐全", st == 200 and ana_ok, "http=%s %s" % (st, ana_detail))

        rng = ana.get("range") or {}
        totals = ana.get("totals") or {}
        series = ana.get("series") or []
        _check("ana-2", "区间/粒度回显：granularity=month 且桶数 = 覆盖月份数",
               rng.get("granularity") == "month" and rng.get("buckets") == len(series) and len(series) >= 5,
               "buckets=%s series=%d" % (rng.get("buckets"), len(series)))

        series_sum_ok = (_sum_cents(series, "out_cents") == int(totals.get("out_cents") or 0)
                         and _sum_cents(series, "in_cents") == int(totals.get("in_cents") or 0))
        _check("ana-3", "序列各桶合计 = 区间总额（支出与收入都对齐）", series_sum_ok,
               "series_out=%s total_out=%s" % (_sum_cents(series, "out_cents"), totals.get("out_cents")))

        cats = ana.get("categories") or []
        cat_sum_ok = _sum_cents(cats, "cents") == int(totals.get("out_cents") or 0)
        pct_ok = abs(sum(float(c.get("pct") or 0) for c in cats) - 100.0) <= 0.5
        _check("ana-4", "类别排名合计 = 支出总额且占比合计 100%（±0.5）", cat_sum_ok and pct_ok,
               "cats=%d sum=%s total=%s pct=%.2f" % (len(cats), _sum_cents(cats, "cents"), totals.get("out_cents"),
                                                     sum(float(c.get("pct") or 0) for c in cats)))

        merchants = ana.get("merchants") or []
        _check("ana-5", "商户 TOP10 最多 10 条且按金额倒序",
               len(merchants) <= 10 and all(int(merchants[i].get("cents") or 0) >= int(merchants[i + 1].get("cents") or 0)
                                            for i in range(len(merchants) - 1)), "n=%d" % len(merchants))

        txns = ana.get("transactions") or []
        tx_sorted = [str(t.get("ts")) for t in txns] == sorted((str(t.get("ts")) for t in txns), reverse=True)
        _check("ana-6", "明细最多 100 笔且按时间倒序（供分页与 CSV 导出）",
               len(txns) == min(100, int(totals.get("txn_count") or 0)) and tx_sorted, "n=%d" % len(txns))

        _check("ana-7", "日均支出 = 区间支出 ÷ 跨度天数",
               int(totals.get("avg_day_out_cents") or 0) == int(round(int(totals.get("out_cents") or 0)
                                                                      / float(int(rng.get("span_days") or 1)))),
               "avg=%s span=%s" % (totals.get("avg_day_out_cents"), rng.get("span_days")))

        _check("ana-8", "订阅倒计时：每条有 next_charge 与天数为整数",
               all(str(s.get("next_charge")) and isinstance(s.get("days_left"), int)
                   for s in (ana.get("subscriptions") or [])),
               "subs=%d" % len(ana.get("subscriptions") or []))

        savings = ana.get("savings") or {}
        _check("ana-9", "储蓄建议：可优化金额为非负整数且建议不超过 3 条",
               isinstance(savings.get("potential_cents"), int) and int(savings.get("potential_cents")) >= 0
               and len(savings.get("suggestions") or []) <= 3,
               "potential=%s sugg=%d" % (savings.get("potential_cents"), len(savings.get("suggestions") or [])))

        # ---------- 时间窗与粒度语义 ----------
        _, day = _get(port, "/api/analytics?session_id=%s&from=%s&to=%s&granularity=day" % (
            sid, (today - timedelta(days=29)).isoformat(), to))
        _, week = _get(port, "/api/analytics?session_id=%s&from=%s&to=%s&granularity=week" % (
            sid, (today - timedelta(days=29)).isoformat(), to))
        _check("ana-10", "同一区间：day 粒度 30 桶、week 粒度 5 桶",
               len(day.get("series") or []) == 30 and len(week.get("series") or []) == 5,
               "day=%d week=%d" % (len(day.get("series") or []), len(week.get("series") or [])))

        bucket_keys = [s.get("bucket") for s in (day.get("series") or [])]
        _check("ana-11", "day 粒度桶键形如 YYYY-MM-DD 且无重复",
               all(re.match(r"^\d{4}-\d{2}-\d{2}$", str(b)) for b in bucket_keys) and len(set(bucket_keys)) == len(bucket_keys),
               "first=%s last=%s" % (bucket_keys[:1], bucket_keys[-1:]))

        one_day = today.isoformat()
        _, narrow = _get(port, "/api/analytics?session_id=%s&from=%s&to=%s&granularity=day" % (sid, one_day, one_day))
        _check("ana-12", "单日区间：1 个桶，且总额不超过整月",
               len(narrow.get("series") or []) == 1
               and int((narrow.get("totals") or {}).get("out_cents") or 0) <= int(totals.get("out_cents") or 0),
               "buckets=%d out=%s" % (len(narrow.get("series") or []), (narrow.get("totals") or {}).get("out_cents")))

        st, body = _get(port, "/api/analytics?session_id=%s&from=%s&to=%s" % (sid, to, frm))
        _check("ana-13", "start > end → 400 BAD_RANGE", st == 400 and _err_code(body) == "BAD_RANGE",
               "http=%s code=%s" % (st, _err_code(body)))

        st, body = _get(port, "/api/analytics?session_id=%s&granularity=hour" % sid)
        _check("ana-14", "granularity=hour → 400 BAD_RANGE", st == 400 and _err_code(body) == "BAD_RANGE",
               "http=%s code=%s" % (st, _err_code(body)))

        st, body = _get(port, "/api/analytics?session_id=%s&from=2020-01-01&to=%s" % (sid, to))
        _check("ana-15", "跨度超过 400 天 → 400 BAD_RANGE", st == 400 and _err_code(body) == "BAD_RANGE",
               "http=%s code=%s" % (st, _err_code(body)))

        st, body = _get(port, "/api/dashboard?session_id=s_not_exist&window=30d")
        _check("ana-16", "未知会话 → 404", st == 404, "http=%s code=%s" % (st, _err_code(body)))

        # ---------- 只读性 ----------
        entries1, chain1 = _audit(port)
        txns_after = len(_get(port, "/api/transactions?limit=200")[1].get("transactions") or [])
        _check("ana-17", "聚合接口不写审计链：checked 不变、valid 仍为 True",
               chain1.get("checked") == chain0.get("checked") and chain1.get("valid") is True,
               "before=%s after=%s" % (chain0.get("checked"), chain1.get("checked")))
        _check("ana-18", "聚合接口不产生资金流水：transactions 条数不变", txns_before == txns_after,
               "before=%d after=%d" % (txns_before, txns_after))

        # ---------- v0.7.1：账户页只读接口 ----------
        st, snap = _get(port, "/api/accounts?session_id=%s" % sid)
        acc_rows = snap.get("accounts") or []
        _check("acc-1", "/api/accounts 返回 3 个账户", st == 200 and len(acc_rows) == 3,
               "http=%s n=%d" % (st, len(acc_rows)))
        card_fields = ("card_id", "card_kind", "card_status", "single_limit_cents", "daily_limit_cents")
        checking = [a for a in acc_rows if a.get("kind") == "checking"]
        credit_rows = [a for a in acc_rows if a.get("kind") == "credit"]
        _check("acc-2", "工资卡带卡片状态与限额字段",
               bool(checking) and all(k in checking[0] for k in card_fields) and checking[0].get("card_status") == "active",
               "keys=%s" % (sorted(set(checking[0].keys()) & set(card_fields)) if checking else []))
        _check("acc-3", "信用卡额度 3000000 分且与账户余额一致",
               bool(credit_rows) and int(credit_rows[0].get("credit_limit_cents") or 0) == 3000000
               and int(credit_rows[0].get("balance_cents") or 0) == -843200,
               "limit=%s balance=%s" % (credit_rows[0].get("credit_limit_cents") if credit_rows else None,
                                        credit_rows[0].get("balance_cents") if credit_rows else None))
        credit_id = credit_rows[0].get("account_id") if credit_rows else ""

        st, only = _get(port, "/api/transactions?session_id=%s&account_id=%s&limit=50" % (sid, credit_id))
        rows_only = only.get("transactions") or []
        _check("acc-4", "按账户过滤流水：只返回该账户且回显 account_id",
               st == 200 and only.get("account_id") == credit_id and rows_only
               and all(r.get("account_id") == credit_id for r in rows_only),
               "http=%s n=%d echo=%s" % (st, len(rows_only), only.get("account_id")))
        all_rows = _get(port, "/api/transactions?session_id=%s&limit=200" % sid)[1].get("transactions") or []
        _check("acc-5", "过滤后条数不超过 limit 且少于全部",
               len(rows_only) <= 50 and 0 < len(rows_only) < len(all_rows),
               "filtered=%d all=%d" % (len(rows_only), len(all_rows)))

        st, body = _get(port, "/api/transactions?session_id=%s&account_id=acc_999" % sid)
        _check("acc-6", "不存在的账户 → 400 BAD_ACCOUNT", st == 400 and _err_code(body) == "BAD_ACCOUNT",
               "http=%s code=%s" % (st, _err_code(body)))

        fails = [c for c in CHECKS if not c["ok"]]
        print("-" * 88)
        print("结果：%d/%d 通过" % (len(CHECKS) - len(fails), len(CHECKS)))
        if fails and args.verbose:
            print("--- 主服务日志尾部 ---")
            print(_tail(LOG_MAIN))
        if args.json:
            print(json.dumps({"ok": not fails, "passed": len(CHECKS) - len(fails), "total": len(CHECKS),
                              "checks": CHECKS}, ensure_ascii=False))
        return 0 if not fails else 1
    except RuntimeError as exc:
        print("环境不可用：%s" % exc)
        if args.verbose:
            print(_tail(LOG_MAIN))
        return 2
    finally:
        _stop_procs()
        _wait_port_free(port)


if __name__ == "__main__":
    sys.exit(main())
