#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ZeroStep v0.5.0 流式回答 E2E：真 SSE 跑完整回答 + 中途取消。

用独立端口、独立副本启动 backend/server.py（默认 8010，被占用则自动换端口），
通过真实 HTTP 连接验证：

  full-1  GET /api/health 版本与仓库 VERSION 一致
  full-2  完整流 SSE 200 + Content-Type: text/event-stream
  full-3  事件序 session→intent→plan→tool_start→tool_end→answer_start→
          answer_delta×n→answer_end→insight_card→done
  full-4  answer_start.chars / answer_end.chunks / delta 序号与块长一致，cancelled=false
  full-5  拼接 delta 与非流式 /api/chat 的 reply 逐字节相等（末尾无多余字符）
  full-6  stream_text=false：只发 message 事件，message.delta == 完整回答
  canc-1  不存在的 thread_id 取消 → 200 {cancelled:false, reason:not_streaming}
  canc-2  answer_start 且 ≥2 个 delta 后取消 → 200 {cancelled:true, emitted_chunks}
  canc-3  answer_end.cancelled=true；chunks==delta 数==emitted_chunks<总块数
  canc-4  note 含「已停止输出」，仍有 done、无 error，已发 delta 是完整回答前缀
  canc-5  已执行工具不回滚：tool_end 有 spending_insights、insight_card 仍在
  canc-6  审计恰 1 条 answer_cancelled（tool=stream）且链 valid
  clean-1 进程清理，端口无残留，日志无 Traceback

用法：
    python scripts/e2e_stream.py
    python scripts/e2e_stream.py --json --verbose
退出码：0 全过 / 1 有失败 / 2 环境错误（副本构建或进程启动失败）。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
TMP_ROOT = ROOT / ".eval_tmp"
WORK = TMP_ROOT / "e2e_stream"
APP_DIR = WORK / "app"
LOG_DIR = WORK / "logs"
HOST = "127.0.0.1"
PREFERRED_PORT = 8010
PYTHON = sys.executable

# 流式验证用的一句话；规则引擎可确定性解析成 insight（只读工具，无审批）
PROMPT = "帮我看看这个月的消费洞察"
PROMPT_BILLS = "看看我这个月的账单"


class Checks(object):
    def __init__(self, verbose=False):
        self.items = []
        self.verbose = verbose

    def add(self, cid, desc, ok, detail=""):
        self.items.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": str(detail)})
        if self.verbose or not ok:
            print("[%s] %s %s%s" % ("PASS" if ok else "FAIL", cid, desc,
                                    ("" if ok else "  → " + str(detail))))
        return bool(ok)

    @property
    def failed(self):
        return [it for it in self.items if not it["ok"]]


def _log(msg):
    print(msg, flush=True)


def _repo_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _rmtree_retry(path, attempts=5):
    for i in range(attempts):
        try:
            if path.exists():
                shutil.rmtree(path)
            return True
        except OSError:
            time.sleep(0.4 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _prepare_app():
    """复制 backend/skills/VERSION 到隔离副本，避免污染仓库演示库。

    Windows 上偶发旧副本目录被占用导致 rmtree 失败：此时换 app_<时间戳>_<pid>
    目录完整复制，绝不复用半成品目录（否则子进程找不到 server.py）。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    # 顺手清理上次遗留的 app_* 副本（失败忽略）
    for stale in WORK.glob("app_*"):
        if stale.is_dir():
            _rmtree_retry(stale, attempts=2)
    target = APP_DIR
    if target.exists() and not _rmtree_retry(target):
        target = WORK / ("app_%s_%d" % (time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
        if target.exists():
            _rmtree_retry(target, attempts=2)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("backend", "skills"):
        src = ROOT / name
        if src.exists():
            shutil.copytree(src, target / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                          "data", "*.db", "*.db-wal",
                                                          "*.db-shm"))
    (target / "backend" / "data").mkdir(parents=True, exist_ok=True)
    if VERSION_FILE.exists():
        shutil.copy2(VERSION_FILE, target / "VERSION")
    return target


def _port_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_port(preferred):
    if _port_free(HOST, preferred):
        return preferred
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_http(port, path="/api/health", timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data, _ctype = _http_json(port, "GET", path, timeout=3)
            if status == 200:
                return data
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _http_json(port, method, path, payload=None, timeout=30):
    import http.client
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        data = {}
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                data = {"_raw": raw.decode("utf-8", "replace")}
        return resp.status, data, ctype
    finally:
        conn.close()


def _start_server(port, log_path, app_dir):
    env = {
        "PORT": str(port),
        "HOST": HOST,
        "DEMO_MODE": "1",
        "ZEROSTEP_DISABLE_LLM": "1",
        "MCP_ENABLED": "0",
        "CHANNEL_ENABLED": "0",
        "SCHEDULER_ENABLED": "0",
        "ZS_STREAM_DELAY_MS": "40",
        "PLUGINS_FILE": str(Path(app_dir) / "backend" / "data" / "plugins.json"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DEEPSEEK_", "ZS_")) and k not in (
                "PORT", "HOST", "DEMO_MODE", "ZEROSTEP_DISABLE_LLM", "MCP_ENABLED",
                "CHANNEL_ENABLED", "SCHEDULER_ENABLED", "PLUGINS_FILE")}
    base.update(env)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(str(log_path), "wb")
    proc = subprocess.Popen([PYTHON, str(Path(app_dir) / "backend" / "server.py")],
                            cwd=str(app_dir), env=base, stdout=log, stderr=subprocess.STDOUT)
    proc._qa_log = log
    return proc


def _stop_server(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass
    finally:
        log = getattr(proc, "_qa_log", None)
        if log is not None:
            try:
                log.close()
            except Exception:
                pass


def _port_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((HOST, port)) == 0
    finally:
        s.close()


# ---------------------------------------------------------------- SSE 解析


def _open_sse(port, payload, timeout=90):
    """POST /api/chat 建立 SSE 连接，返回 (conn, resp, status, ctype)。"""
    import http.client
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn.request("POST", "/api/chat", body=body,
                 headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    resp = conn.getresponse()
    return conn, resp, resp.status, resp.getheader("Content-Type") or ""


def _iter_sse(resp):
    """逐帧产出 (event, data_dict)。"""
    current_event = None
    data_lines = []
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            if current_event is not None or data_lines:
                raw = "\n".join(data_lines)
                try:
                    data = json.loads(raw) if raw else {}
                except ValueError:
                    data = {"_raw": raw}
                yield (current_event or "message", data)
            current_event = None
            data_lines = []
            continue
        if text.startswith(":"):
            continue
        if text.startswith("event:"):
            current_event = text[len("event:"):].strip()
        elif text.startswith("data:"):
            data_lines.append(text[len("data:"):].strip())
    if current_event is not None or data_lines:
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"_raw": raw}
        yield (current_event or "message", data)


def _collect_sse(port, payload, timeout=90):
    conn, resp, status, ctype = _open_sse(port, payload, timeout)
    events = []
    try:
        for event, data in _iter_sse(resp):
            events.append((event, data))
    finally:
        conn.close()
    return status, ctype, events


class _StreamReader(object):
    """后台线程读取 SSE，支持等待指定事件。"""

    def __init__(self, port, payload, timeout=90):
        self.port = port
        self.payload = payload
        self.timeout = timeout
        self.events = []
        self.status = None
        self.ctype = ""
        self.error = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        conn = None
        try:
            conn, resp, status, ctype = _open_sse(self.port, self.payload, self.timeout)
            self.status, self.ctype = status, ctype
            for event, data in _iter_sse(resp):
                with self._lock:
                    self.events.append((event, data))
        except Exception as exc:  # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def snapshot(self):
        with self._lock:
            return list(self.events)

    def wait_for(self, predicate, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self.snapshot()):
                return True
            if not self._thread.is_alive() and self.error:
                return False
            time.sleep(0.02)
        return False

    def join(self, timeout=60):
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _payload_types(events):
    return [name for name, _ in events]


def _first(events, name):
    for event, data in events:
        if event == name:
            return data
    return None


def _all(events, name):
    return [data for event, data in events if event == name]


def _order_ok(seq, expected):
    pos = 0
    for item in seq:
        if pos < len(expected) and item == expected[pos]:
            pos += 1
    return pos == len(expected)


def _entry_events(port, session_id, limit=200):
    status, data, _ = _http_json(port, "GET",
                                 "/api/audit?session_id=%s&limit=%d" % (session_id, limit))
    return status, data


def _verify(port, checks, prefix, session_id):
    try:
        status, data = _entry_events(port, session_id, 200)
        entries = data.get("entries") or []
        chain = data.get("chain") or {}
        checks.add(prefix + ".status", "GET /api/audit 200", status == 200, status)
        checks.add(prefix + ".valid", "审计链 valid=true 且 checked>0",
                   bool(chain.get("valid")) and int(chain.get("checked") or 0) > 0,
                   json.dumps(chain, ensure_ascii=False)[:160])
        return entries, chain
    except Exception as exc:  # noqa: BLE001
        checks.add(prefix + ".error", "读取 /api/audit 失败", False, exc)
        return [], {}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep v0.5.0 流式回答 e2e")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    parser.add_argument("--verbose", action="store_true", help="打印每条断言")
    parser.add_argument("--keep", action="store_true", help="保留临时副本与日志")
    args = parser.parse_args(argv)

    checks = Checks(verbose=args.verbose)
    version = _repo_version()
    started = time.time()
    proc = None
    session_id = ""
    port = PREFERRED_PORT
    result = {"ok": False, "port": port, "version": version, "checks": [], "error": ""}

    try:
        if not (ROOT / "backend" / "server.py").exists():
            raise RuntimeError("未找到 backend/server.py")
        app_dir = _prepare_app()
        port = _pick_port(PREFERRED_PORT)
        result["port"] = port
        _log("ZeroStep v%s 流式 e2e（主服务 %s:%d，副本 %s）"
             % (version or "?", HOST, port, app_dir))
        log_path = LOG_DIR / ("server_%d.log" % port)
        proc = _start_server(port, log_path, app_dir)
        health = _wait_http(port)
        if not health:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                pass
            raise RuntimeError("主服务 25s 内未就绪（poll=%s，日志 %s）：%s"
                               % (proc.poll(), log_path, tail.strip()))

        checks.add("env-1", "GET /api/health 版本 == 仓库 VERSION",
                   health.get("version") == version, "%s vs %s" % (health.get("version"), version))
        checks.add("env-2", "GET /api/health status == ok", health.get("status") == "ok",
                   health.get("status"))

        status, data, _ = _http_json(port, "POST", "/api/session", {})
        session_id = str((data or {}).get("session_id") or "")
        checks.add("env-3", "POST /api/session 返回 session_id",
                   status == 200 and bool(session_id), "%s %s" % (status, session_id))

        # ---------------- 阶段 1：完整流式回答 ----------------
        t0 = time.time()
        status, ctype, events = _collect_sse(port, {
            "session_id": session_id, "text": PROMPT, "stream": True})
        full_elapsed = time.time() - t0
        types = _payload_types(events)
        checks.add("full-1", "完整流 SSE 200 且 Content-Type 含 text/event-stream",
                   status == 200 and "text/event-stream" in ctype,
                   "%s %s" % (status, ctype))
        checks.add("full-2", "事件序 session→intent→plan→tool_start→tool_end→"
                             "answer_start→answer_end→done",
                   _order_ok(types, ["session", "intent", "plan", "tool_start", "tool_end",
                                     "answer_start", "answer_end", "done"]),
                   types)
        start = _first(events, "answer_start") or {}
        end = _first(events, "answer_end") or {}
        deltas = _all(events, "answer_delta")
        joined = "".join(str(d.get("text") or "") for d in deltas)
        checks.add("full-3", "answer_start 带 turn_id/thread_id 且 chars>0",
                   bool(start.get("turn_id")) and bool(start.get("thread_id"))
                   and int(start.get("chars") or 0) > 0,
                   json.dumps(start, ensure_ascii=False)[:160])
        checks.add("full-4", "delta 序号 i 从 0 连续，且除末块外均为 12 字符",
                   [int(d.get("i", -1)) for d in deltas] == list(range(len(deltas))) and
                   all(len(str(d.get("text") or "")) == 12 for d in deltas[:-1]),
                   [len(str(d.get("text") or "")) for d in deltas[:3]])
        checks.add("full-5", "answer_end.chunks == delta 数 == ceil(chars/12) 且 cancelled=false",
                   int(end.get("chunks") or -1) == len(deltas)
                   and int(end.get("chunks") or 0) == (int(start.get("chars") or 0) + 11) // 12
                   and end.get("cancelled") is False,
                   "chunks=%s deltas=%d cancelled=%s"
                   % (end.get("chunks"), len(deltas), end.get("cancelled")))
        checks.add("full-6", "intent == insight 且 insight_card 存在",
                   (_first(events, "intent") or {}).get("name") == "insight"
                   and isinstance(_first(events, "insight_card"), dict),
                   (_first(events, "intent") or {}).get("name"))
        done = _first(events, "done") or {}
        checks.add("full-7", "done.status == ok 且无 error 事件",
                   done.get("status") == "ok" and "error" not in types,
                   "done=%s error=%s" % (done.get("status"), "error" in types))

        # ---------------- 阶段 2：非流式对照 ----------------
        status, data, _ = _http_json(port, "POST", "/api/chat", {
            "session_id": session_id, "text": PROMPT})
        reply = str((data or {}).get("reply") or "")
        checks.add("plain-1", "非流式 /api/chat 200 且 reply 非空",
                   status == 200 and bool(reply), status)
        checks.add("plain-2", "拼接 delta == 非流式 reply（逐字节，末尾无多余字符）",
                   joined == reply and reply == reply.strip() and not reply.endswith("已停止输出"),
                   "joined=%d reply=%d 尾部=%r" % (len(joined), len(reply), reply[-6:]))
        checks.add("plain-3", "answer_start.chars == len(reply)",
                   int(start.get("chars") or 0) == len(reply),
                   "%s vs %s" % (start.get("chars"), len(reply)))

        # ---------------- 阶段 3：stream_text=false ----------------
        status, ctype, events2 = _collect_sse(port, {
            "session_id": session_id, "text": PROMPT_BILLS, "stream": True,
            "stream_text": False})
        types2 = _payload_types(events2)
        msgs = _all(events2, "message")
        checks.add("nostream-1", "stream_text=false：SSE 200 且无 answer_start/delta/end",
                   status == 200 and "answer_start" not in types2
                   and "answer_delta" not in types2 and "answer_end" not in types2, types2)
        checks.add("nostream-2", "stream_text=false：恰好 1 条 message 事件且 delta 非空",
                   len(msgs) == 1 and bool(str(msgs[0].get("delta") or "")),
                   [len(str(m.get("delta") or "")) for m in msgs])
        status2, data2, _ = _http_json(port, "POST", "/api/chat", {
            "session_id": session_id, "text": PROMPT_BILLS})
        reply2 = str((data2 or {}).get("reply") or "")
        checks.add("nostream-3", "stream_text=false 的 message.delta == 非流式 reply",
                   len(msgs) == 1 and str(msgs[0].get("delta") or "") == reply2,
                   "%d vs %d" % (len(str(msgs[0].get("delta") or "")) if msgs else 0, len(reply2)))

        # ---------------- 阶段 4：非流式阶段取消 → not_streaming ----------------
        status, data, _ = _http_json(port, "POST", "/api/turn/cancel",
                                     {"thread_id": "thread_not_streaming"})
        checks.add("canc-1", "不在流式阶段取消 → 200 {cancelled:false, reason:not_streaming}",
                   status == 200 and data.get("cancelled") is False
                   and data.get("reason") == "not_streaming",
                   "%s %s" % (status, json.dumps(data, ensure_ascii=False)[:120]))

        # ---------------- 阶段 5：流式阶段中途取消 ----------------
        total_before = None
        try:
            status, acc_before, _ = _http_json(port, "GET",
                                               "/api/accounts?session_id=%s" % session_id)
            total_before = int((acc_before or {}).get("total_cents"))
        except Exception:
            total_before = None

        reader = _StreamReader(port, {"session_id": session_id, "text": PROMPT,
                                      "stream": True})
        got = reader.wait_for(lambda evs: (
            any(e == "answer_start" for e, _ in evs)
            and sum(1 for e, _ in evs if e == "answer_delta") >= 2), timeout=30)
        checks.add("canc-2", "流式已开始（answer_start + ≥2 delta）", got,
                   _payload_types(reader.snapshot()))
        snap = reader.snapshot()
        start2 = _first(snap, "answer_start") or {}
        thread_id = str(start2.get("thread_id") or "")
        checks.add("canc-3", "answer_start.thread_id 非空（取消必须带 thread_id）",
                   bool(thread_id), thread_id)
        cancel_status, cancel_data, _ = _http_json(port, "POST", "/api/turn/cancel",
                                                   {"thread_id": thread_id})
        joined_ok = reader.join(60)
        events3 = reader.snapshot()
        types3 = _payload_types(events3)
        checks.add("canc-4", "取消返回 200 {cancelled:true, emitted_chunks>0}",
                   cancel_status == 200 and cancel_data.get("cancelled") is True
                   and int(cancel_data.get("emitted_chunks") or 0) > 0,
                   json.dumps(cancel_data, ensure_ascii=False)[:160])
        end3 = _first(events3, "answer_end") or {}
        deltas3 = _all(events3, "answer_delta")
        joined3 = "".join(str(d.get("text") or "") for d in deltas3)
        total_chunks = (int(start2.get("chars") or 0) + 11) // 12
        checks.add("canc-5", "读线程结束（SSE 正常收尾）", joined_ok, reader.error or "")
        checks.add("canc-6", "answer_end.cancelled=true 且 chunks == delta 数 == emitted_chunks",
                   end3.get("cancelled") is True
                   and int(end3.get("chunks") or -1) == len(deltas3)
                   and int(end3.get("chunks") or -1) == int(cancel_data.get("emitted_chunks") or -2),
                   "end=%s deltas=%d cancel=%s"
                   % (end3.get("chunks"), len(deltas3), cancel_data.get("emitted_chunks")))
        checks.add("canc-7", "0 < chunks < 总块数（确实中途停住）",
                   0 < len(deltas3) < total_chunks,
                   "chunks=%d total=%d" % (len(deltas3), total_chunks))
        checks.add("canc-8", "已发 delta 是完整回答的前缀，answer_start.chars 未变",
                   joined3 == reply[:len(joined3)] and int(start2.get("chars") or 0) == len(reply),
                   "prefix=%s chars=%s/%s" % (joined3 == reply[:len(joined3)],
                                              start2.get("chars"), len(reply)))
        notes = _all(events3, "note")
        checks.add("canc-9", "note 末条 text == 已停止输出，且仍有 done、无 error",
                   bool(notes) and str(notes[-1].get("text") or "") == "已停止输出"
                   and "done" in types3 and "error" not in types3,
                   json.dumps(notes, ensure_ascii=False)[:160])
        checks.add("canc-10", "事件序 answer_start→answer_end→note→done",
                   _order_ok(types3, ["answer_start", "answer_end", "note", "done"]),
                   types3)
        tool_ends = [d for d in _all(events3, "tool_end")
                     if str(d.get("tool") or "") == "spending_insights"]
        insight_card = _first(events3, "insight_card") or {}
        insight_ok = isinstance(insight_card.get("insight"), dict)
        checks.add("canc-11", "已执行只读工具不回滚（tool_end=spending_insights 且 insight_card 在）",
                   bool(tool_ends) and insight_ok
                   and int(((insight_card.get("insight") or {}).get("totals") or {})
                           .get("out_cents") or 0) > 0,
                   "tool_ends=%d insight=%s" % (len(tool_ends), insight_ok))

        # 取消不撤销已执行工具：余额不变
        status, acc_after, _ = _http_json(port, "GET",
                                          "/api/accounts?session_id=%s" % session_id)
        total_after = int((acc_after or {}).get("total_cents"))
        checks.add("canc-12", "取消前后账户总余额不变",
                   total_before is not None and total_before == total_after,
                   "%s -> %s" % (total_before, total_after))

        # 审计：answer_cancelled
        entries, chain = _verify(port, checks, "audit", session_id)
        cancelled_entries = [e for e in entries if e.get("event_type") == "answer_cancelled"]
        checks.add("audit-cancel", "审计恰 1 条 answer_cancelled 且 tool=stream",
                   len(cancelled_entries) == 1
                   and str(cancelled_entries[0].get("tool") or "") == "stream",
                   "count=%d" % len(cancelled_entries))

        # ---------------- 收尾：进程与端口 ----------------
        _stop_server(proc)
        proc = None
        time.sleep(0.4)
        checks.add("clean-1", "主服务已停止，端口 %d 无残留监听" % port,
                   not _port_listening(port), "port=%d" % port)
        log_path = LOG_DIR / ("server_%d.log" % port)
        log_text = ""
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        checks.add("clean-2", "服务日志无 Traceback", "Traceback" not in log_text,
                   "traceback" if "Traceback" in log_text else "clean")

        result["ok"] = not checks.failed
        result["session_id"] = session_id
        result["elapsed_s"] = round(time.time() - started, 2)
        result["full_stream_s"] = round(full_elapsed, 2)
        result["checks"] = checks.items
        result["summary"] = {"total": len(checks.items),
                             "pass": len(checks.items) - len(checks.failed),
                             "fail": len(checks.failed)}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["checks"] = checks.items
        result["summary"] = {"total": len(checks.items),
                             "pass": len(checks.items) - len(checks.failed),
                             "fail": len(checks.failed)}
        if not args.json:
            _log("ERROR: %s" % result["error"])
    finally:
        if proc is not None:
            _stop_server(proc)
        if not args.keep:
            _rmtree_retry(WORK)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result.get("summary") or {}
        _log("")
        _log("流式 e2e：%d/%d 通过，%d 失败（端口 %s，耗时 %.2fs）"
             % (summary.get("pass", 0), summary.get("total", 0),
                summary.get("fail", 0), result.get("port"), result.get("elapsed_s", 0.0)))
        if checks.failed:
            _log("失败项：")
            for item in checks.failed:
                _log("  - %s %s → %s" % (item["id"], item["desc"], item["detail"]))
    return 0 if result.get("ok") else (2 if result.get("error") else 1)


if __name__ == "__main__":
    sys.exit(main())
