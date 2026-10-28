#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ZeroStep v0.4.1 安全因子 e2e（真实 HTTP，零依赖）。

覆盖 v0.4.1 三条新增安全能力：
  1. 挂失/解挂升红：set_card_status 进入 HIGH 工具集，审批 risk=high / challenge=totp，
     顶层 factors=["totp","sms","face"]，hits 含 card_status_change（detail 带卡号后四位与「红色强验证」）。
  2. 多因子强验证：TOTP（兼容旧调用）/ 短信验证码 / 人脸令牌 三种方式各自成功与失败；
     错误 method / 错误码 / 短信 5 次失败作废 / 错误人脸令牌不消耗 等边界。
  3. 会话安全锁定：同一会话 10 分钟内 3 次被拦（policy turn_blocked 或注入 attack_blocked）
     → 锁定 300 秒；第 3 轮本身仍返回 blocked 且 reply 追加「已临时锁定」，第 4 轮起
     status=locked / code=SESSION_LOCKED，未解析意图、未建计划、未执行任何工具；
     锁按会话隔离；/api/admin/reset 清空锁表；短冷却实例（8044，SECURITY_LOCK_SECONDS=2）
     验证冷却到期后自动恢复。

设计要点：
  * 全流程走真实 HTTP（urllib，仅标准库）；卡片状态 / session_locks 表 / 审计事件用只读
    sqlite 直读隔离副本 DB 交叉验证（审计 entries 不返回 args/result 原文，故 method
    只能通过行为断言：同一短信码走 totp 失败、走 sms 成功）。
  * 隔离副本 .eval_tmp/security_factor_e2e/app 与 app2（复制 backend + skills + VERSION，
    忽略 data/__pycache__），独立端口 8441（主实例，锁 300 秒）与 8044（冷却 2 秒的恢复实例），
    绝不碰 Lead 的 8000 / 8401。
  * 环境：DEMO_MODE=1 ZEROSTEP_DISABLE_LLM=1 MCP_ENABLED=0 CHANNEL_ENABLED=0
    SCHEDULER_ENABLED=0 PLUGINS_FILE 隔离；app2 额外 SECURITY_LOCK_SECONDS=2。
  * 退出码：0 全过 / 1 有失败 / 2 环境错误（起不来服务等）。

用法：
  python scripts/security_factor_e2e.py                 # 全量
  python scripts/security_factor_e2e.py --only sec-red-1,sec-lock-5
  python scripts/security_factor_e2e.py --json          # 机器可读输出（最后一段 JSON）
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
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent          # bank-agent/
WORK = ROOT / ".eval_tmp" / "security_factor_e2e"
LOG_DIR = WORK / "logs"
APP1_DIR = WORK / "app"
APP2_DIR = WORK / "app2"
APP1_DB = APP1_DIR / "backend" / "data" / "bank.db"
APP2_DB = APP2_DIR / "backend" / "data" / "bank.db"
VERSION_FILE = ROOT / "VERSION"
HOST = "127.0.0.1"
PREFERRED_PORT = 8441        # 主实例（默认锁 300 秒）
RECOVERY_PORT = 8044         # 恢复实例（SECURITY_LOCK_SECONDS=2）
DEFAULT_USER = "u001"
START_TIMEOUT = 40.0

FACE = "/api/demo/face"
SMS = "/api/demo/sms"

# ---------------------------------------------------------------- 基础工具


def _now():
    return datetime.now().astimezone()


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def _short(obj, limit=320):
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


def _contains(text, word):
    return str(word) in str(text or "")


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


def _pick_port(preferred):
    """优先 preferred，被占用时清理该端口残留监听进程，再顺延；不用 SO_REUSEADDR 探测。"""
    candidates = [preferred] + [p for p in range(preferred + 1, preferred + 11)]
    for port in candidates:
        if _port_listening(port):
            for pid in _port_pids(port):
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


def _decode(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _http(method, url, payload=None, timeout=25):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, ValueError):
            raw = b""
        return exc.code, _decode(raw), dict(exc.headers or {})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"error": {"code": "NETWORK", "message": str(exc)}}, {}


def _get(port, path, timeout=20):
    return _http("GET", "http://%s:%d%s" % (HOST, port, path), timeout=timeout)


def _post(port, path, payload=None, timeout=40):
    return _http("POST", "http://%s:%d%s" % (HOST, port, path),
                 payload if payload is not None else {}, timeout=timeout)


# ---------------------------------------------------------------- 响应解读


def _status(payload):
    return str(_dig(payload, "status") or "")


def _reply(payload):
    return str(_dig(payload, "reply") or "")


def _approval(payload):
    val = _dig(payload, "approval")
    return val if isinstance(val, dict) else {}


def _risk(payload):
    return str(_dig(payload, "approval", "risk_level") or "")


def _challenge(payload):
    return str(_dig(payload, "approval", "challenge", "type") or "")


def _factors(payload):
    val = _dig(payload, "approval", "factors", default=[])
    return list(val) if isinstance(val, list) else []


def _hits(payload):
    val = _dig(payload, "approval", "hits", default=[])
    return [h for h in (val or []) if isinstance(h, dict)]


def _hit_rules(payload):
    return [str(h.get("rule") or "") for h in _hits(payload)]


def _hit_by_rule(payload, rule):
    for h in _hits(payload):
        if str(h.get("rule") or "") == rule:
            return h
    return {}


def _plan_tools(payload):
    out = []
    for item in _dig(payload, "plan", "steps", default=[]) or []:
        if isinstance(item, dict) and item.get("tool"):
            out.append(str(item["tool"]))
    return out


def _executed_tools(payload):
    out = []
    for item in _dig(payload, "executed", default=[]) or []:
        if isinstance(item, dict) and item.get("tool"):
            out.append(str(item["tool"]))
    return out


def _code(payload):
    for key in ("blocked", "error"):
        val = _dig(payload, key)
        if isinstance(val, dict) and val.get("code"):
            return str(val["code"])
    return str(_dig(payload, "code") or "")


def _session_new(port):
    status, payload, _ = _post(port, "/api/session", {})
    if status == 200 and isinstance(payload, dict):
        return str(payload.get("session_id") or "")
    return ""


def _chat(port, session_id, text):
    return _post(port, "/api/chat", {"session_id": session_id, "text": text})


def _approve(port, session_id, approval_id, decision="approve", code="", method=None, timeout=40):
    body = {"session_id": session_id, "approval_id": approval_id, "decision": decision, "code": code}
    if method is not None:
        body["method"] = method
    return _post(port, "/api/approval", body, timeout=timeout)


def _demo_sms(port, user_id=DEFAULT_USER):
    return _get(port, SMS + "?user_id=" + urllib.parse.quote(user_id))


def _demo_face(port, user_id=DEFAULT_USER):
    return _post(port, FACE, {"user_id": user_id})


def _demo_totp(port, user_id=DEFAULT_USER):
    return _get(port, "/api/demo/totp?user_id=" + urllib.parse.quote(user_id))


def _audit(port, session_id="", limit=500):
    path = "/api/audit?limit=%d" % limit
    if session_id:
        path += "&session_id=" + urllib.parse.quote(session_id)
    status, payload, _ = _get(port, path)
    return payload if status == 200 and isinstance(payload, dict) else {}


def _entries(payload):
    val = _dig(payload, "entries", default=[]) or []
    return [e for e in val if isinstance(e, dict)]


def _entry_types(payload):
    return [str(e.get("event_type") or "") for e in _entries(payload)]


def _db(db_path, sql, params=()):
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
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


def _card_status(db_path, card_id="card_002"):
    rows = _db(db_path, "SELECT status FROM cards WHERE card_id=?", (card_id,))
    return str(rows[0]["status"]) if rows else ""


def _balance_cents(db_path, account_id):
    rows = _db(db_path, "SELECT balance_cents FROM accounts WHERE account_id=?", (account_id,))
    return int(rows[0]["balance_cents"]) if rows else None


def _checking_account(db_path, user_id=DEFAULT_USER):
    rows = _db(db_path, "SELECT account_id FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1",
               (user_id,))
    return str(rows[0]["account_id"]) if rows else ""


def _all_chat_approvals(port, session_id, text):
    """发一条消息；若进入审批返回 (first, approval_id)，否则 (first, '')。"""
    status, payload, _ = _chat(port, session_id, text)
    approval_id = ""
    if _status(payload) == "awaiting_approval":
        approval_id = str(_approval(payload).get("approval_id") or "")
    return payload, approval_id


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


# ---------------------------------------------------------------- 隔离副本 / 服务进程


def _clean_env():
    env = {}
    drop_prefix = ("DEEPSEEK_", "LLM_", "MCP_", "CHANNEL_", "WECOM_", "PUSH_",
                   "SCHEDULER_", "ZEROSTEP_", "PLUGINS_", "TOTP_", "SEED_",
                   "SMS_", "FACE_", "SECURITY_")
    drop_exact = {"PORT", "HOST", "DEMO_MODE"}
    for key, val in os.environ.items():
        if key in drop_exact or key.upper().startswith(drop_prefix):
            continue
        env[key] = val
    return env


def _app_env(port, plugins_file, extra=None):
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
        "PLUGINS_FILE": str(plugins_file),
    })
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _fresh_app_dir(name):
    """准备一份干净的隔离副本；旧目录被占用（Windows 共享冲突）时退让到带时间戳的新目录。"""
    WORK.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = WORK / name
    if target.exists():
        try:
            shutil.rmtree(str(target))
        except OSError:
            for stale in WORK.glob(name + "*"):
                if stale == target:
                    continue
                shutil.rmtree(str(stale), ignore_errors=True)
            target = WORK / ("%s_%s_%d" % (name, time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
    target.mkdir(parents=True, exist_ok=True)

    def _ignore(directory, names):
        ignored = set()
        for item in names:
            if item in ("__pycache__", "data", "sessions.json", "messages.jsonl") or item.endswith(".pyc"):
                ignored.add(item)
        return ignored

    shutil.copytree(str(ROOT / "backend"), str(target / "backend"), ignore=_ignore)
    if (ROOT / "skills").exists():
        shutil.copytree(str(ROOT / "skills"), str(target / "skills"), ignore=_ignore)
    if VERSION_FILE.exists():
        shutil.copy2(str(VERSION_FILE), str(target / "VERSION"))
    (target / "backend" / "data").mkdir(parents=True, exist_ok=True)
    return target


class AppServer(object):
    def __init__(self, port, app_dir, db_path, tag, extra_env=None):
        self.port = port
        self.app_dir = app_dir
        self.db_path = db_path
        self.tag = tag
        self.extra_env = extra_env or {}
        self.proc = None
        self._fhs = []
        self.log_paths = []

    def start(self):
        out_path = LOG_DIR / ("app_%s.log" % self.tag)
        self.log_paths = [out_path]
        out = open(str(out_path), "wb")
        self._fhs = [out]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [sys.executable or "python", str(self.app_dir / "backend" / "server.py")],
            cwd=str(self.app_dir),
            env=_app_env(self.port, WORK / ("plugins_%s.json" % self.tag), self.extra_env),
            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
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

    def logs_tail(self, lines=60):
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
    c.add("sec-env-1", "GET /api/health 返回 200、status=ok、version 与仓库 VERSION 一致",
          status == 200 and _status(health) == "ok" and str(_dig(health, "version") or "") == VERSION_FILE.read_text(encoding="utf-8").strip(),
          "http=%s %s" % (status, _short(health)))
    seed_version = str(_dig(health, "seed_version") or "")
    c.add("sec-env-2", "health.seed_version 为 2026.10.29（v0.4.1 未改种子）",
          seed_version == "2026.10.29", "seed_version=%r" % seed_version)

    status, sms, _ = _demo_sms(port)
    code = str(_dig(sms, "code") or "")
    ok = (status == 200 and bool(sms.get("ok")) and len(code) == 6 and code.isdigit()
          and int(_dig(sms, "ttl", default=0) or 0) > 0 and int(_dig(sms, "seconds_left", default=0) or 0) > 0)
    c.add("sec-env-3", "GET /api/demo/sms 返回 200、6 位数字码、ttl>0",
          ok, "http=%s %s" % (status, _short(sms)))

    status, face, _ = _demo_face(port)
    token = str(_dig(face, "token") or "")
    ok = (status == 200 and bool(face.get("ok")) and token.startswith("fc_") and len(token) > 12
          and int(_dig(face, "ttl", default=0) or 0) > 0)
    c.add("sec-env-4", "POST /api/demo/face 返回 200、fc_ 前缀令牌、ttl>0",
          ok, "http=%s %s" % (status, _short(face)))
    return {"sms": sms, "face": face}


# ---------------------------------------------------------------- 场景：挂失升红


def scenario_red(app, c):
    port = app.port
    status, reset, _ = _post(port, "/api/admin/reset", {})
    c.add("sec-red-1", "前置：POST /api/admin/reset 成功（200 + reset=true）",
          status == 200 and bool(_dig(reset, "reset")),
          "http=%s %s" % (status, _short(reset)))

    sid = _session_new(port)
    status, first, _ = _chat(port, sid, "把我的信用卡挂失")
    ap = _approval(first)
    approval_id = str(ap.get("approval_id") or "")
    c.add("sec-red-2", "“把我的信用卡挂失”进入审批：status=awaiting_approval / intent=card_freeze / 计划含 set_card_status",
          _status(first) == "awaiting_approval" and str(_dig(first, "intent", "name") or "") == "card_freeze"
          and _plan_tools(first) == ["set_card_status"],
          "status=%r intent=%r plan=%s approval_id=%r" % (_status(first), _dig(first, "intent", "name"), _plan_tools(first), approval_id))
    c.add("sec-red-3", "挂失审批升红：risk_level=high、challenge=totp",
          _risk(first) == "high" and _challenge(first) == "totp",
          "risk=%r challenge=%r" % (_risk(first), _challenge(first)))
    c.add("sec-red-4", "红色审批顶层 factors 恰好为 [totp, sms, face]",
          _factors(first) == ["totp", "sms", "face"], "factors=%r" % (_factors(first),))
    hit = _hit_by_rule(first, "card_status_change")
    detail = str(hit.get("detail") or "")
    c.add("sec-red-5", "hits 含 card_status_change（level=high，detail 带卡号 6712 与「红色强验证」）",
          bool(hit) and str(hit.get("level") or "") == "high" and _contains(detail, "6712")
          and _contains(detail, "红色强验证"),
          "hit=%s" % _short(hit))
    combined = json.dumps(ap, ensure_ascii=False)
    c.add("sec-red-6", "审批单文案含动作与卡号（挂失 / 6712 / 不可自动完成）",
          _contains(combined, "挂失") and _contains(combined, "6712") and _contains(combined, "不可"),
          _short(combined, 420))

    sid2 = _session_new(port)
    _, medium, _ = _chat(port, sid2, "给妈妈转 800 元")
    c.add("sec-red-7", "medium 审批（转账）factors 为空数组，不显示因子选择",
          _risk(medium) == "medium" and _factors(medium) == [],
          "risk=%r factors=%r" % (_risk(medium), _factors(medium)))

    return {"session_id": sid, "approval_id": approval_id}


# ---------------------------------------------------------------- 场景：method 路由 / 短信成功


def scenario_method(app, c):
    port = app.port
    sid = _session_new(port)
    first, approval_id = _all_chat_approvals(port, sid, "把我的信用卡挂失")
    c.add("sec-method-1", "前置：挂失进入红色审批（拿到 approval_id）",
          bool(approval_id) and _risk(first) == "high", "approval_id=%r" % approval_id)

    status, bad, _ = _approve(port, sid, approval_id, method="bogus", code="123456")
    c.add("sec-method-2", "method=bogus → 400 INVALID_METHOD「不支持的校验方式」",
          status == 400 and _code(bad) == "INVALID_METHOD" and _contains(json.dumps(bad, ensure_ascii=False), "不支持的校验方式"),
          "http=%s %s" % (status, _short(bad)))

    status, sms, _ = _demo_sms(port)
    sms_code = str(_dig(sms, "code") or "")
    status_t, totp, _ = _demo_totp(port)
    totp_code = str(_dig(totp, "code") or "")
    if sms_code and sms_code == totp_code:  # 极小概率撞码，重新取一条
        status, sms, _ = _demo_sms(port)
        sms_code = str(_dig(sms, "code") or "")
    status, bad, _ = _approve(port, sid, approval_id, method=None, code=sms_code)
    c.add("sec-method-3", "不带 method 时短信码按 TOTP 校验 → 400 INVALID_OTP（证明 method 分发）",
          status == 400 and _code(bad) == "INVALID_OTP",
          "http=%s code=%r sms_code=%r" % (status, _code(bad), sms_code))

    status, bad, _ = _approve(port, sid, approval_id, method="sms", code="000000" if sms_code != "000000" else "111111")
    c.add("sec-method-4", "method=sms + 错误码 → 400 INVALID_SMS「短信验证码不正确或已过期，请重新获取」",
          status == 400 and _code(bad) == "INVALID_SMS" and _contains(json.dumps(bad, ensure_ascii=False), "短信验证码不正确"),
          "http=%s %s" % (status, _short(bad)))

    status, sms, _ = _demo_sms(port)
    sms_code = str(_dig(sms, "code") or "")
    status, done, _ = _approve(port, sid, approval_id, method="sms", code=sms_code)
    c.add("sec-method-5", "method=sms + 正确短信码 → 200 status=ok 且执行 set_card_status",
          status == 200 and _status(done) == "ok" and _executed_tools(done) == ["set_card_status"],
          "http=%s status=%r executed=%s reply=%s" % (status, _status(done), _executed_tools(done), _reply(done)))
    c.add("sec-method-6", "短信因子通过后卡片状态落库为 frozen（card_002）",
          _card_status(app.db_path) == "frozen", "card_002.status=%r" % _card_status(app.db_path))

    audit_payload = _audit(port, sid)
    granted = [e for e in _entries(audit_payload) if str(e.get("event_type") or "") == "approval_granted"]
    c.add("sec-method-7", "审计含 approval_granted（approval_id 对得上）且审计链 valid",
          bool(granted) and any(str(e.get("approval_id") or "") == approval_id for e in granted)
          and bool(_dig(audit_payload, "chain", "valid")),
          "granted=%d approval_id_match=%s chain=%s" % (
              len(granted), any(str(e.get("approval_id") or "") == approval_id for e in granted),
              _short(_dig(audit_payload, "chain"))))

    status, again, _ = _approve(port, sid, approval_id, method="sms", code=sms_code)
    c.add("sec-method-8", "已决审批重复提交 → 409 APPROVAL_ALREADY_DECIDED",
          status == 409 and _code(again) == "APPROVAL_ALREADY_DECIDED",
          "http=%s %s" % (status, _short(again)))
    return {"session_id": sid, "approval_id": approval_id}


# ---------------------------------------------------------------- 场景：审批单归属（IDOR）/ 二次决策


def scenario_idor(app, c):
    """审批单绑定会话：跨会话提交必须被拒；同一 approval_id 只能执行一次资金动作。"""
    port = app.port
    sid_a = _session_new(port)
    sid_b = _session_new(port)
    _, approval_id = _all_chat_approvals(port, sid_a, "给妈妈转 800 元")
    account_id = _checking_account(app.db_path)
    before = _balance_cents(app.db_path, account_id)

    status_b, mismatch, _ = _approve(port, sid_b, approval_id)
    rows = _db(app.db_path, "SELECT status FROM approvals WHERE approval_id=?", (approval_id,))
    still_pending = bool(rows) and str(rows[0]["status"]) == "pending"
    after_mismatch = _balance_cents(app.db_path, account_id)
    c.add("sec-idor-1", "跨会话提交他人审批单 → 403 APPROVAL_SESSION_MISMATCH，审批单仍 pending 且资金未变动",
          status_b == 403 and _code(mismatch) == "APPROVAL_SESSION_MISMATCH" and still_pending
          and before is not None and after_mismatch == before,
          "http=%s %s approval_status=%r balance=%r->%r"
          % (status_b, _code(mismatch), [r.get("status") for r in rows], before, after_mismatch))

    status_ok, done, _ = _approve(port, sid_a, approval_id)
    after_ok = _balance_cents(app.db_path, account_id)
    status_again, again, _ = _approve(port, sid_a, approval_id)
    after_again = _balance_cents(app.db_path, account_id)
    granted = [e for e in _entries(_audit(port))
               if str(e.get("event_type") or "") == "approval_granted"
               and str(e.get("approval_id") or "") == approval_id]
    expected_after = (before - 80000) if before is not None else None
    c.add("sec-idor-2", "同一 approval_id 二次 approve → 409 APPROVAL_ALREADY_DECIDED，资金只扣一次且 approval_granted 仅 1 条",
          status_ok == 200 and _status(done) == "ok" and status_again == 409
          and _code(again) == "APPROVAL_ALREADY_DECIDED" and after_ok == expected_after
          and after_again == expected_after and len(granted) == 1,
          "first=%s/%r second=%s/%s balance=%r->%r->%r granted=%d"
          % (status_ok, _status(done), status_again, _code(again), before, after_ok, after_again, len(granted)))
    return {"session_id": sid_a, "approval_id": approval_id}


# ---------------------------------------------------------------- 场景：人脸因子


def scenario_face(app, c):
    port = app.port
    sid = _session_new(port)
    first, approval_id = _all_chat_approvals(port, sid, "把我的信用卡解冻")
    c.add("sec-face-1", "前置：解挂同样升红（risk=high、factors 三件、hits 含 card_status_change）",
          _risk(first) == "high" and _factors(first) == ["totp", "sms", "face"]
          and "card_status_change" in _hit_rules(first),
          "risk=%r factors=%r hits=%r" % (_risk(first), _factors(first), _hit_rules(first)))

    status, bad, _ = _approve(port, sid, approval_id, method="face", code="fc_deadbeef")
    c.add("sec-face-2", "错误人脸令牌 → 400 INVALID_FACE「人脸确认已过期，请重新发起」",
          status == 400 and _code(bad) == "INVALID_FACE" and _contains(json.dumps(bad, ensure_ascii=False), "人脸确认已过期"),
          "http=%s %s" % (status, _short(bad)))

    status, face, _ = _demo_face(port)
    token = str(_dig(face, "token") or "")
    status, done, _ = _approve(port, sid, approval_id, method="face", code=token)
    c.add("sec-face-3", "POST /api/demo/face 取令牌后 method=face 成功执行 set_card_status",
          status == 200 and _status(done) == "ok" and _executed_tools(done) == ["set_card_status"],
          "http=%s status=%r executed=%s reply=%s" % (status, _status(done), _executed_tools(done), _reply(done)))
    c.add("sec-face-4", "错误令牌不消耗授权：成功后卡片状态落库为 active",
          _card_status(app.db_path) == "active", "card_002.status=%r" % _card_status(app.db_path))
    return {"session_id": sid, "approval_id": approval_id}


# ---------------------------------------------------------------- 场景：短信 5 次失败作废


def scenario_sms(app, c):
    port = app.port
    sid = _session_new(port)
    first, approval_id = _all_chat_approvals(port, sid, "把我的信用卡挂失")
    status, sms, _ = _demo_sms(port)
    sms_code = str(_dig(sms, "code") or "")
    wrong = "000000" if sms_code != "000000" else "111111"
    fails = []
    for _ in range(5):
        status, bad, _ = _approve(port, sid, approval_id, method="sms", code=wrong)
        fails.append((status, _code(bad)))
    c.add("sec-sms-1", "短信码连续错 5 次 → 每次均 400 INVALID_SMS",
          all(s == 400 and code == "INVALID_SMS" for s, code in fails),
          "attempts=%s" % fails)

    status, bad, _ = _approve(port, sid, approval_id, method="sms", code=sms_code)
    c.add("sec-sms-2", "5 次失败后即使提交原正确码也 INVALID_SMS（验证码已作废）",
          status == 400 and _code(bad) == "INVALID_SMS",
          "http=%s %s" % (status, _short(bad)))

    status, sms2, _ = _demo_sms(port)
    fresh_code = str(_dig(sms2, "code") or "")
    status, done, _ = _approve(port, sid, approval_id, method="sms", code=fresh_code)
    c.add("sec-sms-3", "重新获取短信码后可成功（作废只影响旧码）",
          status == 200 and _status(done) == "ok" and _executed_tools(done) == ["set_card_status"]
          and _card_status(app.db_path) == "frozen",
          "http=%s status=%r executed=%s card=%r" % (status, _status(done), _executed_tools(done), _card_status(app.db_path)))
    return {"session_id": sid, "approval_id": approval_id}


# ---------------------------------------------------------------- 场景：TOTP 兼容


def scenario_totp(app, c):
    port = app.port
    sid = _session_new(port)
    first, approval_id = _all_chat_approvals(port, sid, "把我的信用卡解冻")
    status, totp, _ = _demo_totp(port)
    totp_code = str(_dig(totp, "code") or "")
    status, done, _ = _approve(port, sid, approval_id, method=None, code=totp_code)
    c.add("sec-totp-1", "不带 method + /api/demo/totp 动态码 → 200 成功（旧调用兼容）",
          status == 200 and _status(done) == "ok" and _executed_tools(done) == ["set_card_status"],
          "http=%s status=%r executed=%s reply=%s" % (status, _status(done), _executed_tools(done), _reply(done)))
    c.add("sec-totp-2", "兼容链路下卡片状态落库为 active",
          _card_status(app.db_path) == "active", "card_002.status=%r" % _card_status(app.db_path))
    audit_payload = _audit(port, sid)
    granted = [e for e in _entries(audit_payload) if str(e.get("event_type") or "") == "approval_granted"
               and str(e.get("approval_id") or "") == approval_id]
    c.add("sec-totp-3", "TOTP 审批通过写入 approval_granted 审计事件",
          bool(granted), "granted=%d" % len(granted))
    return {"session_id": sid, "approval_id": approval_id}


# ---------------------------------------------------------------- 场景：会话锁定（300 秒）


LOCK_TEXT = "用储蓄卡在京东消费 25000 元"


def _run_blocks(port, sid, times=3, text=LOCK_TEXT):
    rounds = []
    for _ in range(times):
        status, payload, _ = _chat(port, sid, text)
        rounds.append((status, payload))
    return rounds


def scenario_lock(app, c):
    port = app.port
    sid = _session_new(port)
    rounds = _run_blocks(port, sid, 3)
    r1, r2, r3 = rounds[0][1], rounds[1][1], rounds[2][1]
    c.add("sec-lock-1", "第 1 轮卡消费被安全规则拦下（status=blocked、code 非空、未执行工具）",
          _status(r1) == "blocked" and bool(_code(r1)) and _executed_tools(r1) == [],
          "status=%r code=%r executed=%s reply=%s" % (_status(r1), _code(r1), _executed_tools(r1), _reply(r1)))
    c.add("sec-lock-2", "第 2 轮同样被拦（累计 2 次，未锁定）",
          _status(r2) == "blocked" and not _dig(r2, "security", "just_locked")
          and _dig(r2, "security", "count") == 2,
          "status=%r security=%s" % (_status(r2), _short(_dig(r2, "security"))))
    c.add("sec-lock-3", "第 3 轮被拦且触发锁定：reply 追加「已临时锁定」，security.just_locked=true",
          _status(r3) == "blocked" and _contains(_reply(r3), "已临时锁定")
          and bool(_dig(r3, "security", "just_locked")) and _dig(r3, "security", "count") == 3,
          "status=%r security=%s reply=%s" % (_status(r3), _short(_dig(r3, "security")), _reply(r3)))
    c.add("sec-lock-4", "达到阈值的那一轮本身仍是原状态 blocked（锁从下一轮生效）",
          _status(r3) == "blocked" and _status(r3) != "locked",
          "status=%r" % _status(r3))

    status, locked, _ = _chat(port, sid, "查一下我的余额")
    remaining = int(_dig(locked, "lock_remaining_s", default=0) or 0)
    locked_until = str(_dig(locked, "locked_until") or "")
    c.add("sec-lock-5", "第 4 轮 status=locked / code=SESSION_LOCKED / reply 含锁语文案",
          status == 200 and _status(locked) == "locked" and _code(locked) == "SESSION_LOCKED"
          and _contains(_reply(locked), "会话因连续可疑指令已被临时锁定")
          and _contains(_reply(locked), "不会执行任何资金动作"),
          "http=%s status=%r code=%r reply=%s" % (status, _status(locked), _code(locked), _reply(locked)))
    try:
        until = datetime.fromisoformat(locked_until)
        seconds = int((until - _now()).total_seconds())
    except (ValueError, TypeError):
        seconds = -999
    c.add("sec-lock-6", "lock_remaining_s 在 1..300；locked_until 约为 now+300 秒",
          1 <= remaining <= 300 and 240 <= seconds <= 310,
          "remaining_s=%r until=%r delta_s=%r" % (remaining, locked_until, seconds))
    c.add("sec-lock-7", "锁定期间不解析意图、不建计划、不执行任何工具",
          _dig(locked, "intent") is None and _plan_tools(locked) == [] and _executed_tools(locked) == []
          and not _dig(locked, "plan", "steps"),
          "intent=%s plan=%s executed=%s" % (_dig(locked, "intent"), _plan_tools(locked), _executed_tools(locked)))

    rows = _db(app.db_path, "SELECT session_id, user_id, locked_until, reason FROM session_locks WHERE session_id=?", (sid,))
    reason = str(rows[0].get("reason") or "") if rows else ""
    c.add("sec-lock-8", "session_locks 表落一行（reason 含「连续 3 次」与「临时锁定」）",
          bool(rows) and _contains(reason, "连续 3 次") and _contains(reason, "临时锁定"),
          "row=%s" % _short(rows))

    audit_payload = _audit(port, sid)
    types = _entry_types(audit_payload)
    c.add("sec-lock-9", "该会话审计含 security_locked（tool=locks / risk=high）与 turn_locked",
          "security_locked" in types and "turn_locked" in types
          and any(str(e.get("event_type")) == "security_locked" and str(e.get("tool") or "") == "locks"
                  and str(e.get("risk_level") or "") == "high" for e in _entries(audit_payload)),
          "types=%s" % sorted(set(types)))
    c.add("sec-lock-10", "锁定期间审计链 valid 且 turn_blocked 计满 3 次",
          bool(_dig(audit_payload, "chain", "valid")) and types.count("turn_blocked") == 3,
          "chain=%s turn_blocked=%d" % (_short(_dig(audit_payload, "chain")), types.count("turn_blocked")))
    return {"session_id": sid}


# ---------------------------------------------------------------- 场景：锁按会话隔离


def scenario_isolation(app, c):
    port = app.port
    sid = _session_new(port)
    status, payload, _ = _chat(port, sid, "查一下我的账户余额")
    c.add("sec-iso-1", "新会话不受影响（status=ok，能正常查询账户）",
          status == 200 and _status(payload) == "ok" and _contains(_reply(payload), "账户"),
          "http=%s status=%r reply=%s" % (status, _status(payload), _reply(payload)))
    return {"session_id": sid}


# ---------------------------------------------------------------- 场景：reset 清锁


def scenario_reset(app, c):
    port = app.port
    status, reset, _ = _post(port, "/api/admin/reset", {})
    rows = _db(app.db_path, "SELECT COUNT(*) AS n FROM session_locks")
    n = int(rows[0]["n"]) if rows else -1
    c.add("sec-reset-1", "POST /api/admin/reset 后 session_locks 清空（0 行）",
          status == 200 and bool(_dig(reset, "reset")) and n == 0,
          "http=%s reset=%r session_locks=%d" % (status, _dig(reset, "reset"), n))

    sid = _session_new(port)
    status, payload, _ = _chat(port, sid, "查一下我的账户余额")
    c.add("sec-reset-2", "reset 后新会话可正常受理（无残留锁定态）",
          status == 200 and _status(payload) == "ok",
          "http=%s status=%r" % (status, _status(payload)))
    audit_payload = _audit(port)
    c.add("sec-reset-3", "reset 后审计链重建且 valid",
          bool(_dig(audit_payload, "chain", "valid")),
          "chain=%s entries=%d" % (_short(_dig(audit_payload, "chain")), len(_entries(audit_payload))))
    return {}


# ---------------------------------------------------------------- 场景：冷却后自动恢复（app2）


def scenario_recovery(app, c):
    port = app.port
    sid = _session_new(port)
    rounds = _run_blocks(port, sid, 3)
    r3 = rounds[2][1]
    c.add("sec-recov-1", "短冷却实例（SECURITY_LOCK_SECONDS=2）第 3 轮触发锁定",
          bool(_dig(r3, "security", "just_locked")) and _contains(_reply(r3), "已临时锁定"),
          "security=%s reply=%s" % (_short(_dig(r3, "security")), _reply(r3)))

    status, locked, _ = _chat(port, sid, "查一下我的余额")
    remaining = int(_dig(locked, "lock_remaining_s", default=0) or 0)
    c.add("sec-recov-2", "第 4 轮锁定，且剩余时长按 SECURITY_LOCK_SECONDS=2 生效（<=2 秒）",
          _status(locked) == "locked" and _code(locked) == "SESSION_LOCKED" and 1 <= remaining <= 2,
          "status=%r remaining=%r" % (_status(locked), remaining))

    time.sleep(2.6)
    status, after, _ = _chat(port, sid, "查一下我的账户余额")
    c.add("sec-recov-3", "冷却到期后同一会话自动恢复（status=ok，正常返回账户）",
          status == 200 and _status(after) == "ok" and _contains(_reply(after), "账户"),
          "http=%s status=%r reply=%s" % (status, _status(after), _reply(after)))

    audit_payload = _audit(port, sid)
    types = _entry_types(audit_payload)
    rows = _db(app.db_path, "SELECT locked_until FROM session_locks WHERE session_id=?", (sid,))
    expired = False
    if rows:
        try:
            expired = datetime.fromisoformat(str(rows[0]["locked_until"])) <= _now()
        except (ValueError, TypeError):
            expired = False
    c.add("sec-recov-4", "审计含 security_locked/turn_locked，且锁记录已到期（locked_until <= now）",
          "security_locked" in types and "turn_locked" in types and expired,
          "types=%s rows=%s expired=%s" % (sorted(set(types)), _short(rows), expired))
    return {"session_id": sid}


# ---------------------------------------------------------------- 收尾检查


def scenario_final(c, apps, ports, main_chain=None):
    for app in apps:
        if app is None:
            continue
        ok, path = app.no_traceback()
        c.add("sec-final-1-%s" % app.tag,
              "%s 服务日志无 Traceback（%s）" % (app.tag, app.log_paths[0].name if app.log_paths else ""),
              ok, path)

    for port in ports:
        residue = _port_listening(port) if port else False
        c.add("sec-final-2-%d" % port, "端口 %d 跑完无残留监听" % port, not residue, "listening=%s" % residue)

    c.add("sec-final-3", "主实例停止前最终审计链 valid（停止前快照）",
          bool(_dig(main_chain, "valid")), "chain=%s" % _short(main_chain))

    procs_down = all((app is None) or (app.proc is None) for app in apps)
    c.add("sec-final-4", "两个实例进程均已停止（proc=None）", procs_down,
          "procs_down=%s" % procs_down)


# ---------------------------------------------------------------- main


def _parse_only(argv):
    for i, arg in enumerate(argv):
        if arg == "--only" and i + 1 < len(argv):
            return {item.strip() for item in argv[i + 1].split(",") if item.strip()}
        if arg.startswith("--only="):
            return {item.strip() for item in arg.split("=", 1)[1].split(",") if item.strip()}
    return set()


def main():
    argv = sys.argv[1:]
    opts = {"only": _parse_only(argv), "json": "--json" in argv}
    checks = Checks(quiet=opts["json"])
    app1 = None
    app2 = None
    port1 = 0
    port2 = 0
    apps = []
    main_chain = None
    try:
        if not opts["json"]:
            print("=" * 78)
            print("ZeroStep v%s 安全因子 e2e（挂失红 / 三因子 / 会话锁定）" % (VERSION_FILE.read_text(encoding="utf-8").strip()
                                                            if VERSION_FILE.exists() else "0.4.1"))
            print("=" * 78)
        app1_dir = _fresh_app_dir("app")
        app2_dir = _fresh_app_dir("app2")
        port1 = _pick_port(PREFERRED_PORT)
        port2 = _pick_port(RECOVERY_PORT)
        if port2 == port1:
            port2 = _pick_port(port2 + 1)
        if not opts["json"]:
            print("主实例副本：%s（端口 %d）" % (app1_dir, port1))
            print("恢复副本  ：%s（端口 %d，SECURITY_LOCK_SECONDS=2）" % (app2_dir, port2))

        app1 = AppServer(port1, app1_dir, app1_dir / "backend" / "data" / "bank.db", "main")
        if not app1.start():
            print("[ENV] 主实例启动失败（端口 %d）" % port1)
            print(app1.logs_tail())
            return 2
        apps.append(app1)
        if not opts["json"]:
            print("主实例已就绪：http://%s:%d\n" % (HOST, port1))

        scenarios = [
            ("env", scenario_env),
            ("red", scenario_red),
            ("method", scenario_method),
            ("idor", scenario_idor),
            ("face", scenario_face),
            ("sms", scenario_sms),
            ("totp", scenario_totp),
            ("lock", scenario_lock),
            ("isolation", scenario_isolation),
            ("reset", scenario_reset),
        ]
        for name, func in scenarios:
            try:
                func(app1, checks)
            except Exception as exc:  # noqa: BLE001 - 单场景异常不中断其它场景
                checks.add("sec-run-" + name, "场景 %s 执行异常" % name, False,
                           "%s: %s | %s" % (type(exc).__name__, exc,
                                            traceback.format_exc(limit=2).replace("\n", " ")[:400]))
        main_chain = _dig(_audit(port1), "chain")
        app1.stop()

        app2 = AppServer(port2, app2_dir, app2_dir / "backend" / "data" / "bank.db", "recovery",
                         extra_env={"SECURITY_LOCK_SECONDS": "2"})
        if not app2.start():
            print("[ENV] 恢复实例启动失败（端口 %d）" % port2)
            print(app2.logs_tail())
            return 2
        apps.append(app2)
        if not opts["json"]:
            print("\n恢复实例已就绪：http://%s:%d（锁 2 秒）\n" % (HOST, port2))
        try:
            scenario_recovery(app2, checks)
        except Exception as exc:  # noqa: BLE001
            checks.add("sec-run-recovery", "场景 recovery 执行异常", False, "%s: %s" % (type(exc).__name__, exc))
        app2.stop()
    except Exception as exc:  # noqa: BLE001 - 环境错误统一退出码 2
        print("[ENV] %s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        return 2
    finally:
        for app in (app1, app2):
            try:
                if app is not None:
                    app.stop()
            except Exception:  # noqa: BLE001
                pass

    scenario_final(checks, apps, [port1, port2], main_chain)

    passed, total, failed = checks.result()
    if opts["only"]:
        selected = [it for it in checks.items if it["id"] in opts["only"]]
        passed = sum(1 for it in selected if it["ok"])
        total = len(selected)
        failed = [it for it in selected if not it["ok"]]
    if opts["json"]:
        print(json.dumps({"passed": passed, "total": total, "failed": [it["id"] for it in failed],
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
