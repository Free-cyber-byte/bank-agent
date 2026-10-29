#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ZeroStep v0.4.0 新能力 e2e（真实 HTTP，零依赖）。

覆盖 Lead 下发的 6 组新能力 + 环境自检：
  1. 理财赎回：赎回 500 元稳利宝 → 审批(medium) → approve → 持仓减少 / checking 增加 / 审计 tool_result；
     赎回 999999 元 → INSUFFICIENT_HOLDING。
  2. 卡额度：把信用卡日限额调到 500 元 → 审批 → approve → list_cards daily_limit_cents=50000；
     把日限额调到 -1 → 400/LIMIT_INVALID。
  3. 卡消费：刷信用卡 300 元买咖啡成功；再刷 300 → 日限额拦截；刷 5000 元 → 单笔限额拦截；
     冻结卡消费 → CARD_FROZEN（随后解冻）。
  4. 异常扫描：最近有没有异常交易 → reply 含规则名与金额；年度账单：今年的年度账单 → reply 含年份与总支出。
  5. 手机号转账：13800001111 → 命中通讯录妈妈（hits 含 payee_known）；13999998888 → 高风险 TOTP（不是 blocked）。
  6. 预约转账：3 天后给妈妈转 800 元 → 审批 → approve → GET /api/scheduled pending →
     POST /api/scheduler/tick（带未来时间）→ executed / 余额变化 / 审计 scheduled_transfer_executed；
     再建一笔 → 取消最近一笔预约 → cancelled。

设计要点：
  * 全流程走真实 HTTP（urllib，仅标准库），不 import 产品代码做断言；持仓 / card_txns /
    scheduled_transfers 等未暴露 API 的内部状态用只读 sqlite 直读隔离副本 DB 交叉验证。
  * 独立副本 .eval_tmp/capability_e2e/app（复制 backend + skills + VERSION，忽略 data/__pycache__），
    独立端口（默认 8051，被占用自动顺延并清理本段残留），绝不碰 Lead 的 8000。
  * 环境变量：PORT=8051 HOST=127.0.0.1 DEMO_MODE=1 ZEROSTEP_DISABLE_LLM=1 MCP_ENABLED=0
    CHANNEL_ENABLED=0 SCHEDULER_ENABLED=0（避免后台调度线程在 tick 之前自行执行预约）。
  * 退出码：0 全过 / 1 有失败 / 2 环境错误（起不来服务等）。
  * --smoke 只跑环境自检（可先在未就绪的产品代码上验证脚本机制）。

用法：
  python scripts/capability_e2e.py                # 全量
  python scripts/capability_e2e.py --smoke        # 仅环境自检
  python scripts/capability_e2e.py --only cap-red-1,cap-red-2
  python scripts/capability_e2e.py --json         # 机器可读输出（最后一行 JSON）
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent          # bank-agent/
APP_ROOT = ROOT / ".eval_tmp" / "capability_e2e"
APP_DIR = APP_ROOT / "app"
LOG_DIR = APP_ROOT / "logs"
DB_PATH = APP_DIR / "backend" / "data" / "bank.db"
VERSION_FILE = ROOT / "VERSION"
HOST = "127.0.0.1"
PREFERRED_PORT = 8051
DEFAULT_USER = "u001"
START_TIMEOUT = 40.0

# 覆盖点检查项 id 前缀（--only 过滤用）
ALL_PREFIXES = ("cap-smoke-", "cap-env-", "cap-red-", "cap-lim-", "cap-spd-",
                "cap-rad-", "cap-phone-", "cap-sch-", "cap-final-", "cap-run-")


# ---------------------------------------------------------------- 基础工具

def _now():
    return datetime.now().astimezone()


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def _future_iso(days=4):
    return _iso(_now() + timedelta(days=days))


def _yuan(cents):
    try:
        return "%.2f" % (float(cents) / 100.0)
    except (TypeError, ValueError):
        return "0.00"


def _short(obj, limit=260):
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(obj)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _dig(obj, *keys, default=None):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


# ---------------------------------------------------------------- 端口处理

def _port_listening(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":%d" % port):
                return True
    return False


def _port_pids(port):
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return pids
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING" \
                and parts[1].endswith(":%d" % port):
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                continue
    return pids


def _can_bind(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _pick_port(preferred=PREFERRED_PORT):
    """优先 preferred，被占用时先清理该端口残留监听进程，再顺延；不用 SO_REUSEADDR 探测。"""
    candidates = [preferred] + [p for p in range(preferred + 1, preferred + 11)]
    for port in candidates:
        if _port_listening(port):
            pids = _port_pids(port)
            for pid in pids:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass
            deadline = time.time() + 5.0
            while time.time() < deadline and _port_listening(port):
                time.sleep(0.2)
        if not _port_listening(port) and _can_bind(port):
            return port
    raise RuntimeError("端口段 %d-%d 均不可用，请检查残留进程" % (preferred, preferred + 10))


# ---------------------------------------------------------------- HTTP

def _http(method, url, payload=None, timeout=25):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _decode(raw), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, ValueError):
            raw = b""
        return exc.code, _decode(raw), dict(exc.headers or {})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"error": {"code": "NETWORK", "message": str(exc)}}, {}


def _decode(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _get(port, path, timeout=20):
    return _http("GET", "http://%s:%d%s" % (HOST, port, path), timeout=timeout)


def _post(port, path, payload=None, timeout=40):
    return _http("POST", "http://%s:%d%s" % (HOST, port, path), payload if payload is not None else {}, timeout=timeout)


# ---------------------------------------------------------------- 响应解读

def _reply(payload):
    return str(_dig(payload, "reply") or "")


def _status(payload):
    return str(_dig(payload, "status") or "")


def _tools(payload):
    out = []
    for item in _dig(payload, "executed", default=[]) or []:
        if isinstance(item, dict) and item.get("ok") and item.get("tool"):
            out.append(str(item["tool"]))
    return out


def _plan_tools(payload):
    out = []
    for item in _dig(payload, "plan", "steps", default=[]) or []:
        if isinstance(item, dict) and item.get("tool"):
            out.append(str(item["tool"]))
    return out


def _code_from(status, payload):
    for key in ("blocked", "error"):
        val = _dig(payload, key)
        if isinstance(val, dict) and val.get("code"):
            return str(val["code"])
    if isinstance(payload, dict) and payload.get("code"):
        return str(payload["code"])
    return ""


def _is_error(status, payload):
    if status and status >= 400:
        return True
    return _status(payload) in ("blocked", "error") or bool(_code_from(status, payload))


def _hits(approval):
    out = []
    for item in (approval or {}).get("hits") or []:
        if isinstance(item, dict):
            rule = item.get("rule") or item.get("code") or item.get("name")
            if rule:
                out.append(str(rule))
        elif isinstance(item, str):
            out.append(item)
    return out


def _approval(payload):
    val = _dig(payload, "approval")
    return val if isinstance(val, dict) else {}


def _challenge(payload):
    return str(_dig(payload, "approval", "challenge", "type") or "")


def _risk(payload):
    return str(_dig(payload, "approval", "risk_level") or "")


# ---------------------------------------------------------------- 业务辅助

def new_session(port):
    status, payload, _ = _post(port, "/api/session", {})
    if status != 200 or not isinstance(payload, dict) or not payload.get("session_id"):
        return ""
    return str(payload["session_id"])


def chat(port, session_id, text):
    return _post(port, "/api/chat", {"session_id": session_id, "text": text})


def approve(port, session_id, result, decision="approve", user_id=DEFAULT_USER):
    """按审批 challenge 自动补齐 TOTP 后提交；返回 (http_status, payload, challenge_type)。"""
    ap = _approval(result) if isinstance(result, dict) else {}
    approval_id = str(ap.get("approval_id") or "")
    challenge = str(_dig(ap, "challenge", "type") or "")
    code = ""
    if decision == "approve" and challenge == "totp":
        status, totp, _ = _get(port, "/api/demo/totp?user_id=" + urllib.parse.quote(user_id))
        if status == 200 and isinstance(totp, dict):
            code = str(totp.get("code") or "")
    status, payload, _ = _post(port, "/api/approval",
                            {"session_id": session_id, "approval_id": approval_id,
                             "decision": decision, "code": code})
    return status, payload, challenge


def chat_approve(port, session_id, text, decision="approve"):
    """发消息；若进入审批则自动审批（approve 时按 challenge 补 TOTP），返回 (http, final, challenge, first)。"""
    status, first, _ = chat(port, session_id, text)
    if _status(first) == "awaiting_approval":
        status2, final, challenge = approve(port, session_id, first, decision)
        return status2, final, challenge, first
    return status, first, "", first


def accounts(port, session_id):
    status, payload, _ = _get(port, "/api/accounts?session_id=" + urllib.parse.quote(session_id))
    return payload if status == 200 and isinstance(payload, dict) else {}


def checking_total(port, session_id):
    cards = accounts(port, session_id).get("accounts") or []
    try:
        return sum(int(a.get("balance_cents") or 0) for a in cards if a.get("kind") == "checking")
    except (TypeError, ValueError):
        return 0


def credit_card(port, session_id):
    for card in accounts(port, session_id).get("cards") or []:
        if isinstance(card, dict) and card.get("kind") == "credit":
            return card
    return {}


def audit(port, session_id="", limit=400):
    path = "/api/audit?limit=%d" % limit
    if session_id:
        path += "&session_id=" + urllib.parse.quote(session_id)
    status, payload, _ = _get(port, path)
    return payload if status == 200 and isinstance(payload, dict) else {}


def entries(payload):
    val = _dig(payload, "entries", default=[]) or []
    return [e for e in val if isinstance(e, dict)]


def _db_rows(sql, params=()):
    """只读直读隔离副本 DB（用于验证无 HTTP 暴露的内部状态）。"""
    if not DB_PATH.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def sched_items(payload):
    """GET /api/scheduled 返回 {ok, scheduled:[...], total}；兼容旧版 items 字段。"""
    val = _dig(payload, "scheduled", default=None)
    if val is None:
        val = _dig(payload, "items", default=[]) or []
    return [it for it in (val or []) if isinstance(it, dict)]


def _mark_schedule_due(sched_id, when):
    """测试夹具：把预约的 run_at 改到过去，便于手动 tick 确定性地执行到期预约。

    服务端 POST /api/scheduler/tick 当前忽略请求体（不接受自定义时间），
    因此无法用 HTTP 参数模拟「时间前进」；这里只改隔离副本 DB，不触碰产品代码。
    """
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute("UPDATE scheduled_transfers SET run_at=? WHERE sched_id=?", (when, sched_id))
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def db_tables():
    return {row["name"] for row in _db_rows("SELECT name FROM sqlite_master WHERE type='table'") if row.get("name")}


def holding_total():
    rows = _db_rows("SELECT amount_cents FROM wealth_holdings WHERE user_id=? AND status='holding'", (DEFAULT_USER,))
    try:
        return sum(int(r.get("amount_cents") or 0) for r in rows)
    except (TypeError, ValueError):
        return 0


def card_txn_count(card_id):
    rows = _db_rows("SELECT COUNT(*) AS n FROM card_txns WHERE card_id=?", (card_id,))
    return int(rows[0]["n"]) if rows else 0


def current_year():
    return _now().year


def _contains_any(text, words):
    return any(w in text for w in words)


# ---------------------------------------------------------------- 检查项

class Checks(object):
    def __init__(self, quiet=False):
        self.items = []
        self.quiet = quiet

    def add(self, cid, desc, ok, detail=""):
        detail = _short(detail, 700)
        item = {"id": cid, "desc": desc, "ok": bool(ok), "detail": detail}
        self.items.append(item)
        if not self.quiet:
            print("[%s] %-14s %s%s" % ("PASS" if ok else "FAIL", cid, desc,
                                       ("  | " + detail) if detail else ""))
        return bool(ok)

    def result(self):
        passed = sum(1 for it in self.items if it["ok"])
        failed = [it for it in self.items if not it["ok"]]
        return passed, len(self.items), failed


# ---------------------------------------------------------------- 服务进程

def _clean_env():
    env = {}
    drop_prefix = ("DEEPSEEK_", "LLM_", "MCP_", "CHANNEL_", "WECOM_", "PUSH_",
                   "SCHEDULER_", "ZEROSTEP_", "PLUGINS_", "TOTP_", "SEED_")
    drop_exact = {"PORT", "HOST", "DEMO_MODE"}
    for key, val in os.environ.items():
        if key in drop_exact or key.upper().startswith(drop_prefix):
            continue
        env[key] = val
    return env


def _app_env(port):
    env = _clean_env()
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PORT": str(port),
        "HOST": HOST,
        "DEMO_MODE": "1",
        "ZEROSTEP_DISABLE_LLM": "1",
        "DEEPSEEK_API_KEY": "",
        "MCP_ENABLED": "0",
        "CHANNEL_ENABLED": "0",
        "SCHEDULER_ENABLED": "0",
        "PLUGINS_FILE": str(APP_ROOT / "plugins.json"),
    })
    return env


def _prepare_app():
    """准备隔离副本。

    正常情况下清空 .eval_tmp/capability_e2e/app 后重建；若旧目录里的 sqlite 文件被
    残留进程 / 扫描器占用（Windows 下偶发共享冲突，ignore_errors 的 rmtree 会留下
    app/backend/data/bank.db），则退让到带时间戳的新 app 目录，避免整轮 e2e 被
    陈旧文件拖死。DB_PATH / APP_DIR 为模块级全局，后续场景统一读全局。
    """
    global APP_DIR, LOG_DIR, DB_PATH
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    app_dir = APP_ROOT / "app"
    if app_dir.exists():
        try:
            shutil.rmtree(str(app_dir))
        except OSError:
            # 旧副本被占用：清理其余老副本后改用独立目录
            for stale in APP_ROOT.glob("app*"):
                if stale == app_dir:
                    continue
                shutil.rmtree(str(stale), ignore_errors=True)
            app_dir = APP_ROOT / ("app_%s_%d" % (time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
    APP_DIR = app_dir
    DB_PATH = APP_DIR / "backend" / "data" / "bank.db"
    APP_DIR.mkdir(parents=True, exist_ok=True)

    def _ignore(directory, names):
        ignored = set()
        for name in names:
            if name in ("__pycache__", "data", "sessions.json", "messages.jsonl") or name.endswith(".pyc"):
                ignored.add(name)
        return ignored

    shutil.copytree(str(ROOT / "backend"), str(APP_DIR / "backend"), ignore=_ignore)
    if (ROOT / "skills").exists():
        shutil.copytree(str(ROOT / "skills"), str(APP_DIR / "skills"), ignore=_ignore)
    if VERSION_FILE.exists():
        shutil.copy2(str(VERSION_FILE), str(APP_DIR / "VERSION"))
    (APP_DIR / "backend" / "data").mkdir(parents=True, exist_ok=True)


class AppServer(object):
    def __init__(self, port):
        self.port = port
        self.proc = None
        self._fhs = []
        self.log_paths = []

    def start(self):
        out_path = LOG_DIR / "app_stdout.log"
        err_path = LOG_DIR / "app_stderr.log"
        self.log_paths = [out_path, err_path]
        out = open(str(out_path), "wb")
        err = open(str(err_path), "wb")
        self._fhs = [out, err]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [sys.executable or "python", str(APP_DIR / "backend" / "server.py")],
            cwd=str(APP_DIR), env=_app_env(self.port),
            stdout=out, stderr=err, stdin=subprocess.DEVNULL, creationflags=flags,
        )
        return self.wait_ready()

    def wait_ready(self, timeout=START_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            status, payload, _ = _get(self.port, "/api/health", timeout=2)
            if status == 200 and isinstance(payload, dict):
                return True
            time.sleep(0.3)
        return False

    def logs_tail(self, lines=40):
        out = []
        for path in self.log_paths:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
            if tail:
                out.append("--- %s ---\n%s" % (Path(path).name, "\n".join(tail)))
        return "\n".join(out)

    def no_traceback(self):
        for path in self.log_paths:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "Traceback (most recent call last)" in text:
                return False, str(path)
        return True, ""

    def stop(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                    proc.wait(timeout=6)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        self.proc = None
        for fh in self._fhs:
            try:
                fh.close()
            except OSError:
                pass
        self._fhs = []


# ---------------------------------------------------------------- 场景：环境自检

def scenario_env(app, c):
    port = app.port
    status, health, _ = _get(port, "/api/health")
    c.add("cap-env-1", "GET /api/health 返回 200 且 status=ok",
          status == 200 and _status(health) == "ok",
          "http=%s body=%s" % (status, _short(health)))
    try:
        file_version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_version = ""
    version = str(_dig(health, "version") or "")
    c.add("cap-env-2", "health.version 与仓库 VERSION 文件一致（不硬编码版本号，docs 升级前后都绿）",
          bool(version) and bool(file_version) and version == file_version,
          "health.version=%r file=%r" % (version, file_version))
    seed_version = str(_dig(health, "seed_version") or "")
    c.add("cap-env-2b", "health.seed_version 与 v0.8.0 起冻结的种子版本一致（2026.11.01）",
          seed_version == "2026.11.01", "seed_version=%r" % seed_version)

    status, reset, _ = _post(port, "/api/admin/reset", {})
    c.add("cap-env-3", "POST /api/admin/reset 成功（200 + reset=True）",
          status == 200 and isinstance(reset, dict) and bool(reset.get("reset")),
          "http=%s body=%s" % (status, _short(reset)))

    tables = db_tables()
    need = ("card_txns", "scheduled_transfers")
    missing = [t for t in need if t not in tables]
    c.add("cap-env-4", "v0.4.0 数据层表存在（card_txns / scheduled_transfers）",
          not missing, "missing=%s tables=%s" % (missing, sorted(tables)[:24]))

    payees = _db_rows("SELECT name, phone FROM payees WHERE user_id=?", (DEFAULT_USER,))
    phone_map = {}
    for row in payees:
        phone_map[str(row.get("name") or "")] = str(row.get("phone") or "")
    ok_phone = phone_map.get("妈妈") == "13800001111" and phone_map.get("房东王姐") == "13900002222"
    c.add("cap-env-5", "种子手机号：妈妈=13800001111 / 房东王姐=13900002222",
          ok_phone, "phones=%s" % _short(phone_map))

    # 重置后新表应为空（说明 RESET_TABLES 覆盖新表）
    sched_rows = _db_rows("SELECT COUNT(*) AS n FROM scheduled_transfers")
    txn_rows = _db_rows("SELECT COUNT(*) AS n FROM card_txns")
    empty_ok = (sched_rows and int(sched_rows[0].get("n") or 0) == 0) and \
               (txn_rows and int(txn_rows[0].get("n") or 0) == 0)
    c.add("cap-env-6", "admin/reset 清空新表（scheduled_transfers / card_txns 为 0）",
          empty_ok, "scheduled=%s card_txns=%s" % (_short(sched_rows), _short(txn_rows)))


# ---------------------------------------------------------------- 场景：赎回

def scenario_redeem(app, c):
    port = app.port
    session_id = new_session(port)
    if not session_id:
        c.add("cap-red-0", "创建会话", False, "POST /api/session 失败")
        return
    before_hold = holding_total()
    before_check = checking_total(port, session_id)

    status, result, _ = chat(port, session_id, "赎回 500 元稳利宝")
    c.add("cap-red-1", "赎回 500 元稳利宝进入审批（status=awaiting_approval）",
          status == 200 and _status(result) == "awaiting_approval",
          "http=%s status=%s reply=%s" % (status, _status(result), _short(_reply(result))))
    c.add("cap-red-2", "赎回审批风险等级为 medium", _status(result) == "awaiting_approval" and _risk(result) == "medium",
          "risk=%s approval=%s" % (_risk(result), _short(_approval(result))))
    c.add("cap-red-3", "赎回计划含 redeem_wealth 步骤", "redeem_wealth" in _plan_tools(result),
          "plan_tools=%s" % _plan_tools(result))

    if _status(result) == "awaiting_approval":
        status2, final, challenge = approve(port, session_id, result)
    else:
        status2, final, challenge = status, result, ""
    c.add("cap-red-4", "审批通过后赎回执行成功（status=ok）",
          status2 == 200 and _status(final) == "ok",
          "http=%s status=%s challenge=%s reply=%s" % (status2, _status(final), challenge, _short(_reply(final))))
    executed = _tools(final)
    c.add("cap-red-5", "执行结果含 redeem_wealth 工具", "redeem_wealth" in executed,
          "executed=%s" % executed)
    reply = _reply(final)
    c.add("cap-red-6", "回复含赎回金额 500 与赎回文案", ("500" in reply) and ("赎" in reply),
          "reply=%s" % _short(reply))

    after_hold = holding_total()
    c.add("cap-red-7", "持仓金额减少 50,000 分（500 元）",
          after_hold == before_hold - 50000,
          "before=%d after=%d delta=%d" % (before_hold, after_hold, after_hold - before_hold))
    after_check = checking_total(port, session_id)
    c.add("cap-red-8", "checking 账户合计增加 50,000 分（500 元）",
          after_check == before_check + 50000,
          "before=%d after=%d delta=%d" % (before_check, after_check, after_check - before_check))

    audit_payload = audit(port, session_id)
    tool_rows = [e for e in entries(audit_payload)
                 if str(e.get("event_type") or "") == "tool_result" and str(e.get("tool") or "") == "redeem_wealth"]
    c.add("cap-red-9", "审计链出现 tool_result / tool=redeem_wealth", len(tool_rows) >= 1,
          "hits=%d entries=%d" % (len(tool_rows), len(entries(audit_payload))))

    status3, over_first, _ = chat(port, session_id, "赎回 999999 元稳利宝")
    # 超赎回可能先进入审批（policy 对 redeem 不设单笔上限），approve 后由 mock_core 抛 INSUFFICIENT_HOLDING
    if _status(over_first) == "awaiting_approval":
        status3, over, _ = approve(port, session_id, over_first)
    else:
        over = over_first
    code = _code_from(status3, over)
    c.add("cap-red-10", "超额赎回 999999 元 → INSUFFICIENT_HOLDING",
          _is_error(status3, over) and code == "INSUFFICIENT_HOLDING",
          "http=%s status=%s code=%s reply=%s" % (status3, _status(over), code, _short(_reply(over))))
    hold_after_over = holding_total()
    c.add("cap-red-11", "超额赎回未改动持仓",
          hold_after_over == after_hold,
          "after_success=%d after_over=%d delta=%d" % (after_hold, hold_after_over, hold_after_over - after_hold))


# ---------------------------------------------------------------- 场景：卡额度

def scenario_card_limit(app, c):
    port = app.port
    session_id = new_session(port)
    if not session_id:
        c.add("cap-lim-0", "创建会话", False, "POST /api/session 失败")
        return
    status, result, _ = chat(port, session_id, "把信用卡日限额调到 500 元")
    c.add("cap-lim-1", "调整信用卡日限额进入审批（status=awaiting_approval）",
          status == 200 and _status(result) == "awaiting_approval",
          "http=%s status=%s reply=%s" % (status, _status(result), _short(_reply(result))))
    c.add("cap-lim-2", "调额审批风险等级为 medium", _status(result) == "awaiting_approval" and _risk(result) == "medium",
          "risk=%s" % _risk(result))
    c.add("cap-lim-3", "调额计划含 set_card_limit 步骤", "set_card_limit" in _plan_tools(result),
          "plan_tools=%s" % _plan_tools(result))

    if _status(result) == "awaiting_approval":
        status2, final, _ = approve(port, session_id, result)
    else:
        status2, final = status, result
    c.add("cap-lim-4", "审批通过后调额执行成功（status=ok + set_card_limit）",
          status2 == 200 and _status(final) == "ok" and "set_card_limit" in _tools(final),
          "http=%s status=%s executed=%s reply=%s" % (status2, _status(final), _tools(final), _short(_reply(final))))

    card = credit_card(port, session_id)
    c.add("cap-lim-5", "信用卡 daily_limit_cents 变为 50000（500 元）",
          int(card.get("daily_limit_cents") or -1) == 50000,
          "card=%s" % _short(card))

    status3, invalid, _ = chat(port, session_id, "把信用卡日限额调到 -1")
    code = _code_from(status3, invalid)
    reply_invalid = _reply(invalid)
    c.add("cap-lim-6", "非法日限额「把信用卡日限额调到 -1」→ LIMIT_INVALID（负号不得被吃成 1 元）",
          code == "LIMIT_INVALID" and "限额必须大于 0" in reply_invalid and "-1.00" in reply_invalid,
          "http=%s status=%s code=%s plan=%s reply=%s"
          % (status3, _status(invalid), code, _plan_tools(invalid), _short(reply_invalid)))
    card_after = credit_card(port, session_id)
    c.add("cap-lim-7", "非法调额后限额保持不变（仍为 50000）",
          int(card_after.get("daily_limit_cents") or -1) == 50000,
          "daily_limit_cents=%s" % card_after.get("daily_limit_cents"))


# ---------------------------------------------------------------- 场景：卡消费

def scenario_card_spend(app, c):
    port = app.port
    session_id = new_session(port)
    if not session_id:
        c.add("cap-spd-0", "创建会话", False, "POST /api/session 失败")
        return
    card = credit_card(port, session_id)
    card_id = str(card.get("card_id") or "card_002")
    if int(card.get("daily_limit_cents") or 0) != 50000:
        # 依赖 cap-lim 场景把日限额调到 500 元；这里兜底再调一次，保证消费断言可判定
        st, res, _ = chat(port, session_id, "把信用卡日限额调到 500 元")
        if _status(res) == "awaiting_approval":
            approve(port, session_id, res)
        card = credit_card(port, session_id)

    before_rows = card_txn_count(card_id)
    status, spend, _ch, _first = chat_approve(port, session_id, "刷信用卡 300 元买咖啡")
    c.add("cap-spd-1", "刷信用卡 300 元成功（status=ok + card_spend）",
          status == 200 and _status(spend) == "ok" and "card_spend" in _tools(spend),
          "http=%s status=%s executed=%s reply=%s" % (status, _status(spend), _tools(spend), _short(_reply(spend))))
    c.add("cap-spd-2", "消费回复含金额 300 与消费/咖啡文案",
          ("300" in _reply(spend)) and _contains_any(_reply(spend), ("消费", "咖啡", "刷")),
          "reply=%s" % _short(_reply(spend)))
    rows = _db_rows("SELECT amount_cents, merchant, status FROM card_txns WHERE card_id=? ORDER BY ts DESC LIMIT 1",
                    (card_id,))
    c.add("cap-spd-3", "card_txns 新增 30000 分 / 咖啡 的成功流水",
          bool(rows) and int(rows[0].get("amount_cents") or 0) == 30000,
          "rows=%s count_before=%d" % (_short(rows), before_rows))

    status2, second, _ch2, _first2 = chat_approve(port, session_id, "刷信用卡 300 元买咖啡")
    code2 = _code_from(status2, second)
    daily_codes = ("CARD_DAILY_LIMIT", "LIMIT_EXCEEDED", "DAILY_LIMIT_EXCEEDED")
    c.add("cap-spd-4", "同日再刷 300 元 → 日限额拦截（CARD_DAILY_LIMIT 优先）",
          _is_error(status2, second) and code2 in daily_codes,
          "http=%s status=%s code=%s reply=%s" % (status2, _status(second), code2, _short(_reply(second))))
    c.add("cap-spd-5", "日限额拦截未新增 card_txns 流水",
          card_txn_count(card_id) == before_rows + 1,
          "count=%d expected=%d" % (card_txn_count(card_id), before_rows + 1))

    single_limit = int(card.get("single_limit_cents") or 0)
    spend_amount = 5000  # 元，Lead 口径
    if single_limit > 0 and spend_amount * 100 <= single_limit:
        spend_amount = single_limit // 100 + 100
    status3, single, _ch3, _first3 = chat_approve(port, session_id, "刷信用卡 %d 元买手表" % spend_amount)
    code3 = _code_from(status3, single)
    c.add("cap-spd-6", "刷信用卡 %d 元 → CARD_SINGLE_LIMIT" % spend_amount,
          _is_error(status3, single) and code3 == "CARD_SINGLE_LIMIT",
          "http=%s status=%s code=%s single_limit_cents=%s reply=%s"
          % (status3, _status(single), code3, single_limit, _short(_reply(single))))
    c.add("cap-spd-7", "单笔限额拦截未新增 card_txns 流水",
          card_txn_count(card_id) == before_rows + 1,
          "count=%d expected=%d" % (card_txn_count(card_id), before_rows + 1))

    st_freeze, freeze, _ = chat(port, session_id, "冻结信用卡")
    if _status(freeze) == "awaiting_approval":
        st_freeze, freeze, _ = approve(port, session_id, freeze)
    c.add("cap-spd-8", "冻结信用卡成功（set_card_status）",
          _status(freeze) == "ok" and "set_card_status" in _tools(freeze),
          "http=%s status=%s executed=%s reply=%s" % (st_freeze, _status(freeze), _tools(freeze), _short(_reply(freeze))))

    status4, frozen, _ch4, _first4 = chat_approve(port, session_id, "刷信用卡 10 元买水")
    code4 = _code_from(status4, frozen)
    c.add("cap-spd-9", "冻结卡消费 → CARD_FROZEN",
          _is_error(status4, frozen) and code4 == "CARD_FROZEN",
          "http=%s status=%s code=%s reply=%s" % (status4, _status(frozen), code4, _short(_reply(frozen))))

    st_unfreeze, unfreeze, _ = chat(port, session_id, "解冻信用卡")
    if _status(unfreeze) == "awaiting_approval":
        st_unfreeze, unfreeze, _ = approve(port, session_id, unfreeze)
    c.add("cap-spd-10", "解冻信用卡成功（set_card_status）",
          _status(unfreeze) == "ok" and "set_card_status" in _tools(unfreeze),
          "http=%s status=%s executed=%s" % (st_unfreeze, _status(unfreeze), _tools(unfreeze)))

    status5, spend_after, _ch5, _first5 = chat_approve(port, session_id, "刷信用卡 10 元买水")
    c.add("cap-spd-11", "解冻后 10 元消费恢复成功（status=ok + card_spend）",
          status5 == 200 and _status(spend_after) == "ok" and "card_spend" in _tools(spend_after),
          "http=%s status=%s executed=%s reply=%s"
          % (status5, _status(spend_after), _tools(spend_after), _short(_reply(spend_after))))


# ---------------------------------------------------------------- 场景：异常扫描 + 年度报告

def scenario_scan_report(app, c):
    port = app.port
    session_id = new_session(port)
    if not session_id:
        c.add("cap-rad-0", "创建会话", False, "POST /api/session 失败")
        return

    # 制造可判定样本：两笔「同收款方 + 同金额」的真实转账，命中 duplicate_charge 规则。
    # （card_spend 只写 card_txns，不写 transactions，不参与 scan_anomalies；种子近 30 天无重复扣款。）
    made = 0
    for _i in range(2):
        st_mk, mk, _ch, first_mk = chat_approve(port, session_id, "给妈妈转 800 元")
        if _status(mk) == "ok":
            made += 1
        else:
            c.add("cap-rad-0b", "制造重复扣款样本（第 %d 笔转账）" % (_i + 1), False,
                  "http=%s status=%s code=%s reply=%s"
                  % (st_mk, _status(mk), _code_from(st_mk, mk), _short(_reply(mk))))
    c.add("cap-rad-0a", "重复扣款样本已写入（给妈妈转 800 元 ×2，status=ok）",
          made == 2, "made=%d/2" % made)

    status, scan, _ = chat(port, session_id, "最近有没有异常交易")
    reply = _reply(scan)
    rules = ("amount_outlier", "duplicate_charge", "late_night_large")
    c.add("cap-rad-1", "异常扫描回复含规则名（amount_outlier/duplicate_charge/late_night_large）",
          status == 200 and _status(scan) == "ok" and _contains_any(reply, rules),
          "http=%s status=%s reply=%s" % (status, _status(scan), _short(reply)))
    c.add("cap-rad-1b", "异常扫描回复命中重复扣款的中文描述（重复/可疑/异常）",
          _contains_any(reply, ("重复", "可疑", "异常")), "reply=%s" % _short(reply))
    c.add("cap-rad-2", "异常扫描回复含具体金额（¥x.xx）",
          bool(re.search(r"¥\s?[\d,]+\.\d{2}", reply)),
          "reply=%s" % _short(reply))

    status2, annual, _ = chat(port, session_id, "今年的年度账单")
    reply2 = _reply(annual)
    year = str(current_year())
    c.add("cap-rad-3", "年度账单回复含当前年份 %s" % year,
          status2 == 200 and _status(annual) == "ok" and year in reply2,
          "http=%s status=%s reply=%s" % (status2, _status(annual), _short(reply2)))

    rows = _db_rows("SELECT COALESCE(SUM(amount_cents),0) AS total FROM transactions "
                    "WHERE user_id=? AND direction='out' AND ts LIKE ?", (DEFAULT_USER, year + "-%"))
    total_out = int(rows[0]["total"]) if rows else 0
    candidates = ["%.2f" % (total_out / 100.0), "{:,.2f}".format(total_out / 100.0)]
    hit = any(cand in reply2 for cand in candidates)
    c.add("cap-rad-4", "年度账单回复含总支出金额（DB 交叉校验 %s 元）" % _yuan(total_out),
          hit, "expected=%s reply=%s" % (candidates, _short(reply2)))


# ---------------------------------------------------------------- 场景：手机号转账

def scenario_phone_transfer(app, c):
    port = app.port
    session_id = new_session(port)
    if not session_id:
        c.add("cap-phone-0", "创建会话", False, "POST /api/session 失败")
        return

    status, known, _ = chat(port, session_id, "给 13800001111 转 8 元")
    hits = _hits(_approval(known))
    c.add("cap-phone-1", "给 13800001111（妈妈）转 8 元进入审批",
          status == 200 and _status(known) == "awaiting_approval",
          "http=%s status=%s reply=%s" % (status, _status(known), _short(_reply(known))))
    c.add("cap-phone-2", "命中通讯录 → 审批 hits 含 payee_known",
          "payee_known" in hits, "hits=%s approval=%s" % (hits, _short(_approval(known))))
    c.add("cap-phone-3", "已知收款人小额转账风险等级为 medium", _risk(known) == "medium",
          "risk=%s" % _risk(known))
    if _status(known) == "awaiting_approval":
        approve(port, session_id, known, decision="reject")  # 拒绝，避免真实扣款影响后续余额断言

    status2, unknown, _ = chat(port, session_id, "给 13999998888 转 8 元")
    hits2 = _hits(_approval(unknown))
    c.add("cap-phone-4", "陌生手机号转账进入二次确认而非 blocked",
          status2 == 200 and _status(unknown) == "awaiting_approval",
          "http=%s status=%s code=%s reply=%s"
          % (status2, _status(unknown), _code_from(status2, unknown), _short(_reply(unknown))))
    c.add("cap-phone-5", "陌生手机号 → 高风险 TOTP（risk_level=high / challenge=totp）",
          _risk(unknown) == "high" and _challenge(unknown) == "totp",
          "risk=%s challenge=%s" % (_risk(unknown), _challenge(unknown)))
    c.add("cap-phone-6", "陌生手机号审批 hits 含 payee_new / payee_unverified",
          _contains_any(hits2, ("payee_new", "payee_unverified")),
          "hits=%s" % hits2)
    if _status(unknown) == "awaiting_approval":
        approve(port, session_id, unknown, decision="reject")


# ---------------------------------------------------------------- 场景：预约转账

def scenario_scheduled(app, c):
    port = app.port
    session_id = new_session(port)
    if not session_id:
        c.add("cap-sch-0", "创建会话", False, "POST /api/session 失败")
        return

    status, created, _ = chat(port, session_id, "3 天后给妈妈转 800 元")
    c.add("cap-sch-1", "创建预约转账进入审批（status=awaiting_approval）",
          status == 200 and _status(created) == "awaiting_approval",
          "http=%s status=%s reply=%s" % (status, _status(created), _short(_reply(created))))
    c.add("cap-sch-2", "预约计划含 create_scheduled_transfer 步骤",
          "create_scheduled_transfer" in _plan_tools(created),
          "plan_tools=%s" % _plan_tools(created))

    if _status(created) == "awaiting_approval":
        st_approve, final, _ = approve(port, session_id, created)
    else:
        st_approve, final = status, created
    c.add("cap-sch-3", "审批通过后预约创建成功（ok + create_scheduled_transfer）",
          st_approve == 200 and _status(final) == "ok" and "create_scheduled_transfer" in _tools(final),
          "http=%s status=%s executed=%s reply=%s"
          % (st_approve, _status(final), _tools(final), _short(_reply(final))))

    before_balance = checking_total(port, session_id)
    status_list, listed, _ = _get(port, "/api/scheduled?session_id=" + urllib.parse.quote(session_id))
    pending = [it for it in sched_items(listed) if str(it.get("status") or "") == "pending"]
    sched_id = ""
    if pending:
        target = pending[0]
        sched_id = str(target.get("sched_id") or "")
    c.add("cap-sch-4", "GET /api/scheduled 有 1 条 pending（妈妈 / 800 元）",
          status_list == 200 and len(pending) >= 1
          and any(str(it.get("payee_name") or "") == "妈妈" and int(it.get("amount_cents") or 0) == 80000
                  for it in pending),
          "http=%s pending=%d items=%s" % (status_list, len(pending), _short(pending)))

    # 服务端 POST /api/scheduler/tick 目前忽略请求体（不接受自定义时间），先按契约传 now/now_iso
    # 做一次前向兼容调用；若没有执行结果，再用测试夹具把 run_at 改到过去后重试（只改隔离副本 DB）。
    future = _future_iso(5)
    status_tick, ticked, _ = _post(port, "/api/scheduler/tick", {"now": future, "now_iso": future})
    executed = _dig(ticked, "executed", default=[]) or []
    executed_ids = [str(it.get("sched_id") or "") for it in executed if isinstance(it, dict)]
    fixture_used = False
    if sched_id and sched_id not in executed_ids:
        fixture_used = _mark_schedule_due(sched_id, _iso(_now() - timedelta(days=1)))
        status_tick, ticked, _ = _post(port, "/api/scheduler/tick", {})
        executed = _dig(ticked, "executed", default=[]) or []
        executed_ids = [str(it.get("sched_id") or "") for it in executed if isinstance(it, dict)]
    c.add("cap-sch-5", "scheduler/tick 执行到期预约（executed 含 sched_id）",
          status_tick == 200 and bool(sched_id) and sched_id in executed_ids,
          "http=%s sched_id=%r fixture=%s executed=%s"
          % (status_tick, sched_id, fixture_used, _short(ticked)))

    status_after, listed_after, _ = _get(port, "/api/scheduled?session_id=" + urllib.parse.quote(session_id))
    target_row = {}
    for it in sched_items(listed_after):
        if str(it.get("sched_id") or "") == sched_id:
            target_row = it
            break
    db_after = _db_rows("SELECT status, transfer_id, executed_at FROM scheduled_transfers WHERE sched_id=?",
                        (sched_id,))
    db_row = db_after[0] if db_after else {}
    c.add("cap-sch-6", "预约状态变为 executed（API）且 DB 记录 transfer_id/executed_at",
          status_after == 200 and str(target_row.get("status") or "") == "executed"
          and str(db_row.get("status") or "") == "executed"
          and bool(db_row.get("transfer_id")) and bool(db_row.get("executed_at")),
          "api_row=%s db_row=%s" % (_short(target_row), _short(db_row)))

    after_balance = checking_total(port, session_id)
    c.add("cap-sch-7", "tick 后 checking 余额减少 80000 分（800 元）",
          after_balance == before_balance - 80000,
          "before=%d after=%d delta=%d" % (before_balance, after_balance, after_balance - before_balance))

    global_audit = audit(port, "", limit=600)
    sched_events = [e for e in entries(global_audit)
                    if str(e.get("event_type") or "") == "scheduled_transfer_executed"]
    c.add("cap-sch-8", "审计出现 scheduled_transfer_executed", len(sched_events) >= 1,
          "hits=%d entries=%d" % (len(sched_events), len(entries(global_audit))))

    status_c, created2, _ = chat(port, session_id, "3 天后给妈妈转 100 元")
    if _status(created2) == "awaiting_approval":
        _, final2, _ = approve(port, session_id, created2)
    status_list2, listed2, _ = _get(port, "/api/scheduled?session_id=" + urllib.parse.quote(session_id))
    pending2 = [it for it in sched_items(listed2) if str(it.get("status") or "") == "pending"]
    latest = _db_rows("SELECT sched_id FROM scheduled_transfers WHERE user_id=? AND status='pending' "
                       "ORDER BY created_at DESC LIMIT 1", (DEFAULT_USER,))
    second_id = str(latest[0].get("sched_id") or "") if latest else ""
    if not second_id or second_id == sched_id:
        second_id = str(pending2[-1].get("sched_id") or "") if pending2 else ""
    c.add("cap-sch-9", "再建一笔 100 元预约成功（pending，且与第一笔不同）",
          status_c == 200 and bool(second_id) and second_id != sched_id,
          "http=%s second_id=%r pending=%s" % (status_c, second_id, _short(pending2)))

    status_cancel, cancelled, _ch_c, first_c = chat_approve(port, session_id, "取消最近一笔预约")
    ok_cancel = (status_cancel == 200 and _status(cancelled) == "ok"
                 and _contains_any(_reply(cancelled), ("取消", "已取消")))
    c.add("cap-sch-10", "取消最近一笔预约成功（reply 含取消）",
          ok_cancel, "http=%s status=%s reply=%s first=%s"
          % (status_cancel, _status(cancelled), _short(_reply(cancelled)), _short(first_c)))

    status_list3, listed3, _ = _get(port, "/api/scheduled?session_id=" + urllib.parse.quote(session_id))
    cancelled_row = {}
    for it in sched_items(listed3):
        if str(it.get("sched_id") or "") == second_id:
            cancelled_row = it
            break
    c.add("cap-sch-11", "被取消的预约为 cancelled 状态",
          str(cancelled_row.get("status") or "") == "cancelled",
          "row=%s" % _short(cancelled_row))

    db_rows = _db_rows("SELECT sched_id, status FROM scheduled_transfers WHERE sched_id IN (?, ?)",
                       (sched_id, second_id)) if sched_id and second_id else []
    db_status = {str(r.get("sched_id")): str(r.get("status")) for r in db_rows}
    c.add("cap-sch-12", "DB 交叉校验：第一笔 executed / 第二笔 cancelled",
          db_status.get(sched_id) == "executed" and db_status.get(second_id) == "cancelled",
          "db=%s" % _short(db_status))


# ---------------------------------------------------------------- 场景：最终校验

def scenario_final(app, c, port):
    status, payload, _ = _get(port, "/api/audit?limit=1000")
    chain = _dig(payload, "chain", default={}) or {}
    checked = int(chain.get("checked") or 0)
    c.add("cap-final-1", "审计哈希链 valid 且 checked>0",
          status == 200 and bool(chain.get("valid")) and checked > 0,
          "http=%s chain=%s entries=%d" % (status, _short(chain), len(entries(payload))))
    ok_log, where = app.no_traceback()
    c.add("cap-final-2", "服务日志无 Traceback", ok_log, "traceback=%s" % where)


# ---------------------------------------------------------------- 冒烟（脚本机制自检）

def scenario_smoke(app, c):
    port = app.port
    status, health, _ = _get(port, "/api/health")
    c.add("cap-smoke-1", "GET /api/health 200 且 status=ok",
          status == 200 and _status(health) == "ok", "http=%s body=%s" % (status, _short(health)))
    status, session, _ = _post(port, "/api/session", {})
    session_id = str(_dig(session, "session_id") or "")
    c.add("cap-smoke-2", "POST /api/session 返回 session_id",
          status == 200 and bool(session_id), "http=%s body=%s" % (status, _short(session)))
    status, acc, _ = _get(port, "/api/accounts?session_id=" + urllib.parse.quote(session_id))
    c.add("cap-smoke-3", "GET /api/accounts 返回账户列表",
          status == 200 and isinstance(acc, dict) and bool(acc.get("accounts")),
          "http=%s accounts=%d" % (status, len(acc.get("accounts") or [])))
    status, audit_payload, _ = _get(port, "/api/audit?limit=50")
    chain = _dig(audit_payload, "chain", default={}) or {}
    # 新建副本的审计链可能为空（checked=0），只要链本身 valid 即可
    c.add("cap-smoke-4", "GET /api/audit 返回 entries 结构 + valid 链",
          status == 200 and isinstance(chain, dict) and chain.get("valid") is True,
          "http=%s chain=%s entries=%d" % (status, _short(chain), len(entries(audit_payload))))
    status, reset, _ = _post(port, "/api/admin/reset", {})
    c.add("cap-smoke-5", "POST /api/admin/reset 200",
          status == 200, "http=%s body=%s" % (status, _short(reset)))
    rows = _db_rows("SELECT COUNT(*) AS n FROM accounts")
    c.add("cap-smoke-6", "只读 sqlite 直读隔离副本 DB 正常",
          bool(rows) and int(rows[0].get("n") or 0) > 0, "rows=%s" % _short(rows))


# ---------------------------------------------------------------- 主流程

def _parse_args(argv):
    opts = {"smoke": False, "json": False, "only": "", "verbose": False}
    for arg in argv:
        if arg == "--smoke":
            opts["smoke"] = True
        elif arg == "--json":
            opts["json"] = True
        elif arg in ("-v", "--verbose"):
            opts["verbose"] = True
        elif arg.startswith("--only="):
            opts["only"] = arg.split("=", 1)[1]
        elif arg == "--only":
            opts["only"] = "next"
        elif opts["only"] == "next":
            opts["only"] = arg
        else:
            print("未知参数：%s" % arg)
            print(__doc__)
            raise SystemExit(2)
    if isinstance(opts["only"], str) and opts["only"] not in ("", "next"):
        opts["only"] = [x.strip() for x in opts["only"].split(",") if x.strip()]
    else:
        opts["only"] = []
    return opts


def main(argv=None):
    opts = _parse_args(list(sys.argv[1:] if argv is None else argv))
    checks = Checks(quiet=opts["json"])
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    if not opts["json"]:
        print("=" * 78)
        print("ZeroStep v0.4.0 新能力 e2e（capability_e2e）  版本：%s" % version)
        print("仓库：%s" % ROOT)
        print("模式：%s" % ("冒烟自检" if opts["smoke"] else "全量"))
        print("=" * 78)

    app = None
    port = 0
    try:
        _prepare_app()
        port = _pick_port(PREFERRED_PORT)
        if not opts["json"]:
            print("隔离副本：%s\n主服务端口：%d" % (APP_DIR, port))
        app = AppServer(port)
        if not app.start():
            print("[ENV] 主服务启动失败（端口 %d）" % port)
            print(app.logs_tail())
            return 2
        if not opts["json"]:
            print("主服务已就绪：http://%s:%d\n" % (HOST, port))
        try:
            if opts["smoke"]:
                scenario_smoke(app, checks)
            else:
                scenarios = [
                    ("env", scenario_env),
                    ("redeem", scenario_redeem),
                    ("card_limit", scenario_card_limit),
                    ("card_spend", scenario_card_spend),
                    ("phone_transfer", scenario_phone_transfer),
                    ("scan_report", scenario_scan_report),
                    ("scheduled", scenario_scheduled),
                ]
                for name, func in scenarios:
                    try:
                        func(app, checks)
                    except Exception as exc:  # noqa: BLE001 - 单个场景异常不中断其它场景
                        cid = "cap-run-" + name
                        detail = "%s: %s | %s" % (type(exc).__name__, exc,
                                                  traceback.format_exc(limit=2).replace("\n", " ")[:400])
                        checks.add(cid, "场景 %s 执行异常" % name, False, detail)
                try:
                    scenario_final(app, checks, port)
                except Exception as exc:  # noqa: BLE001
                    checks.add("cap-run-final", "最终校验执行异常", False, "%s: %s" % (type(exc).__name__, exc))
        finally:
            app.stop()
            app = None
    except Exception as exc:  # noqa: BLE001 - 环境错误统一退出码 2
        print("[ENV] %s: %s" % (type(exc).__name__, exc))
        if app is not None:
            print(app.logs_tail())
        return 2

    # 收尾：端口必须无残留监听
    residue = []
    if port:
        deadline = time.time() + 5.0
        while time.time() < deadline and _port_listening(port):
            time.sleep(0.2)
        if _port_listening(port):
            residue.append(port)
    checks.add("cap-final-3", "跑完无端口残留监听（%d）" % port, not residue,
               "residue=%s" % residue)

    passed, total, failed = checks.result()
    if opts["only"]:
        # --only 时只保留选中的检查项重新统计
        selected = [it for it in checks.items if it["id"] in opts["only"]]
        passed = sum(1 for it in selected if it["ok"])
        total = len(selected)
        failed = [it for it in selected if not it["ok"]]
    if opts["json"]:
        print(json.dumps({"passed": passed, "total": total,
                          "failed": [it["id"] for it in failed],
                          "checks": checks.items}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "-" * 78)
        if failed:
            print("失败项：")
            for it in failed:
                print("  - %s %s | %s" % (it["id"], it["desc"], it["detail"]))
        print("结果：%d/%d 通过%s" % (passed, total, "" if not failed else "（%d 失败）" % len(failed)))
        print("退出码：%d" % (0 if not failed else 1))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
