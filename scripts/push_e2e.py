#!/usr/bin/env python3
"""ZeroStep 主动触达（/api/push）端到端测试（零依赖，仅标准库；v0.3.2 引入）。

覆盖「银行有事找你」的三态与三类模板：
- 未配置 CHANNEL_PUSH_URL：200 且 pushed=false / delivered=false / channel.status=not_configured；
- 配置且渠道桩可达：200 且 pushed=true / delivered=true / channel.status=delivered，
  桩收到 JSON {user_id, kind, title, text}，url_masked 打码、latency_ms>0；
- 配置但不可达：200 且 pushed=true / delivered=false / channel.status=failed，error 可读；
- 三类模板 subscription / large_txn / repayment 的文案关键数字（金额、日期、占比、卡号后四位）；
- 每次推送写 1 条审计 event_type=push_message / tool=push / actor=system / risk_level=low，
  审计链始终 valid；非法 kind 返回 400 BAD_KIND；缺省 body 按默认 kind=subscription。
- 静态 PWA 资源（v0.3.2 交付）：/manifest.webmanifest（application/manifest+json、icons 尺寸与
  实际 PNG 一致）、/sw.js（application/javascript、与源文件逐字节一致、含 install/fetch）、
  /assets/icons/ 5 个 PNG（200 image/png、尺寸与文件名一致、字节一致）。

隔离策略：把 backend/、skills/、VERSION 复制到 .eval_tmp/push_e2e/app/ 下再启动主服务，
使用独立数据库与独立日志，不碰仓库里的 backend/data/bank.db（演示/并行开发可能在用）。
主服务按三态各重启一次；渠道桩是脚本内自起的标准库 HTTP 服务，跑完即停。

用法：
    python scripts/push_e2e.py
    python scripts/push_e2e.py -v      # 失败时打印主服务日志尾部
    python scripts/push_e2e.py --json  # 追加打印机器可读结果

端口：主服务 8041（被占用时自动换空闲端口）、推送桩 8422（同上）。
退出码：0 全部通过；1 有失败；2 环境不可用。
"""
import argparse
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
WORK = os.path.join(ROOT, ".eval_tmp", "push_e2e")
APP = os.path.join(WORK, "app")
LOGS = os.path.join(WORK, "logs")
LOG_MAIN = os.path.join(LOGS, "main_server.log")

USER = "u001"
RE_YUAN = re.compile(r"¥\s?\d[\d,]*\.\d{2}")
RE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
RE_PCT = re.compile(r"\d+\.\d%")

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
    print("%s  %s  %-58s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


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
    for key in list(env):
        if key.startswith("PUSH_") or key in ("CHANNEL_PUSH_URL", "CHANNEL_PUSH_TIMEOUT"):
            env.pop(key, None)
    return env


def _start_main(port, push_url=None, push_timeout="5"):
    env = _base_env()
    env["PORT"] = str(port)
    if push_url:
        env["CHANNEL_PUSH_URL"] = push_url
        env["CHANNEL_PUSH_TIMEOUT"] = str(push_timeout)
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


def _stop_main(port):
    _stop_procs()
    _wait_port_free(port)


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
    """POST JSON，返回 (HTTP 状态码, 响应 dict)；4xx/5xx 返回响应体而不抛异常。"""
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


def _get_json(port, path, timeout=10.0):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_raw(port, path, timeout=15.0):
    """GET 原始响应，返回 (状态码, 小写头字典, body bytes)，用于静态资源断言。"""
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as resp:
        headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        return resp.status, headers, resp.read()


def _png_size(data):
    """从 PNG IHDR 读宽高；非法 PNG 返回 None。"""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        return struct.unpack(">II", data[16:24])
    except struct.error:
        return None


def _audit(port):
    data = _get_json(port, "/api/audit?limit=200")
    return data.get("entries") or [], data.get("chain") or {}


def _push_entries(entries):
    return [e for e in entries
            if e.get("event_type") == "push_message" and e.get("tool") == "push"]


def _tail(path, lines=25):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return "".join(fp.readlines()[-lines:])
    except OSError:
        return "(无日志)"


# ---------------- 推送渠道桩 ----------------
class PushStub(object):
    """模拟企业微信/钉钉机器人 webhook：POST /push 收 JSON，GET /health 返回调用次数。"""

    def __init__(self):
        self.rows = []
        self.headers = []
        self.port = 0
        self._httpd = None
        self._lock = threading.Lock()

    def start(self, preferred=8422):
        self.port = _pick_port(preferred)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # 安静
                return

            def _send(self, code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if urlparse(self.path).path == "/health":
                    return self._send(200, {"ok": True, "calls": len(stub.rows)})
                return self._send(404, {"error": "NOT_FOUND"})

            def do_POST(self):  # noqa: N802
                if urlparse(self.path).path != "/push":
                    return self._send(404, {"error": "NOT_FOUND"})
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {"_raw": raw[:200].decode("utf-8", "replace")}
                with stub._lock:
                    stub.rows.append(payload)
                    stub.headers.append({str(k): str(v) for k, v in self.headers.items()})
                return self._send(200, {"ok": True})

        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        httpd.daemon_threads = True
        httpd.allow_reuse_address = True
        self._httpd = httpd
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        _wait_http("http://127.0.0.1:%d/health" % self.port, timeout=10.0)
        return self.port

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def calls(self):
        return len(self.rows)


# ---------------- 断言片段 ----------------
def _channel_ok(body):
    channel = body.get("channel") if isinstance(body.get("channel"), dict) else {}
    return channel


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 主动触达 /api/push 端到端测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印主服务日志尾部")
    parser.add_argument("--json", action="store_true", help="追加打印机器可读结果")
    args = parser.parse_args(argv)

    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8-sig") as fp:
            version_text = fp.read().strip()
    except OSError:
        version_text = "?"

    main_port = _pick_port(8041)
    stub = PushStub()
    print("ZeroStep v%s 主动触达 E2E（主服务:%d 推送桩:%s）" % (version_text, main_port, "自动"))
    print("-" * 84)
    try:
        _prepare_app()
        if not os.path.exists(os.path.join(APP, "backend", "app", "push.py")):
            print("未找到 backend/app/push.py，主动触达尚未交付。")
            return 2

        # ================= 状态 A：未配置 CHANNEL_PUSH_URL =================
        _start_main(main_port)
        _wait_main(main_port)
        entries0, chain0 = _audit(main_port)
        base_push = len(_push_entries(entries0))

        # A-1: 缺省 body（{}）默认 kind=subscription，未配置渠道不发送
        try:
            st, body = _post(main_port, "/api/push", {})
            channel = _channel_ok(body)
            text = str(body.get("text") or "")
            title = str(body.get("title") or "")
            ok = (st == 200 and body.get("ok") is True and body.get("kind") == "subscription"
                  and body.get("user_id") == USER
                  and body.get("pushed") is False and body.get("delivered") is False
                  and channel.get("configured") is False and channel.get("status") == "not_configured"
                  and title.startswith("订阅扣费提醒")
                  and "自动扣费" in text and "取消订阅" in text
                  and RE_YUAN.search(text) is not None and RE_DATE.search(text) is not None)
            _check("push-1", "未配置推送地址：200 + pushed=false/not_configured + 订阅文案含金额与日期", ok,
                   "http=%s kind=%s pushed=%s status=%s title=%s" % (st, body.get("kind"), body.get("pushed"),
                                                                     channel.get("status"), title[:26]))
        except Exception as exc:  # noqa: BLE001
            _check("push-1", "未配置推送地址：200 + pushed=false/not_configured + 订阅文案含金额与日期", False, repr(exc))

        # A-2: large_txn 模板
        try:
            st, body = _post(main_port, "/api/push", {"kind": "large_txn", "user_id": USER})
            channel = _channel_ok(body)
            text = str(body.get("text") or "")
            title = str(body.get("title") or "")
            ok = (st == 200 and body.get("ok") is True and body.get("kind") == "large_txn"
                  and body.get("pushed") is False and channel.get("status") == "not_configured"
                  and title.startswith("大额支出提醒")
                  and "大额支出" in text and "占本月支出" in text and "冻结卡片" in text
                  and RE_YUAN.search(text) is not None and RE_PCT.search(text) is not None
                  and RE_DATE.search(text) is not None)
            _check("push-2", "large_txn：大额支出文案含金额/占比/日期且未配置时不发送", ok,
                   "http=%s ok=%s title=%s" % (st, body.get("ok"), title[:26]))
        except Exception as exc:  # noqa: BLE001
            _check("push-2", "large_txn：大额支出文案含金额/占比/日期且未配置时不发送", False, repr(exc))

        # A-3: repayment 模板（种子数据：信用卡 **** 6712 待还 ¥8,432.00）
        try:
            st, body = _post(main_port, "/api/push", {"kind": "repayment"})
            channel = _channel_ok(body)
            text = str(body.get("text") or "")
            title = str(body.get("title") or "")
            ok = (st == 200 and body.get("ok") is True and body.get("kind") == "repayment"
                  and body.get("pushed") is False and channel.get("status") == "not_configured"
                  and title.startswith("还款提醒")
                  and "**** 6712" in text and "当前待还 ¥8,432.00" in text and "最晚" in text
                  and "二次确认" in text and RE_DATE.search(text) is not None)
            _check("push-3", "repayment：还款文案含卡号后四位/待还金额/最晚还款日", ok,
                   "http=%s title=%s text=%s" % (st, title[:22], text[:46]))
        except Exception as exc:  # noqa: BLE001
            _check("push-3", "repayment：还款文案含卡号后四位/待还金额/最晚还款日", False, repr(exc))

        # A-4: 每次推送写一条 push_message 审计，链 valid
        try:
            entries, chain = _audit(main_port)
            pushes = _push_entries(entries)
            delta = len(pushes) - base_push
            fields_ok = all(p.get("actor") == "system" and p.get("risk_level") == "low"
                            and p.get("event_type") == "push_message" and p.get("tool") == "push"
                            for p in pushes)
            seqs = [int(p.get("seq") or 0) for p in pushes]
            ok = (delta == 3 and chain.get("valid") is True and int(chain.get("checked") or 0) >= 3
                  and fields_ok and len(seqs) == len(set(seqs)) and seqs == sorted(seqs))
            _check("push-4", "三次推送各写 1 条 push_message 审计（actor=system/risk=low），链 valid", ok,
                   "delta=%d checked=%s valid=%s" % (delta, chain.get("checked"), chain.get("valid")))
        except Exception as exc:  # noqa: BLE001
            _check("push-4", "三次推送各写 1 条 push_message 审计（actor=system/risk=low），链 valid", False, repr(exc))

        # A-5: 非法 kind 400 BAD_KIND；缺省再次按默认 kind 并审计 +1
        try:
            st_bad, body_bad = _post(main_port, "/api/push", {"kind": "not_a_kind"})
            err = body_bad.get("error") if isinstance(body_bad.get("error"), dict) else {}
            bad_ok = (st_bad == 400 and err.get("code") == "BAD_KIND"
                      and "subscription" in str(err.get("message") or ""))
            st2, body2 = _post(main_port, "/api/push", {})
            entries2, _ = _audit(main_port)
            delta2 = len(_push_entries(entries2)) - base_push
            ok = bad_ok and st2 == 200 and body2.get("kind") == "subscription" and delta2 == 4
            _check("push-5", "非法 kind 返回 400 BAD_KIND；缺省 body 按 subscription 且审计累计 4 条", ok,
                   "bad_http=%s bad_code=%s default_kind=%s delta=%d" % (st_bad, err.get("code"),
                                                                        body2.get("kind"), delta2))
        except Exception as exc:  # noqa: BLE001
            _check("push-5", "非法 kind 返回 400 BAD_KIND；缺省 body 按 subscription 且审计累计 4 条", False, repr(exc))

        # ================= 状态 B：配置且渠道桩可达 =================
        _stop_main(main_port)
        stub.start(8422)
        push_url = "http://127.0.0.1:%d/push" % stub.port
        _start_main(main_port, push_url=push_url, push_timeout="5")
        _wait_main(main_port)
        entries_b0, _ = _audit(main_port)
        base_b = len(_push_entries(entries_b0))

        # B-1: 推送成功三态字段与桩收到的 JSON 契约
        try:
            st, body = _post(main_port, "/api/push", {"kind": "subscription", "user_id": USER})
            channel = _channel_ok(body)
            row = stub.rows[0] if stub.rows else {}
            header = stub.headers[0] if stub.headers else {}
            content_type = ""
            for key, value in header.items():
                if key.lower() == "content-type":
                    content_type = value
            payload_ok = (isinstance(row, dict)
                          and row.get("user_id") == USER and row.get("kind") == "subscription"
                          and isinstance(row.get("title"), str) and row.get("title")
                          and isinstance(row.get("text"), str) and row.get("text"))
            masked_ok = (str(channel.get("url_masked") or "").startswith("http://127.0.0.1:%d" % stub.port)
                         and str(channel.get("url_masked") or "").endswith("/***"))
            ok = (st == 200 and body.get("ok") is True and body.get("pushed") is True
                  and body.get("delivered") is True and channel.get("configured") is True
                  and channel.get("status") == "delivered" and int(channel.get("latency_ms") or 0) > 0
                  and masked_ok and payload_ok and "application/json" in content_type.lower())
            _check("push-6", "配置且桩可达：delivered=true + url_masked 打码 + 桩收到 user_id/kind/title/text", ok,
                   "http=%s pushed=%s delivered=%s status=%s masked=%s stub=%d" % (
                       st, body.get("pushed"), body.get("delivered"), channel.get("status"),
                       masked_ok, stub.calls))
        except Exception as exc:  # noqa: BLE001
            _check("push-6", "配置且桩可达：delivered=true + url_masked 打码 + 桩收到 JSON 契约", False, repr(exc))

        # B-2: 三类模板逐个推送均送达，桩记录 kind 序列正确，审计每条各一条
        try:
            kinds = ["subscription", "large_txn", "repayment"]
            statuses = []
            for kind in kinds:
                st, body = _post(main_port, "/api/push", {"kind": kind})
                channel = _channel_ok(body)
                statuses.append((st, body.get("kind"), body.get("delivered"), channel.get("status")))
            entries_b, chain_b = _audit(main_port)
            delta_b = len(_push_entries(entries_b)) - base_b
            delivered_ok = all(s[0] == 200 and s[1] == kinds[i] and s[2] is True and s[3] == "delivered"
                               for i, s in enumerate(statuses))
            stub_kinds = [str(r.get("kind")) for r in stub.rows if isinstance(r, dict)]
            ok = (delivered_ok and stub_kinds == ["subscription"] + kinds
                  and stub.calls == 4 and delta_b == 4 and chain_b.get("valid") is True)
            _check("push-7", "三类模板逐个送达（桩收到 4 条，kind 序列正确），审计各 1 条", ok,
                   "statuses=%s stub_kinds=%s delta=%d" % (statuses, stub_kinds, delta_b))
        except Exception as exc:  # noqa: BLE001
            _check("push-7", "三类模板逐个送达（桩收到 4 条，kind 序列正确），审计各 1 条", False, repr(exc))

        # ================= 状态 C：配置但不可达 =================
        _stop_main(main_port)
        dead_port = _free_port()  # 取一个空闲端口但不监听，连接会被拒绝
        dead_url = "http://127.0.0.1:%d/push" % dead_port
        _start_main(main_port, push_url=dead_url, push_timeout="2")
        _wait_main(main_port)
        entries_c0, _ = _audit(main_port)
        base_c = len(_push_entries(entries_c0))
        try:
            started = time.time()
            st, body = _post(main_port, "/api/push", {"kind": "subscription"})
            elapsed = time.time() - started
            channel = _channel_ok(body)
            text = json.dumps(body, ensure_ascii=False)
            entries_c, chain_c = _audit(main_port)
            delta_c = len(_push_entries(entries_c)) - base_c
            ok = (st == 200 and body.get("ok") is True and body.get("pushed") is True
                  and body.get("delivered") is False and channel.get("status") == "failed"
                  and "推送地址不可达" in str(channel.get("error") or "")
                  and "Traceback" not in text and delta_c == 1
                  and chain_c.get("valid") is True and elapsed < 20.0)
            _check("push-8", "配置但不可达：200 + delivered=false + error 可读（无 traceback），审计仍写 1 条", ok,
                   "http=%s pushed=%s delivered=%s status=%s error=%s delta=%d %.2fs" % (
                       st, body.get("pushed"), body.get("delivered"), channel.get("status"),
                       str(channel.get("error"))[:38], delta_c, elapsed))
        except Exception as exc:  # noqa: BLE001
            _check("push-8", "配置但不可达：200 + delivered=false + error 可读（无 traceback），审计仍写 1 条", False, repr(exc))

        # C-2: /api/push 不修改账务数据（只读）——账户总数与余额不变
        try:
            _, sess = _post(main_port, "/api/session", {})
            sid = str(sess.get("session_id") or "")
            accounts = _get_json(main_port, "/api/accounts?session_id=" + sid)
            total = int(accounts.get("total_cents") or 0)
            expected_total = 1286050 + 4500000  # 种子：工资卡 12,860.50 + 储蓄卡 45,000.00（信用卡不计入资产总额）
            _check("push-9", "主动触达只读：账户总额与种子数据一致（推送未改动账务）", total == expected_total,
                   "total_cents=%d expect=%d" % (total, expected_total))
        except Exception as exc:  # noqa: BLE001
            _check("push-9", "主动触达只读：账户总额与种子数据一致（推送未改动账务）", False, repr(exc))

        # ================= 状态 D：静态 PWA 资源（v0.3.2） =================
        # 主服务仍在运行，直接对隔离副本里的 frontend/ 做静态断言
        frontend_dir = os.path.join(ROOT, "frontend")

        # D-1: manifest 契约（content-type + 内容 + icons 尺寸与实际 PNG 一致）
        try:
            st, headers, raw = _get_raw(main_port, "/manifest.webmanifest")
            ctype = str(headers.get("content-type") or "")
            src_manifest = os.path.join(frontend_dir, "manifest.webmanifest")
            try:
                manifest = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                manifest = {}
            icons = manifest.get("icons") if isinstance(manifest.get("icons"), list) else []
            icon_ok = True
            icon_detail = []
            for item in icons:
                if not isinstance(item, dict):
                    icon_ok = False
                    continue
                src = str(item.get("src") or "")
                want = str(item.get("sizes") or "")
                local = os.path.join(frontend_dir, src.lstrip("/").replace("/", os.sep))
                data = None
                try:
                    with open(local, "rb") as fp:
                        data = fp.read()
                except OSError:
                    icon_ok = False
                dims = _png_size(data or b"")
                if dims is None or want != "%dx%d" % dims:
                    icon_ok = False
                icon_detail.append("%s=%s" % (src.rsplit("/", 1)[-1], want))
                if item.get("type") != "image/png":
                    icon_ok = False
            src_size = os.path.getsize(src_manifest) if os.path.exists(src_manifest) else -1
            ok = (st == 200 and ctype.startswith("application/manifest+json")
                  and len(raw) == src_size and bool(raw)
                  and manifest.get("name") == "ZeroStep 零步银行"
                  and manifest.get("short_name") == "ZeroStep"
                  and manifest.get("start_url") == "/" and manifest.get("scope") == "/"
                  and manifest.get("display") == "standalone"
                  and len(icons) == 4 and icon_ok
                  and all(str(i.get("src") or "").startswith("/assets/icons/") for i in icons if isinstance(i, dict)))
            _check("push-10", "PWA：/manifest.webmanifest 200 application/manifest+json，icons 尺寸与实际 PNG 一致", ok,
                   "http=%s ctype=%s bytes=%d icons=%s" % (st, ctype, len(raw), ",".join(icon_detail)))
        except Exception as exc:  # noqa: BLE001
            _check("push-10", "PWA：/manifest.webmanifest 200 application/manifest+json，icons 尺寸与实际 PNG 一致", False, repr(exc))

        # D-2: Service Worker 契约（content-type + 与源文件逐字节一致 + install/fetch 逻辑）
        try:
            st, headers, raw = _get_raw(main_port, "/sw.js")
            ctype = str(headers.get("content-type") or "")
            src_sw = os.path.join(frontend_dir, "sw.js")
            source = b""
            if os.path.exists(src_sw):
                with open(src_sw, "rb") as fp:
                    source = fp.read()
            text = raw.decode("utf-8", "replace")
            ok = (st == 200 and ctype.startswith("application/javascript")
                  and raw == source and bool(raw)
                  and "self.addEventListener('install'" in text
                  and "self.addEventListener('fetch'" in text
                  and "self.addEventListener('activate'" in text
                  and "'/manifest.webmanifest'" in text and "isApi" in text)
            _check("push-11", "PWA：/sw.js 200 application/javascript，与源文件逐字节一致且含 install/activate/fetch", ok,
                   "http=%s ctype=%s bytes=%d equal=%s" % (st, ctype, len(raw), raw == source))
        except Exception as exc:  # noqa: BLE001
            _check("push-11", "PWA：/sw.js 200 application/javascript，与源文件逐字节一致且含 install/activate/fetch", False, repr(exc))

        # D-3: 图标文件（5 个，content-type/尺寸/字节 三项一致）
        try:
            icons = [("icon-192.png", 192), ("icon-512.png", 512),
                     ("icon-192-maskable.png", 192), ("icon-512-maskable.png", 512),
                     ("apple-touch-icon.png", 180)]
            problems = []
            sizes_seen = []
            for name, want in icons:
                st, headers, raw = _get_raw(main_port, "/assets/icons/" + name)
                ctype = str(headers.get("content-type") or "")
                dims = _png_size(raw)
                sizes_seen.append("%s:%s" % (name, dims))
                if st != 200:
                    problems.append("%s http=%s" % (name, st))
                if not ctype.startswith("image/png"):
                    problems.append("%s ctype=%s" % (name, ctype))
                if dims != (want, want):
                    problems.append("%s dims=%s" % (name, dims))
                src_path = os.path.join(frontend_dir, "assets", "icons", name)
                try:
                    with open(src_path, "rb") as fp:
                        if raw != fp.read():
                            problems.append("%s bytes!=source" % name)
                except OSError:
                    problems.append("%s source missing" % name)
            _check("push-12", "PWA：5 个图标均 200 image/png，PNG 尺寸与文件名一致且与源文件字节一致", not problems,
                   "；".join(problems) or "5/5 通过（%s）" % ", ".join(sizes_seen))
        except Exception as exc:  # noqa: BLE001
            _check("push-12", "PWA：5 个图标均 200 image/png，PNG 尺寸与文件名一致且与源文件字节一致", False, repr(exc))

    except Exception as exc:  # noqa: BLE001
        print("运行异常：%s: %s" % (type(exc).__name__, exc))
        if args.verbose:
            import traceback
            traceback.print_exc()
    finally:
        stub.stop()
        _stop_main(main_port)
        try:
            _wait_port_free(stub.port, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    return _finish(args, main_port, stub.port)


def _finish(args, main_port, stub_port):
    passed = sum(1 for c in CHECKS if c["ok"])
    total = len(CHECKS)
    residue = [p for p in (main_port, stub_port) if p and _port_in_use(p)]
    _check("push-13", "收尾：主服务与推送桩端口均无残留进程监听", not residue,
           "residue=%s" % (residue or "无"))
    passed = sum(1 for c in CHECKS if c["ok"])
    total = len(CHECKS)
    print("-" * 84)
    print("结果：%d/%d 项通过" % (passed, total))
    if passed != total and args.verbose:
        for c in CHECKS:
            if not c["ok"]:
                print("\n[FAIL %s] %s  %s" % (c["id"], c["desc"], c["detail"]))
        print("\n--- main_server.log 尾部 ---\n" + _tail(LOG_MAIN))
    if args.json:
        print(json.dumps({"passed": passed, "total": total, "checks": CHECKS}, ensure_ascii=False))
    if total == 0:
        return 2
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
