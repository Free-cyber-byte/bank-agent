#!/usr/bin/env python3
"""ZeroStep v0.3.0 插件注册表与 /api/tasks 分页端到端测试（零依赖，仅标准库）。

覆盖：
- GET /api/plugins 注册表契约（7 插件、5 大类、字段齐全、core 锁定、版本号来自 VERSION 文件）；
- 错误码：core 锁定 403 PLUGIN_CORE_LOCKED、未知插件 404 PLUGIN_NOT_FOUND、非法 body 400 INVALID_BODY；
- mcp-fx 停用：/api/mcp/status 展示 disabled；对话走内置参考价且不 500；response.mcp_calls 不追加
  （v0.2.0 既有行为）；重启服务后停用状态仍持久化；
- 复原后 /api/mcp/status 不再 disabled；
- skill-pack 停用 -> /api/skills count=0/disabled=true，复原 -> 6 张；
- channel-im 停用 -> /api/channel/status enabled=false，复原 -> true；
- intent-llm 停用 -> /api/llm/status effective=rule 且 plugin_disabled=true，复原；
- mcp-risk 停用 -> 转账审批通过后 response.mcp_calls 追加 risk_screen/source=disabled/ok=false，复原；
- /api/audit 哈希链 valid 且存在 plugin_toggled/plugins.toggle 事件；
- /api/tasks 分页：limit=6/offset 0/6/30/末页/999、无参数全量（条数动态取 tasks/ 目录，不写死）；
- 收尾全量复原、独立端口无残留监听、服务日志无 traceback。

隔离策略：照 channel_e2e.py 的做法，把 backend/、frontend/、skills/、tasks/、VERSION 复制到
.eval_tmp/plugin_e2e/app 后启动服务，环境变量指定独立 PORT 与 PLUGINS_FILE（副本内
backend/data/plugins.json），不碰仓库 backend/data/bank.db 与 plugins.json。

端口卫生：优先 8023，被占用自动换空闲端口；起服务前用 netstat -ano 确认端口无 LISTENING
（Windows SO_REUSEADDR 可能多进程同端口 LISTEN，请求会随机落到旧进程造成假失败），
收尾再次确认无残留监听。

用法：
    python scripts/plugin_e2e.py
    python scripts/plugin_e2e.py -v     # 失败时打印服务日志尾部

退出码：0 全部通过；1 有失败；2 环境不可用。
"""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
WORK = os.path.join(ROOT, ".eval_tmp", "plugin_e2e")
APP = os.path.join(WORK, "app")
LOGS = os.path.join(WORK, "logs")
LOG_MAIN = os.path.join(LOGS, "main_server.log")
PLUGINS_FILE = os.path.join(APP, "backend", "data", "plugins.json")
DB_PATH = os.path.join(APP, "backend", "data", "bank.db")

PREFERRED_PORT = 8023
CHECKS = []
_PROC = None
_LOG = None


# ---------------- 端口与进程 ----------------
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


def _listening_pids(port):
    """用 netstat -ano 找出目标端口上的 LISTENING 进程 PID（取不到返回 []）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             errors="replace", timeout=10).stdout
    except Exception:  # noqa: BLE001 - netstat 缺失时不阻塞用例
        return []
    pids = set()
    needle = ":%d " % int(port)
    for line in out.splitlines():
        if needle not in line:
            continue
        if "LISTEN" not in line.upper():
            continue
        parts = line.split()
        if parts:
            pids.add(parts[-1])
    return sorted(pids)


def _wait_port_closed(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.2)
    return not _port_in_use(port)


def _server_env(port):
    env = dict(os.environ)
    env.update({
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "DEMO_MODE": "1",
        "ZEROSTEP_DISABLE_LLM": "1",
        "MCP_ENABLED": "0",
        "PLUGINS_FILE": PLUGINS_FILE,
        "CHANNEL_ENABLED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _start_server(port):
    global _PROC, _LOG
    _LOG = open(LOG_MAIN, "ab")
    _PROC = subprocess.Popen([PY, os.path.join("backend", "server.py")], cwd=APP,
                             env=_server_env(port), stdin=subprocess.DEVNULL,
                             stdout=_LOG, stderr=subprocess.STDOUT)
    try:
        _wait_http("http://127.0.0.1:%d/api/health" % port,
                   expect=lambda b: b.get("status") == "ok", timeout=30.0)
    except Exception:
        _stop_server()
        raise


def _stop_server():
    global _PROC, _LOG
    proc, log = _PROC, _LOG
    _PROC, _LOG = None, None
    if proc is None:
        return
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
    finally:
        if log is not None:
            try:
                log.close()
            except Exception:  # noqa: BLE001
                pass


def _tail(path, lines=30):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return "".join(fp.readlines()[-lines:])
    except OSError:
        return "(无日志)"


# ---------------- HTTP ----------------
def _request(method, port, path, payload=None, raw=None, timeout=20.0):
    """发 HTTP 请求，4xx/5xx 也返回 (status, body)，不抛 HTTPError。"""
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if method == "POST":
        if raw is not None:
            data = raw.encode("utf-8")
        else:
            data = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace") or "{}"
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"raw": text}
        return exc.code, body


def _get(port, path, timeout=20.0):
    return _request("GET", port, path, timeout=timeout)


def _post(port, path, payload=None, raw=None, timeout=20.0):
    return _request("POST", port, path, payload=payload, raw=raw, timeout=timeout)


def _wait_http(url, timeout=30.0, expect=None):
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


def _err_code(body):
    """兼容错误体 {"error":{"code":..}} 与 {"code":..} 两种形状。"""
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("code")
    return body.get("code")


# ---------------- 隔离副本 ----------------
def _prepare_app():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(LOGS, exist_ok=True)

    def ignore(_dir, names):
        return [n for n in names if n in ("__pycache__", "data", "messages.jsonl", "sessions.json",
                                          "plugins.json", "bank.db", "bank.db-wal", "bank.db-shm")]

    shutil.copytree(os.path.join(ROOT, "backend"), os.path.join(APP, "backend"), ignore=ignore)
    for name in ("frontend", "skills", "tasks"):
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(APP, name), ignore=ignore)
    # 版本纪律：config.VERSION 读仓库根 VERSION 文件，隔离副本必须一并复制，
    # 断言里也动态读该文件，不写死 0.2.0 / 0.3.0。
    version_src = os.path.join(ROOT, "VERSION")
    if os.path.exists(version_src):
        shutil.copy2(version_src, os.path.join(APP, "VERSION"))
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _version():
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8-sig") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def _expected_task_ids():
    """隔离副本里真实存在的用例 id（按文件名排序），供 /api/tasks 分页断言动态取数。

    v0.4.0 起用例从 36 条增加到 43 条，断言不写死数量，避免新增用例就误红。
    """
    tdir = os.path.join(APP, "tasks")
    try:
        names = sorted(n for n in os.listdir(tdir) if n.startswith("t") and n.endswith(".json"))
    except OSError:
        return []
    return [n[:-5] for n in names]


# ---------------- 审计库直读（哈希链只存 hashes，用于确认事件存在） ----------------
def _db_audit_rows(limit=200):
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT seq, event_type, tool FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# ---------------- 断言与输出 ----------------
def _check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": detail})
    print("%s  %s  %-58s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


def _run_check(cid, desc, fn):
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - 单条失败不影响后续断言
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
    _check(cid, desc, ok, detail)
    return ok


def _plugin(body, plugin_id):
    for item in body.get("plugins") or []:
        if isinstance(item, dict) and item.get("id") == plugin_id:
            return item
    return None


def _toggle(port, plugin_id, enabled):
    return _post(port, "/api/plugins/%s/toggle" % plugin_id, {"enabled": bool(enabled)})


def _restore_all(port):
    """兜底复原：把所有可停用插件恢复启用（幂等）。"""
    for pid in ("mcp-fx", "mcp-risk", "intent-llm", "skill-pack", "channel-im"):
        try:
            _toggle(port, pid, True)
        except Exception:  # noqa: BLE001
            pass


# ---------------- 各项断言 ----------------
def check_plugins_registry(port):
    st, body = _get(port, "/api/plugins")
    plugins = body.get("plugins") or []
    kinds = {str(k.get("id")) for k in body.get("kinds") or [] if isinstance(k, dict)}
    fields = ("id", "name", "kind", "version", "status", "enabled", "toggleable", "provides", "requires", "detail")
    fields_ok = all(all(f in p for f in fields) for p in plugins)
    provides_ok = all(all(k in (p.get("provides") or {}) for k in ("tools", "skills", "routes")) for p in plugins)
    core_ok = all(p.get("toggleable") is False for p in plugins if p.get("kind") == "core")
    version = _version()
    version_ok = bool(version) and all(str(p.get("version")) == version for p in plugins)
    ids = {str(p.get("id")) for p in plugins}
    ok = (st == 200 and body.get("ok") is True and body.get("count") == 7 and len(plugins) == 7
          and {"core", "mcp", "model", "skill", "channel"} <= kinds
          and fields_ok and provides_ok and core_ok and version_ok
          and body.get("enabled_count") == 7 and ids == {"policy", "audit", "mcp-fx", "mcp-risk",
                                                          "intent-llm", "skill-pack", "channel-im"})
    return ok, ("http=%s count=%s enabled=%s kinds=%s version=%s fields=%s core=%s"
                % (st, body.get("count"), body.get("enabled_count"), sorted(kinds), version or "?",
                   fields_ok, core_ok))


def check_tasks_page_first(port):
    expected = _expected_task_ids()
    st, body = _get(port, "/api/tasks?limit=6&offset=0")
    tasks = body.get("tasks") or []
    ids = [t.get("id") for t in tasks if isinstance(t, dict)]
    ok = (st == 200 and len(tasks) == 6 and expected and body.get("total") == len(expected)
          and body.get("offset") == 0
          and body.get("limit") == 6 and body.get("has_more") is True
          and bool(ids) and ids[0] == expected[0])
    return ok, "http=%s len=%s total=%s(expect=%s) first=%s(expect=%s) has_more=%s" % (
        st, len(tasks), body.get("total"), len(expected), ids[0] if ids else None,
        expected[0] if expected else None, body.get("has_more"))


def check_tasks_page_rest(port):
    expected = _expected_task_ids()
    total = len(expected)
    st2, b2 = _get(port, "/api/tasks?limit=6&offset=6")
    st3, b3 = _get(port, "/api/tasks?limit=6&offset=30")
    last_off = max(0, total - 6)
    st5, b5 = _get(port, "/api/tasks?limit=6&offset=%d" % last_off)
    st4, b4 = _get(port, "/api/tasks?limit=6&offset=999")
    first2 = ((b2.get("tasks") or [{}])[0] or {}).get("id")
    first3 = ((b3.get("tasks") or [{}])[0] or {}).get("id")
    first5 = ((b5.get("tasks") or [{}])[0] or {}).get("id")
    exp2 = expected[6] if total > 6 else None
    exp3 = expected[30] if total > 30 else None
    exp5 = expected[last_off] if expected else None
    ok = (st2 == 200 and first2 == exp2 and b2.get("has_more") is True
          and st3 == 200 and first3 == exp3 and b3.get("has_more") is (30 + 6 < total)
          and st5 == 200 and first5 == exp5 and b5.get("has_more") is False
          and st4 == 200 and (b4.get("tasks") or []) == [])
    return ok, ("offset6=%s(expect=%s,more=%s) offset30=%s(expect=%s,more=%s) "
                "offset%d=%s(more=%s) offset999=%s" % (
                    first2, exp2, b2.get("has_more"), first3, exp3, b3.get("has_more"),
                    last_off, first5, b5.get("has_more"), b4.get("tasks")))


def check_tasks_legacy(port):
    expected = _expected_task_ids()
    st, body = _get(port, "/api/tasks")
    tasks = body.get("tasks") or []
    ok = st == 200 and bool(expected) and len(tasks) == len(expected)
    return ok, "http=%s len=%s(expect=%s)" % (st, len(tasks), len(expected))


def check_error_codes(port):
    s403, b403 = _post(port, "/api/plugins/policy/toggle", {"enabled": False})
    s404, b404 = _post(port, "/api/plugins/not-exist/toggle", {"enabled": False})
    s400a, b400a = _post(port, "/api/plugins/mcp-fx/toggle", {})
    s400b, b400b = _post(port, "/api/plugins/mcp-fx/toggle", raw="not-json")
    s400c, b400c = _post(port, "/api/plugins/mcp-fx/toggle", {"enabled": "false"})
    ok = (s403 == 403 and _err_code(b403) == "PLUGIN_CORE_LOCKED"
          and s404 == 404 and _err_code(b404) == "PLUGIN_NOT_FOUND"
          and s400a == 400 and _err_code(b400a) == "INVALID_BODY"
          and s400b == 400 and _err_code(b400b) == "INVALID_BODY"
          and s400c == 400 and _err_code(b400c) == "INVALID_BODY")
    return ok, "403=%s/%s 404=%s/%s 400=%s/%s/%s" % (
        s403, _err_code(b403), s404, _err_code(b404),
        _err_code(b400a), _err_code(b400b), _err_code(b400c))


def check_disable_fx(port):
    st, body = _post(port, "/api/plugins/mcp-fx/toggle", {"enabled": False})
    fx = body.get("plugin") or {}
    counts = body.get("counts") or {}
    ok = (st == 200 and body.get("ok") is True and fx.get("enabled") is False
          and fx.get("status") == "disabled" and counts.get("total") == 7 and counts.get("disabled") == 1)
    return ok, "http=%s enabled=%s status=%s counts=%s" % (
        st, fx.get("enabled"), fx.get("status"), counts)


def check_mcp_status_disabled(port):
    st, body = _get(port, "/api/mcp/status")
    fx = None
    for row in body.get("servers") or []:
        if isinstance(row, dict) and row.get("name") == "fx":
            fx = row
    ok = (st == 200 and body.get("ok") is True and isinstance(fx, dict)
          and fx.get("status") == "disabled" and fx.get("ok") is False
          and fx.get("degraded") is True and fx.get("last_error") == "插件已停用")
    return ok, "http=%s top_ok=%s fx=%s" % (
        st, body.get("ok"), {k: (fx or {}).get(k) for k in ("status", "ok", "degraded", "last_error")})


def check_fx_chat_disabled(port):
    st, snap = _post(port, "/api/session", {})
    sid = snap.get("session_id") or ""
    st2, body = _post(port, "/api/chat", {"session_id": sid, "text": "查一下美元兑人民币汇率"})
    reply = str(body.get("reply") or "")
    calls = body.get("mcp_calls") or []
    no_fx_call = not any(isinstance(c, dict) and c.get("tool") == "fx_convert" for c in calls)
    audit_hit = any(r.get("event_type") == "mcp_call" and r.get("tool") == "fx_convert"
                    for r in _db_audit_rows())
    ok = (st2 == 200 and body.get("status") == "ok" and reply.startswith("汇率插件已停用")
          and "内置参考价" in reply and no_fx_call and audit_hit)
    return ok, "http=%s status=%s reply=%s mcp_calls_has_fx=%s audit_mcp_call=%s" % (
        st2, body.get("status"), reply[:56].replace("\n", " "), not no_fx_call, audit_hit)


def check_restart_persistence(port):
    _stop_server()
    closed = _wait_port_closed(port, timeout=10.0)
    _start_server(port)
    st, body = _get(port, "/api/plugins")
    fx = _plugin(body, "mcp-fx") or {}
    state = {}
    try:
        with open(PLUGINS_FILE, "r", encoding="utf-8-sig") as fp:
            state = json.load(fp)
    except (OSError, ValueError):
        state = {}
    ok = (closed and st == 200 and fx.get("enabled") is False and fx.get("status") == "disabled"
          and state.get("mcp-fx") is False)
    return ok, "stopped_ok=%s http=%s enabled=%s status=%s file=%s" % (
        closed, st, fx.get("enabled"), fx.get("status"), state)


def check_restore_fx(port):
    st, body = _toggle(port, "mcp-fx", True)
    fx = body.get("plugin") or {}
    st2, mcp = _get(port, "/api/mcp/status")
    row = None
    for item in mcp.get("servers") or []:
        if isinstance(item, dict) and item.get("name") == "fx":
            row = item
    ok = (st == 200 and fx.get("enabled") is True and fx.get("status") == "enabled"
          and st2 == 200 and isinstance(row, dict) and row.get("status") != "disabled")
    return ok, "http=%s enabled=%s status=%s mcp_fx_status=%s" % (
        st, fx.get("enabled"), fx.get("status"), (row or {}).get("status"))


def check_skill_pack(port):
    st, body = _toggle(port, "skill-pack", False)
    off = body.get("plugin") or {}
    st2, sk = _get(port, "/api/skills")
    st3, back = _toggle(port, "skill-pack", True)
    on = back.get("plugin") or {}
    st4, sk2 = _get(port, "/api/skills")
    ok = (st == 200 and off.get("enabled") is False and st2 == 200
          and sk.get("count") == 0 and sk.get("skills") == [] and sk.get("disabled") is True
          and st3 == 200 and on.get("enabled") is True
          and sk2.get("count") == 6 and len(sk2.get("skills") or []) == 6)
    return ok, "off=%s count=%s disabled=%s restored_count=%s" % (
        off.get("enabled"), sk.get("count"), sk.get("disabled"), sk2.get("count"))


def check_channel_im(port):
    st, body = _toggle(port, "channel-im", False)
    off = body.get("plugin") or {}
    st2, cs = _get(port, "/api/channel/status")
    st3, back = _toggle(port, "channel-im", True)
    on = back.get("plugin") or {}
    st4, cs2 = _get(port, "/api/channel/status")
    ok = (st == 200 and off.get("enabled") is False and st2 == 200 and cs.get("enabled") is False
          and st3 == 200 and on.get("enabled") is True and st4 == 200 and cs2.get("enabled") is True)
    return ok, "off_enabled=%s status=%s restored=%s" % (
        cs.get("enabled"), off.get("status"), cs2.get("enabled"))


def check_intent_llm(port):
    st, body = _toggle(port, "intent-llm", False)
    off = body.get("plugin") or {}
    st2, llm = _get(port, "/api/llm/status")
    st3, back = _toggle(port, "intent-llm", True)
    on = back.get("plugin") or {}
    st4, llm2 = _get(port, "/api/llm/status")
    ok = (st == 200 and off.get("enabled") is False and st2 == 200
          and llm.get("effective") == "rule" and llm.get("plugin_disabled") is True
          and st3 == 200 and on.get("enabled") is True
          and st4 == 200 and not llm2.get("plugin_disabled"))
    return ok, "off=%s effective=%s plugin_disabled=%s restored_disabled=%s" % (
        off.get("status"), llm.get("effective"), llm.get("plugin_disabled"), llm2.get("plugin_disabled"))


def check_risk_disabled_transfer(port):
    st0, off = _toggle(port, "mcp-risk", False)
    try:
        st1, snap = _post(port, "/api/session", {})
        sid = snap.get("session_id") or ""
        st2, first = _post(port, "/api/chat", {"session_id": sid, "text": "给妈妈转 800 元"})
        approval = first.get("approval") or {}
        aid = approval.get("approval_id") or ""
        st3, totp = _get(port, "/api/demo/totp?user_id=u001")
        st4, done = _post(port, "/api/approval", {"session_id": sid, "approval_id": aid,
                                                  "decision": "approve", "code": totp.get("code") or ""})
        calls = [c for c in (done.get("mcp_calls") or []) if isinstance(c, dict)]
        risk = [c for c in calls if c.get("tool") == "risk_screen"]
        disabled_call = bool(risk) and risk[0].get("source") == "disabled" and risk[0].get("ok") is False
        reply = str(done.get("reply") or "")
        note = "插件已停用" in reply and "反欺诈" in reply
        ok = ((off.get("plugin") or {}).get("enabled") is False and st1 == 200 and st2 == 200
              and first.get("status") == "awaiting_approval" and bool(aid)
              and st4 == 200 and done.get("status") == "ok" and disabled_call and note)
        return ok, "mcp_calls=%s reply_has_note=%s first=%s done=%s" % (
            risk[:1], note, first.get("status"), done.get("status"))
    finally:
        _toggle(port, "mcp-risk", True)


def check_audit_chain(port):
    st, body = _get(port, "/api/audit?limit=100")
    chain = body.get("chain") or {}
    entries = body.get("entries") or []
    toggles = [e for e in entries if isinstance(e, dict) and e.get("event_type") == "plugin_toggled"]
    tools_ok = bool(toggles) and all(e.get("tool") == "plugins.toggle" for e in toggles)
    hashes_only = not any(("args" in e or "result" in e) for e in entries if isinstance(e, dict))
    ok = (st == 200 and chain.get("valid") is True and int(chain.get("checked") or 0) > 0
          and len(toggles) >= 2 and tools_ok and hashes_only)
    return ok, "http=%s valid=%s checked=%s toggles=%s tool_ok=%s hashes_only=%s" % (
        st, chain.get("valid"), chain.get("checked"), len(toggles), tools_ok, hashes_only)


def check_final_restored(port):
    st, body = _get(port, "/api/plugins")
    plugins = [p for p in (body.get("plugins") or []) if isinstance(p, dict)]
    all_on = bool(plugins) and all(p.get("enabled") is True for p in plugins)
    st2, sk = _get(port, "/api/skills")
    st3, cs = _get(port, "/api/channel/status")
    st4, llm = _get(port, "/api/llm/status")
    ok = (st == 200 and all_on and body.get("enabled_count") == 7
          and st2 == 200 and sk.get("count") == 6
          and st3 == 200 and cs.get("enabled") is True
          and st4 == 200 and not llm.get("plugin_disabled"))
    return ok, "all_enabled=%s enabled_count=%s skills=%s channel=%s llm_disabled=%s" % (
        all_on, body.get("enabled_count"), sk.get("count"), cs.get("enabled"),
        llm.get("plugin_disabled"))


def check_port_hygiene(port, before_pids):
    after = _listening_pids(port)
    log_text = ""
    try:
        with open(LOG_MAIN, "r", encoding="utf-8", errors="replace") as fp:
            log_text = fp.read()
    except OSError:
        log_text = ""
    no_traceback = "Traceback (most recent call last)" not in log_text
    ok = before_pids == [] and after == [] and no_traceback
    return ok, "before=%s after=%s no_traceback=%s" % (before_pids, after, no_traceback)


# ---------------- 主流程 ----------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 插件注册表与分页 E2E 测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印服务日志尾部")
    args = parser.parse_args(argv)

    preferred = int(os.environ.get("PLUGIN_E2E_PORT", str(PREFERRED_PORT)))
    port = _pick_port(preferred)
    before_pids = _listening_pids(port)
    print("ZeroStep v%s 插件 E2E（端口 %d，VERSION=%s）" % (_version() or "?", port, _version() or "?"))
    print("-" * 90)
    if before_pids:
        print("端口 %d 已被 PID %s 监听，为避免 Windows SO_REUSEADDR 双 LISTEN 假失败，改用其它端口重跑。"
              % (port, ",".join(before_pids)))
        return 2

    try:
        _prepare_app()
        if not os.path.exists(os.path.join(APP, "backend", "server.py")):
            print("未找到 backend/server.py，产品尚未交付。")
            return 2
        if not os.path.exists(os.path.join(APP, "backend", "app", "plugins.py")):
            print("未找到 backend/app/plugins.py，插件注册表尚未交付。")
            return 2
        _start_server(port)

        _run_check("plg-1", "/api/plugins 7 插件/5 大类/字段齐全/core 锁定/版本取 VERSION",
                   lambda: check_plugins_registry(port))
        _run_check("plg-2", "/api/tasks?limit=6&offset=0 首页 6 条、total=用例数（动态）、首项 t01、has_more",
                   lambda: check_tasks_page_first(port))
        _run_check("plg-3", "/api/tasks 分页 offset=6→t07、offset=30→t31、末页 has_more=false、offset=999→空 200",
                   lambda: check_tasks_page_rest(port))
        _run_check("plg-4", "/api/tasks 无参数返回全量（条数=用例文件数，动态）",
                   lambda: check_tasks_legacy(port))
        _run_check("plg-5", "插件 toggle 错误码 403 PLUGIN_CORE_LOCKED / 404 PLUGIN_NOT_FOUND / 400 INVALID_BODY",
                   lambda: check_error_codes(port))
        _run_check("plg-6", "停用 mcp-fx：200、plugin.status=disabled、counts 更新",
                   lambda: check_disable_fx(port))
        _run_check("plg-7", "停用后 /api/mcp/status：fx 项 status=disabled、顶层 ok=true",
                   lambda: check_mcp_status_disabled(port))
        _run_check("plg-8", "停用后汇率对话不 500：回复以「汇率插件已停用」开头且含内置参考价、审计有 mcp_call",
                   lambda: check_fx_chat_disabled(port))
        _run_check("plg-9", "重启服务后 mcp-fx 停用状态持久化（plugins.json）",
                   lambda: check_restart_persistence(port))
        _run_check("plg-10", "复原 mcp-fx：/api/mcp/status 不再 disabled",
                   lambda: check_restore_fx(port))
        _run_check("plg-11", "skill-pack 停用→/api/skills count=0/disabled=true，复原→6 张",
                   lambda: check_skill_pack(port))
        _run_check("plg-12", "channel-im 停用→/api/channel/status enabled=false，复原→true",
                   lambda: check_channel_im(port))
        _run_check("plg-13", "intent-llm 停用→/api/llm/status effective=rule/plugin_disabled=true，复原",
                   lambda: check_intent_llm(port))
        _run_check("plg-14", "mcp-risk 停用→转账审批通过后 mcp_calls 追加 risk_screen/source=disabled",
                   lambda: check_risk_disabled_transfer(port))
        _run_check("plg-15", "/api/audit 哈希链 valid 且含 plugin_toggled/plugins.toggle 事件",
                   lambda: check_audit_chain(port))
        _run_check("plg-16", "收尾全量复原：7 插件全部 enabled、skills=6、channel 可用",
                   lambda: check_final_restored(port))

        # 端口卫生：先停服务再确认无残留监听与无 traceback
        _stop_server()
        _wait_port_closed(port, timeout=10.0)
        _run_check("plg-17", "独立端口收尾无残留 LISTENING、服务日志无 traceback",
                   lambda: check_port_hygiene(port, before_pids))

    except Exception as exc:  # noqa: BLE001
        print("运行异常：%s: %s" % (type(exc).__name__, exc))
        if args.verbose:
            import traceback
            traceback.print_exc()
    finally:
        _restore_all(port)
        _stop_server()

    return _finish(args)


def _finish(args):
    passed = sum(1 for c in CHECKS if c["ok"])
    total = len(CHECKS)
    print("-" * 90)
    print("结果：%d/%d 项通过" % (passed, total))
    if total == 0:
        return 2
    if passed != total:
        if args.verbose:
            for c in CHECKS:
                if not c["ok"]:
                    print("\n[FAIL %s] %s" % (c["id"], c["detail"]))
            print("\n--- main_server.log 尾部 ---\n" + _tail(LOG_MAIN))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
