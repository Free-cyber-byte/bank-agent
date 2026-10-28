#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ZeroStep 企业微信回调适配器 e2e（零依赖，独立验证；版本动态读 VERSION）。

与 backend/channels/wecom.py --selftest 不同：本脚本不依赖产品自测结论，
而是自己生成 43 位 EncodingAESKey / Token / CorpID，用独立实现的
PKCS#7 + CBC 链接 + 签名算法构造加密请求，直接打 HTTP 回调接口，
再独立解密/验签服务端回包。产品侧只复用 AES-128 单分组原语，
且先用 FIPS-197 Appendix B/C 官方向量做已知答案校验（KAT），
因此 AES 之外的封装、补位、CBC、签名、XML 结构断言均为独立实现。

覆盖（对应 task-32 范围 A 的 8 项 + 独立补充）：
  wxm-1  AES/PKCS7/密钥派生/签名 独立自检（FIPS-197 KAT）
  wxm-2  GET /health 字段齐全且不泄露 Token / AESKey / CorpID
  wxm-3  GET /wecom 验签后解密 echostr 返回明文
  wxm-4  GET 篡改 msg_signature → 403；缺参数 → 400
  wxm-5  主服务不可达 → 200 加密回包，文案友好且无堆栈
  wxm-6  POST 合法消息 → 回包可独立解密/验签，主服务收到正确 body
  wxm-7  同一 MsgId 重发（故意换文案）→ 回包复读且主服务调用次数不增
  wxm-8  两步确认：needs_confirmation 摘要 +「确认」转发 /api/approval
  wxm-9  回包不含 approval_id / thread_id / confirmation.token / nonce 等内部凭据
  wxm-10 非法报文：坏 XML 400 / 坏签名 403 / CorpID 不符 400
  wxm-11 非文本消息 → 200 友好提示
  wxm-12 转发超时：WECOM_TIMEOUT=1，主服务慢 2.5s → 200「处理超时」且 < 2.2s
  wxm-13 清理：进程全退出、端口无残留、日志无 Traceback

用法：
    python scripts/channel_wecom_e2e.py            # 文本输出，0 全过 / 1 有失败 / 2 环境错误
    python scripts/channel_wecom_e2e.py --json     # 额外输出 JSON 汇总
    python scripts/channel_wecom_e2e.py --keep     # 保留 .eval_tmp/channel_wecom_e2e（默认保留，供排查）

端口：企微主实例优先 8421，超时实例优先 8423，假主服务优先 8041；
被占用时自动换空闲端口，跑完检查三个端口都没有 LISTENING 残留。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # bank-agent
WORK = ROOT / ".eval_tmp" / "channel_wecom_e2e"
APP = WORK / "app"                                      # 隔离副本，避免污染仓库内 sessions.json/messages.jsonl


def _repo_version() -> str:
    """读仓库根 VERSION（banner 用，避免硬编码版本号）。"""
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


HOST = "127.0.0.1"
MAIN_PORT_PREF = 8041
WECOM_PORT_PREF = 8421
TIMEOUT_PORT_PREF = 8423
PYTHON = sys.executable or "python"

# 每次运行随机生成一套合法企微凭据（43 位 Base64 EncodingAESKey）
TOKEN = "wxm_qa_token_%06d" % random.randint(1, 999999)
CORP_ID = "ww_wxm_qa_corp_%04d" % random.randint(1, 9999)
AES_KEY = base64.b64encode(os.urandom(32)).decode("ascii")[:43]

UNAVAILABLE_REPLY = "渠道服务暂时不可用，请稍后再试"
TIMEOUT_REPLY = "处理超时，请稍后再试"
PREVIEW = "向「妈妈」转账 ¥800.00（已知收款人）"
HINT = "回复 确认 或 取消"

RESULTS = []          # [{"id","desc","ok","detail"}]
PROCS = []            # [(Popen, log_handle, name)]
STUB = None
MAIN_URL = ""
WECOM_BASE = ""
TIMEOUT_BASE = ""
_CRYPTO = None

_STUB_STATE = {
    "calls": {"session": 0, "chat": 0, "approval": 0},
    "rows": [],
    "mode": "ok",       # ok | awaiting | slow
    "delay": 2.5,
    "lock": threading.Lock(),
}


# ======================= 基础工具 =======================
def _record(cid: str, desc: str, ok: bool, detail: str = "") -> None:
    item = {"id": cid, "desc": desc, "ok": bool(ok), "detail": str(detail or "")}
    RESULTS.append(item)
    line = "%s  %-7s %s" % ("PASS" if ok else "FAIL", cid, desc)
    if detail:
        line += "  | " + str(detail)
    print(line, flush=True)


def _stub_calls() -> dict:
    with _STUB_STATE["lock"]:
        return dict(_STUB_STATE["calls"])


def _stub_rows(path: str = "") -> list:
    with _STUB_STATE["lock"]:
        rows = list(_STUB_STATE["rows"])
    if path:
        return [row for row in rows if row.get("path") == path]
    return rows


def _set_stub_mode(mode: str) -> None:
    with _STUB_STATE["lock"]:
        _STUB_STATE["mode"] = mode


def _connect_ok(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((HOST, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _free_port(preferred: int) -> int:
    """优先用约定端口；被占用时退回系统分配端口，绝不 kill 别人的进程。

    注意：Windows 下 SO_REUSEADDR 允许两个进程绑同一端口，会让「是否被占用」
    判断失真，因此这里先 verify 无监听、再用裸 bind 探测。
    """
    if not _connect_ok(preferred):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((HOST, int(preferred)))
            return int(sock.getsockname()[1])
        except OSError:
            pass
        finally:
            sock.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _listening_pids(port: int) -> list:
    """netstat 找 LISTENING 的 PID（Windows）；失败时退回 connect 探测。"""
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, timeout=15).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or "LISTENING" not in line:
                continue
            local = parts[1]
            if local.endswith(":%d" % int(port)):
                pids.add(parts[-1])
    except Exception:  # noqa: BLE001 - 非 Windows / netstat 不可用时退回探测
        if _connect_ok(port):
            pids.add("?")
    return sorted(pids)


def _tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _http(method: str, url: str, body: bytes = None, timeout: float = 15.0):
    request = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _wait_http(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            status, _, _ = _http("GET", url, timeout=2.0)
            if status == 200:
                return
            last = "HTTP %d" % status
        except Exception as exc:  # noqa: BLE001
            last = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(0.15)
    raise RuntimeError("等待 %s 就绪超时（%s）" % (url, last))


# ======================= 独立加解密（仅复用 AES 单分组原语） =======================
def _load_crypto() -> None:
    global _CRYPTO
    channel_dir = str(APP / "backend" / "channels")
    if channel_dir not in sys.path:
        sys.path.insert(0, channel_dir)
    import importlib
    _CRYPTO = importlib.import_module("wecom_crypto")


def _derive_key(encoding_aes_key: str) -> bytes:
    """43 位 EncodingAESKey → 16 字节 AES key（独立实现：base64 补 '=' 解 32 字节取前 16）。"""
    value = str(encoding_aes_key or "").strip()
    if len(value) != 43:
        raise AssertionError("EncodingAESKey 必须是 43 位，实际 %d" % len(value))
    raw = base64.b64decode(value + "=")
    if len(raw) != 32:
        raise AssertionError("EncodingAESKey 解码后应为 32 字节，实际 %d" % len(raw))
    return raw[:16]


KEY = b""


def _pad32(data: bytes) -> bytes:
    pad = 32 - len(data) % 32
    return data + bytes([pad]) * pad


def _unpad32(data: bytes) -> bytes:
    if not data or len(data) % 32:
        raise ValueError("密文长度不是 32 的整数倍")
    pad = data[-1]
    if pad < 1 or pad > 32 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS#7 补位非法")
    return data[:-pad]


def _cbc_encrypt(key: bytes, data: bytes) -> bytes:
    """独立 AES-CBC 链接（IV = key，逐块异或后调 AES 单分组原语）。"""
    out = bytearray()
    prev = key
    for offset in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + 16], prev))
        prev = _CRYPTO.encrypt_block(key, block)
        out.extend(prev)
    return bytes(out)


def _cbc_decrypt(key: bytes, data: bytes) -> bytes:
    out = bytearray()
    prev = key
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        plain = _CRYPTO.decrypt_block(key, block)
        out.extend(a ^ b for a, b in zip(plain, prev))
        prev = block
    return bytes(out)


def _wx_encrypt(msg: bytes, corp_id: str = CORP_ID) -> str:
    """独立构造企微密文：random16 + msg_len(4,网络序) + msg + corp_id，PKCS#7 补到 32 字节。"""
    raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + str(corp_id or "").encode("utf-8")
    return base64.b64encode(_cbc_encrypt(KEY, _pad32(raw))).decode("ascii")


def _wx_decrypt(encrypt_b64: str) -> bytes:
    cipher = base64.b64decode(str(encrypt_b64 or ""))
    raw = _unpad32(_cbc_decrypt(KEY, cipher))
    if len(raw) < 20:
        raise ValueError("解密后内容过短")
    msg_len = struct.unpack(">I", raw[16:20])[0]
    if 20 + msg_len > len(raw):
        raise ValueError("消息长度字段非法")
    tail = raw[20 + msg_len:].decode("utf-8", "ignore")
    if tail != CORP_ID:
        raise AssertionError("回包 CorpID 尾不一致：%r" % tail)
    return raw[20:20 + msg_len]


def _verify_sig(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """独立签名：sha1(sorted([token, timestamp, nonce, encrypt]) 拼接)。"""
    parts = sorted([str(token or ""), str(timestamp or ""), str(nonce or ""), str(encrypt or "")])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def _crypto_kat() -> list:
    """FIPS-197 官方向量 + 密钥派生/补位/签名自检，返回失败列表。"""
    fails = []
    cases = [
        ("Appendix C",
         bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
         bytes.fromhex("00112233445566778899aabbccddeeff"),
         bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")),
        ("Appendix B",
         bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"),
         bytes.fromhex("3243f6a8885a308d313198a2e0370734"),
         bytes.fromhex("3925841d02dc09fbdc118597196a0b32")),
    ]
    for name, key, plain, cipher in cases:
        try:
            got = _CRYPTO.encrypt_block(key, plain)
            if got != cipher:
                fails.append("%s 加密不符：%s" % (name, got.hex()))
            back = _CRYPTO.decrypt_block(key, cipher)
            if back != plain:
                fails.append("%s 解密不符" % name)
        except Exception as exc:  # noqa: BLE001
            fails.append("%s 异常：%s" % (name, exc))
    try:
        if len(_derive_key(AES_KEY)) != 16:
            fails.append("密钥派生长度不是 16 字节")
    except Exception as exc:  # noqa: BLE001
        fails.append("密钥派生异常：%s" % exc)
    for size in (0, 1, 15, 16, 31, 32, 33, 64):
        data = bytes(range(size))
        try:
            if _unpad32(_pad32(data)) != data:
                fails.append("补位往返不一致 size=%d" % size)
        except Exception as exc:  # noqa: BLE001
            fails.append("补位异常 size=%d：%s" % (size, exc))
    try:
        expect = hashlib.sha1("".join(sorted([TOKEN, "1700000000", "nonce_x", "ENC"])).encode()).hexdigest()
        if _verify_sig(TOKEN, "1700000000", "nonce_x", "ENC") != expect:
            fails.append("签名算法不是 sha1(sorted 拼接)")
    except Exception as exc:  # noqa: BLE001
        fails.append("签名异常：%s" % exc)
    return fails


# ======================= XML / 回调报文 =======================
def _xml_text(root, tag: str) -> str:
    try:
        return str(root.findtext(tag) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _msg_xml(from_user: str, content: str, msg_id: str, msg_type: str = "text") -> str:
    return (
        "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
        "<FromUserName><![CDATA[%s]]></FromUserName>"
        "<CreateTime>%d</CreateTime>"
        "<MsgType><![CDATA[%s]]></MsgType>"
        "<Content><![CDATA[%s]]></Content>"
        "<MsgId>%s</MsgId><AgentID>1000002</AgentID></xml>"
    ) % (CORP_ID, from_user, int(time.time()), msg_type, content, msg_id)


def _new_msg_id(prefix: str = "qa") -> str:
    return "%s_%d_%06d" % (prefix, int(time.time()), random.randint(1, 999999))


def _get_verify_url(base: str, plaintext: str):
    echostr = _wx_encrypt(plaintext.encode("utf-8"))
    timestamp = str(int(time.time()))
    nonce = "nonce_%06d" % random.randint(1, 999999)
    signature = _verify_sig(TOKEN, timestamp, nonce, echostr)
    url = "%s/wecom?msg_signature=%s&timestamp=%s&nonce=%s&echostr=%s" % (
        base, signature, timestamp, nonce, urllib.parse.quote(echostr))
    return url, signature, timestamp, nonce, echostr


def _post_wecom(base: str, xml_text: str, sign: bool = True, enc_corp: str = CORP_ID):
    encrypt = _wx_encrypt(xml_text.encode("utf-8"), enc_corp)
    timestamp = str(int(time.time()))
    nonce = "nonce_%06d" % random.randint(1, 999999)
    signature = _verify_sig(TOKEN, timestamp, nonce, encrypt) if sign else "0" * 40
    url = "%s/wecom?msg_signature=%s&timestamp=%s&nonce=%s" % (base, signature, timestamp, nonce)
    body = ("<xml><Encrypt><![CDATA[%s]]></Encrypt></xml>" % encrypt).encode("utf-8")
    started = time.time()
    status, content_type, raw = _http("POST", url, body)
    return {"status": status, "content_type": content_type, "body": raw,
            "elapsed": time.time() - started, "encrypt": encrypt}


def _parse_reply(raw: bytes) -> dict:
    """独立解析被动回包：校验外层签名、独立解密、解析内层 XML。"""
    text = raw.decode("utf-8", "ignore")
    outer = ET.fromstring(text)
    encrypt = _xml_text(outer, "Encrypt")
    msg_signature = _xml_text(outer, "MsgSignature")
    timestamp = _xml_text(outer, "TimeStamp")
    nonce = _xml_text(outer, "Nonce")
    inner_text = _wx_decrypt(encrypt).decode("utf-8", "ignore")
    inner = ET.fromstring(inner_text)
    return {
        "body": text,
        "outer_tags": sorted(child.tag for child in outer),
        "inner_tags": sorted(child.tag for child in inner),
        "sig_ok": bool(encrypt and msg_signature and timestamp and nonce
                       and _verify_sig(TOKEN, timestamp, nonce, encrypt) == msg_signature),
        "timestamp": timestamp,
        "nonce": nonce,
        "content": _xml_text(inner, "Content"),
        "to_user": _xml_text(inner, "ToUserName"),
        "from_user": _xml_text(inner, "FromUserName"),
        "msg_type": _xml_text(inner, "MsgType"),
        "create_time": _xml_text(inner, "CreateTime"),
        "inner_text": inner_text,
    }


# ======================= 假主服务（渠道层上游） =======================
class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802 - 静默
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True, "mode": _STUB_STATE["mode"]})
        if path == "/_stats":
            with _STUB_STATE["lock"]:
                payload = {"calls": dict(_STUB_STATE["calls"]),
                           "rows": list(_STUB_STATE["rows"]),
                           "mode": _STUB_STATE["mode"]}
            return self._send(200, payload)
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        with _STUB_STATE["lock"]:
            _STUB_STATE["rows"].append({"path": path, "body": data})
            mode = _STUB_STATE["mode"]
            delay = _STUB_STATE["delay"]
            if path == "/api/session":
                _STUB_STATE["calls"]["session"] += 1
            elif path == "/api/chat":
                _STUB_STATE["calls"]["chat"] += 1
            elif path == "/api/approval":
                _STUB_STATE["calls"]["approval"] += 1
        if path == "/api/session":
            return self._send(200, {"session_id": "sess_fake_001",
                                    "thread_id": "thread_secret_001",
                                    "user_id": "u001"})
        if path == "/api/chat":
            if mode == "slow":
                time.sleep(delay)
            if mode == "awaiting":
                return self._send(200, {
                    "status": "awaiting_approval",
                    "session_id": "sess_fake_001",
                    "intent": {"name": "transfer"},
                    "approval": {
                        "approval_id": "ap_secret_001",
                        "nonce": "nonce_secret_001",
                        "thread_id": "thread_secret_001",
                        "preview": PREVIEW,
                        "title": "转账确认",
                        "risk_level": "medium",
                    },
                })
            return self._send(200, {"status": "ok", "session_id": "sess_fake_001",
                                    "reply": "（桩）已收到：%s" % str(data.get("text") or "")})
        if path == "/api/approval":
            return self._send(200, {"status": "ok", "session_id": "sess_fake_001",
                                    "reply": "（桩）已确认执行转账 ¥800.00"})
        return self._send(404, {"error": {"code": "NOT_FOUND"}})


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """假主服务专用：客户端被 kill 造成的连接重置属预期，不打堆栈污染测试输出。"""
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: ARG002
        return


class _StubServer:
    def __init__(self, port: int):
        self.httpd = _QuietThreadingHTTPServer((HOST, int(port)), _StubHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fake-main", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


# ======================= 隔离副本 / 子进程 =======================
def _prepare_app() -> None:
    if APP.exists():
        shutil.rmtree(APP, ignore_errors=True)
    APP.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo",
                                    "data", "messages.jsonl", "sessions.json")
    shutil.copytree(str(ROOT / "backend"), str(APP / "backend"), ignore=ignore)
    (APP / "backend" / "data").mkdir(parents=True, exist_ok=True)
    version = ROOT / "VERSION"
    if version.is_file():
        shutil.copy2(str(version), str(APP / "VERSION"))
    else:
        (APP / "VERSION").write_text("0.0.0", encoding="utf-8")


def _base_env() -> dict:
    blocked = {
        "WECOM_TOKEN", "WECOM_AES_KEY", "WECOM_CORP_ID", "WECOM_TIMEOUT",
        "WECOM_DEDUPE_SECONDS", "WECOM_PORT", "WECOM_HOST", "WECOM_ENABLED",
        "ZEROSTEP_MAIN_URL", "MAIN_URL", "ZEROSTEP_API_URL", "CHANNEL_TOKEN",
        "PORT", "HOST", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "DEMO_MODE": "1",
        "ZEROSTEP_DISABLE_LLM": "1",
        "MCP_ENABLED": "0",
        "CHANNEL_ENABLED": "0",
        "WECOM_ENABLED": "1",
        "WECOM_TOKEN": TOKEN,
        "WECOM_AES_KEY": AES_KEY,
        "WECOM_CORP_ID": CORP_ID,
        "WECOM_TIMEOUT": "5",
        "WECOM_DEDUPE_SECONDS": "300",
        "ZEROSTEP_MAIN_URL": MAIN_URL,
        "PYTHONPATH": str(APP / "backend"),
    })
    return env


def _start_wecom(port: int, timeout_seconds: str, log_name: str):
    env = _base_env()
    env["WECOM_TIMEOUT"] = str(timeout_seconds)
    env["WECOM_PORT"] = str(port)
    log_path = WORK / log_name
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [PYTHON, str(APP / "backend" / "channels" / "wecom.py"),
         "--port", str(port), "--host", HOST],
        cwd=str(APP), env=env, stdout=handle, stderr=subprocess.STDOUT,
    )
    PROCS.append((proc, handle, log_name))
    return proc


def _stop_all() -> None:
    for proc, handle, _name in PROCS:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass
    del PROCS[:]
    if STUB is not None:
        STUB.stop()


def _health(base: str) -> dict:
    status, _, raw = _http("GET", base + "/health")
    if status != 200:
        raise AssertionError("/health 返回 HTTP %d" % status)
    return json.loads(raw.decode("utf-8"))


# ======================= 各检查项 =======================
def _check_health(base: str, port: int) -> None:
    problems = []
    try:
        health = _health(base)
    except Exception as exc:  # noqa: BLE001
        _record("wxm-2", "GET /health 字段齐全且不泄露密钥", False, "异常：%s" % exc)
        return
    expect_keys = ["ok", "service", "enabled", "token_set", "aes_key_set", "corp_id_set",
                   "dedupe_size", "dedupe_hits", "port", "main_url", "ts"]
    for key in expect_keys:
        if key not in health:
            problems.append("缺字段 %s" % key)
    if health.get("ok") is not True:
        problems.append("ok=%r" % health.get("ok"))
    if health.get("service") != "wecom":
        problems.append("service=%r" % health.get("service"))
    for key in ("enabled", "token_set", "aes_key_set", "corp_id_set"):
        if health.get(key) is not True:
            problems.append("%s=%r" % (key, health.get(key)))
    if int(health.get("port") or 0) != port:
        problems.append("port=%r（期望 %d）" % (health.get("port"), port))
    if str(health.get("main_url") or "") != MAIN_URL:
        problems.append("main_url=%r（期望 %s）" % (health.get("main_url"), MAIN_URL))
    body_text = json.dumps(health, ensure_ascii=False)
    for secret, label in ((TOKEN, "Token"), (AES_KEY, "AESKey"), (CORP_ID, "CorpID")):
        if secret and secret in body_text:
            problems.append("响应泄露 %s" % label)
    _record("wxm-2", "GET /health 字段齐全且不泄露 Token/AESKey/CorpID",
            not problems, "；".join(problems) or "ok")


def _check_get_verify(base: str) -> None:
    plain = "qa_echo_%06d" % random.randint(1, 999999)
    url, signature, timestamp, nonce, echostr = _get_verify_url(base, plain)
    problems = []
    try:
        status, content_type, raw = _http("GET", url)
        body = raw.decode("utf-8", "ignore")
        if status != 200:
            problems.append("HTTP %d" % status)
        if "text/plain" not in (content_type or ""):
            problems.append("Content-Type=%r" % content_type)
        if body != plain:
            problems.append("回显=%r 期望 %r" % (body[:80], plain))
        # 独立验证 echostr 本身就是用我们的 key 加密的合法密文
        if _wx_decrypt(echostr).decode("utf-8", "ignore") != plain:
            problems.append("echostr 独立解密不一致")
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-3", "GET /wecom 验签后解密 echostr 返回明文", not problems, "；".join(problems) or "回显一致")


def _check_get_tamper(base: str) -> None:
    plain = "qa_tamper_%06d" % random.randint(1, 999999)
    url, signature, _timestamp, _nonce, echostr = _get_verify_url(base, plain)
    problems = []
    try:
        bad_sig = ("0" if signature[-1] != "0" else "1")
        bad_url = url.replace("msg_signature=" + signature, "msg_signature=" + signature[:-1] + bad_sig)
        status, _ctype, raw = _http("GET", bad_url)
        body = raw.decode("utf-8", "ignore")
        if status != 403:
            problems.append("篡改签名 HTTP %d（期望 403）" % status)
        if "签名校验失败" not in body:
            problems.append("篡改签名文案=%r" % body[:80])
        if plain in body:
            problems.append("篡改签名仍泄露明文")
        # 缺 echostr 参数
        missing = "%s/wecom?msg_signature=%s&timestamp=%s&nonce=%s" % (
            base, signature, _timestamp, _nonce)
        status2, _ct2, raw2 = _http("GET", missing)
        if status2 != 400:
            problems.append("缺参数 HTTP %d（期望 400）" % status2)
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-4", "GET 篡改签名→403；缺参数→400", not problems, "；".join(problems) or "ok")


def _check_unreachable(base: str) -> None:
    problems = []
    try:
        xml_text = _msg_xml("qa_unreach", "查一下我的余额", _new_msg_id("unreach"))
        result = _post_wecom(base, xml_text)
        reply = _parse_reply(result["body"]) if result["status"] == 200 else {}
        if result["status"] != 200:
            problems.append("HTTP %d（期望 200 加密回包）" % result["status"])
        if "text/xml" not in (result["content_type"] or ""):
            problems.append("Content-Type=%r" % result["content_type"])
        if not reply.get("sig_ok"):
            problems.append("回包签名自校验失败")
        if reply.get("content") != UNAVAILABLE_REPLY:
            problems.append("文案=%r" % (reply.get("content") or "")[:80])
        if "Traceback" in (reply.get("content") or "") or "Error" in (reply.get("content") or ""):
            problems.append("文案含堆栈痕迹")
        if result["elapsed"] > 4.5:
            problems.append("耗时 %.2fs 超过 5 秒硬超时" % result["elapsed"])
        detail = "文案=%s 耗时=%.2fs" % (reply.get("content") or "", result["elapsed"])
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
        detail = ""
    _record("wxm-5", "主服务不可达→200 加密回包，友好文案且无堆栈", not problems,
            "；".join(problems) or detail)


def _check_normal_message(base: str, msg_id: str, user: str):
    """wxm-6：合法消息 → 可独立解密/验签，主服务收到正确 body。返回 (reply, problems)。"""
    content = "查一下我的余额"
    xml_text = _msg_xml(user, content, msg_id)
    problems = []
    reply = {}
    try:
        before = _stub_calls()
        result = _post_wecom(base, xml_text)
        after = _stub_calls()
        if result["status"] != 200:
            problems.append("HTTP %d" % result["status"])
            return reply, problems
        reply = _parse_reply(result["body"])
        if not reply.get("sig_ok"):
            problems.append("回包 MsgSignature 独立校验失败")
        if reply.get("outer_tags") != ["Encrypt", "MsgSignature", "Nonce", "TimeStamp"]:
            problems.append("回包外层字段=%r" % reply.get("outer_tags"))
        if reply.get("inner_tags") != ["Content", "CreateTime", "FromUserName", "MsgType", "ToUserName"]:
            problems.append("回包内层字段=%r" % reply.get("inner_tags"))
        if reply.get("content") != "（桩）已收到：%s" % content:
            problems.append("Content=%r" % (reply.get("content") or "")[:100])
        if reply.get("to_user") != user:
            problems.append("ToUserName=%r" % reply.get("to_user"))
        if reply.get("from_user") != CORP_ID:
            problems.append("FromUserName=%r" % reply.get("from_user"))
        if reply.get("msg_type") != "text":
            problems.append("MsgType=%r" % reply.get("msg_type"))
        if not str(reply.get("create_time") or "").isdigit():
            problems.append("CreateTime=%r" % reply.get("create_time"))
        if after["chat"] != before["chat"] + 1:
            problems.append("主服务 /api/chat 调用 %d→%d（期望 +1）" % (before["chat"], after["chat"]))
        rows = _stub_rows("/api/chat")
        last = rows[-1]["body"] if rows else {}
        if last.get("session_id") != "sess_fake_001":
            problems.append("/api/chat body.session_id=%r" % last.get("session_id"))
        if last.get("text") != content:
            problems.append("/api/chat body.text=%r" % last.get("text"))
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    return reply, problems


def _check_dedupe(base: str, msg_id: str, first_reply: dict, user: str) -> None:
    problems = []
    try:
        before = _stub_calls()
        xml_text = _msg_xml(user, "查一下我的账单", msg_id)   # 同一 MsgId，故意换文案
        result = _post_wecom(base, xml_text)
        after = _stub_calls()
        reply = _parse_reply(result["body"]) if result["status"] == 200 else {}
        if result["status"] != 200:
            problems.append("HTTP %d" % result["status"])
        if reply.get("content") != first_reply.get("content"):
            problems.append("重发回包=%r（期望复读 %r）" % (
                (reply.get("content") or "")[:60], (first_reply.get("content") or "")[:60]))
        if after["chat"] != before["chat"]:
            problems.append("同 MsgId 重发又调用主服务 %d→%d" % (before["chat"], after["chat"]))
        health = _health(base)
        if int(health.get("dedupe_hits") or 0) < 1:
            problems.append("dedupe_hits=%r" % health.get("dedupe_hits"))
        if int(health.get("dedupe_size") or 0) < 1:
            problems.append("dedupe_size=%r" % health.get("dedupe_size"))
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-7", "同 MsgId 重发→回包复读且主服务调用次数不增", not problems,
            "；".join(problems) or "chat 调用未增加")


def _check_confirmation(base: str) -> tuple:
    """wxm-8：needs_confirmation 摘要 +「确认」转发 /api/approval。返回 (awaiting_reply, confirm_reply, problems)。"""
    problems = []
    awaiting_reply = {}
    confirm_reply = {}
    user = "qa_confirm"
    try:
        _set_stub_mode("awaiting")
        msg_id = _new_msg_id("await")
        result = _post_wecom(base, _msg_xml(user, "给妈妈转 800 元", msg_id))
        awaiting_reply = _parse_reply(result["body"]) if result["status"] == 200 else {}
        content = awaiting_reply.get("content") or ""
        if result["status"] != 200:
            problems.append("HTTP %d" % result["status"])
        if "【需要二次确认】" not in content:
            problems.append("缺二次确认标题：%r" % content[:100])
        if PREVIEW not in content:
            problems.append("缺预览文案：%r" % content[:100])
        if HINT not in content:
            problems.append("缺确认提示：%r" % content[:100])
        _set_stub_mode("ok")
        before = _stub_calls()
        result2 = _post_wecom(base, _msg_xml(user, "确认", _new_msg_id("confirm")))
        confirm_reply = _parse_reply(result2["body"]) if result2["status"] == 200 else {}
        after = _stub_calls()
        content2 = confirm_reply.get("content") or ""
        if "已确认执行转账" not in content2:
            problems.append("确认回包=%r" % content2[:100])
        if after["approval"] != before["approval"] + 1:
            problems.append("/api/approval 调用 %d→%d" % (before["approval"], after["approval"]))
        rows = _stub_rows("/api/approval")
        last = rows[-1]["body"] if rows else {}
        if last.get("decision") != "approve":
            problems.append("decision=%r" % last.get("decision"))
        if last.get("approval_id") != "ap_secret_001":
            problems.append("approval_id 转发=%r" % last.get("approval_id"))
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
        _set_stub_mode("ok")
    return awaiting_reply, confirm_reply, problems


def _check_no_secrets(replies: list) -> None:
    problems = []
    forbidden = ["approval_id", "thread_id", "confirmation.token"]
    secrets = ["ap_secret_001", "thread_secret_001", "nonce_secret_001", "sess_fake_001"]
    try:
        for index, reply in enumerate(replies):
            if not reply:
                problems.append("第 %d 个回包为空" % (index + 1))
                continue
            content = (reply.get("content") or "").lower()
            inner = (reply.get("inner_text") or "")
            for word in forbidden:
                if word in content:
                    problems.append("第 %d 个回包内容含 %s" % (index + 1, word))
            for secret in secrets:
                if secret.lower() in inner.lower():
                    problems.append("第 %d 个回包含内部值 %s" % (index + 1, secret))
            if reply.get("outer_tags") != ["Encrypt", "MsgSignature", "Nonce", "TimeStamp"]:
                problems.append("第 %d 个回包外层字段=%r" % (index + 1, reply.get("outer_tags")))
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-9", "回包不含 approval_id/thread_id/confirmation.token 等内部凭据",
            not problems, "；".join(problems) or "三个回包均通过白名单校验")


def _check_malformed(base: str) -> None:
    problems = []
    try:
        # 1) 坏 XML
        status, _ctype, raw = _http("POST", base + "/wecom", b"<xml>broken")
        body = raw.decode("utf-8", "ignore")
        if status != 400:
            problems.append("坏 XML HTTP %d（期望 400）" % status)
        if "请求体不是合法 XML" not in body:
            problems.append("坏 XML 文案=%r" % body[:80])
        # 2) 签名错误
        result = _post_wecom(base, _msg_xml("qa_badsig", "查余额", _new_msg_id("bad")), sign=False)
        body2 = result["body"].decode("utf-8", "ignore")
        if result["status"] != 403:
            problems.append("坏签名 HTTP %d（期望 403）" % result["status"])
        if "签名校验失败" not in body2:
            problems.append("坏签名文案=%r" % body2[:80])
        # 3) CorpID 不符（签名合法但密文尾是别家企业）
        result2 = _post_wecom(base, _msg_xml("qa_wrongcorp", "查余额", _new_msg_id("corp")),
                              enc_corp="ww_other_corp_qa")
        body3 = result2["body"].decode("utf-8", "ignore")
        if result2["status"] != 400:
            problems.append("CorpID 不符 HTTP %d（期望 400）" % result2["status"])
        if "消息解密失败" not in body3:
            problems.append("CorpID 不符文案=%r" % body3[:80])
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-10", "非法报文：坏 XML 400 / 坏签名 403 / CorpID 不符 400",
            not problems, "；".join(problems) or "ok")


def _check_non_text(base: str) -> None:
    problems = []
    try:
        xml_text = _msg_xml("qa_image", "", _new_msg_id("img"), msg_type="image")
        result = _post_wecom(base, xml_text)
        reply = _parse_reply(result["body"]) if result["status"] == 200 else {}
        if result["status"] != 200:
            problems.append("HTTP %d" % result["status"])
        if "暂时只支持文本消息" not in (reply.get("content") or ""):
            problems.append("Content=%r" % (reply.get("content") or "")[:100])
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-11", "非文本消息→200 友好提示", not problems, "；".join(problems) or "ok")


def _check_timeout(base: str) -> None:
    problems = []
    elapsed = 0.0
    try:
        _set_stub_mode("slow")
        with _STUB_STATE["lock"]:
            _STUB_STATE["delay"] = 2.5
        user = "qa_timeout"
        result = _post_wecom(base, _msg_xml(user, "查一下我的余额", _new_msg_id("slow")))
        elapsed = result["elapsed"]
        reply = _parse_reply(result["body"]) if result["status"] == 200 else {}
        if result["status"] != 200:
            problems.append("HTTP %d" % result["status"])
        if reply.get("content") != TIMEOUT_REPLY:
            problems.append("文案=%r" % (reply.get("content") or "")[:100])
        if elapsed >= 2.2:
            problems.append("耗时 %.2fs 未按 WECOM_TIMEOUT=1 降级" % elapsed)
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    finally:
        _set_stub_mode("ok")
    _record("wxm-12", "转发超时：WECOM_TIMEOUT=1 + 主服务慢 2.5s → 200「处理超时」",
            not problems, "；".join(problems) or "耗时 %.2fs" % elapsed)


def _check_cleanup(ports: list, logs: list) -> None:
    problems = []
    try:
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if not any(_listening_pids(port) for port in ports):
                break
            time.sleep(0.3)
        for port in ports:
            pids = _listening_pids(port)
            if pids:
                problems.append("端口 %d 仍有 LISTENING pid=%s" % (port, ",".join(pids)))
        for log_name in logs:
            text = _tail(WORK / log_name, 50)
            if "Traceback" in text:
                problems.append("%s 含 Traceback" % log_name)
    except Exception as exc:  # noqa: BLE001
        problems.append("异常：%s" % exc)
    _record("wxm-13", "清理：进程退出、端口无 LISTENING 残留、日志无 Traceback",
            not problems, "；".join(problems) or "端口 %s 均空闲" % ",".join(str(p) for p in ports))


# ======================= 主流程 =======================
def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = list(argv if argv is not None else sys.argv[1:])
    want_json = "--json" in args
    print("== ZeroStep v%s 企业微信回调适配器 e2e ==" % _repo_version(), flush=True)
    print("独立凭据：Token=%s… CorpID=%s AESKey=%s…" % (TOKEN[:10], CORP_ID, AES_KEY[:8]), flush=True)

    global STUB, MAIN_URL, WECOM_BASE, TIMEOUT_BASE, KEY
    env_error = ""
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        _prepare_app()
        _load_crypto()
        KEY = _derive_key(AES_KEY)
    except Exception as exc:  # noqa: BLE001
        print("ERROR 环境准备失败：%s: %s" % (type(exc).__name__, exc), flush=True)
        return 2

    # wxm-1 独立密码学自检
    kat_fails = _crypto_kat()
    _record("wxm-1", "AES/PKCS7/密钥派生/签名独立自检（FIPS-197 KAT）",
            not kat_fails, "；".join(kat_fails) or "FIPS-197 Appendix B/C 均命中")

    try:
        main_port = _free_port(MAIN_PORT_PREF)
        wecom_port = _free_port(WECOM_PORT_PREF)
        timeout_port = _free_port(TIMEOUT_PORT_PREF)
        MAIN_URL = "http://%s:%d" % (HOST, main_port)
        WECOM_BASE = "http://%s:%d" % (HOST, wecom_port)
        TIMEOUT_BASE = "http://%s:%d" % (HOST, timeout_port)
        print("端口：企微主 %d / 超时实例 %d / 假主服务 %d" % (wecom_port, timeout_port, main_port), flush=True)

        _start_wecom(wecom_port, "5", "wecom-main.log")
        _wait_http(WECOM_BASE + "/health")

        # 阶段 A：主服务未起（不可达）
        _check_health(WECOM_BASE, wecom_port)
        _check_get_verify(WECOM_BASE)
        _check_get_tamper(WECOM_BASE)
        _check_unreachable(WECOM_BASE)

        # 阶段 B：起假主服务，跑正常/去重/两步确认
        STUB = _StubServer(main_port)
        STUB.start()
        _wait_http(MAIN_URL + "/health")

        msg_id = _new_msg_id("normal")
        normal_reply, normal_problems = _check_normal_message(WECOM_BASE, msg_id, "qa_main")
        _record("wxm-6", "POST 合法消息→回包可独立解密/验签，主服务收到正确 body",
                not normal_problems, "；".join(normal_problems) or "签名/结构/回包/转发 body 均正确")

        _check_dedupe(WECOM_BASE, msg_id, normal_reply, "qa_main")

        awaiting_reply, confirm_reply, confirm_problems = _check_confirmation(WECOM_BASE)
        _record("wxm-8", "两步确认：needs_confirmation 摘要 +「确认」转发 /api/approval",
                not confirm_problems, "；".join(confirm_problems) or "确认链路 1 次 /api/approval")

        _check_no_secrets([normal_reply, awaiting_reply, confirm_reply])
        _check_malformed(WECOM_BASE)
        _check_non_text(WECOM_BASE)

        # 阶段 C：停主实例，起 WECOM_TIMEOUT=1 的超时实例
        _stop_all()
        STUB = _StubServer(main_port)
        STUB.start()
        _wait_http(MAIN_URL + "/health")
        _start_wecom(timeout_port, "1", "wecom-timeout.log")
        _wait_http(TIMEOUT_BASE + "/health")
        _check_timeout(TIMEOUT_BASE)
        _stop_all()

        _check_cleanup([wecom_port, timeout_port, main_port],
                       ["wecom-main.log", "wecom-timeout.log"])
    except Exception as exc:  # noqa: BLE001
        env_error = "%s: %s" % (type(exc).__name__, exc)
        print("ERROR 运行期异常：%s" % env_error, flush=True)
        for proc, _handle, name in list(PROCS):
            if proc.poll() is not None:
                print("---- %s 日志尾部 ----\n%s" % (name, _tail(WORK / name)), flush=True)
    finally:
        _stop_all()

    # 补充证据：产品自测（Lead 明确不作为验收依据，仅打印 INFO）
    try:
        env = _base_env()
        env["WECOM_PORT"] = str(_free_port(0))
        completed = subprocess.run(
            [PYTHON, str(APP / "backend" / "channels" / "wecom.py"), "--selftest"],
            cwd=str(APP), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        tail = [line for line in (completed.stdout or "").splitlines() if line.strip()]
        print("INFO  wecom.py --selftest：%s（不作为验收依据）" % (tail[-1] if tail else "无输出"), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("INFO  wecom.py --selftest 未运行：%s" % exc, flush=True)

    passed = sum(1 for item in RESULTS if item["ok"])
    failed = [item for item in RESULTS if not item["ok"]]
    print("", flush=True)
    for item in failed:
        print("FAIL  %s %s | %s" % (item["id"], item["desc"], item["detail"]), flush=True)
    print("结果：%d/%d 通过" % (passed, len(RESULTS)), flush=True)
    if want_json:
        print(json.dumps({"checks": RESULTS, "passed": passed, "total": len(RESULTS)},
                         ensure_ascii=False), flush=True)
    if env_error:
        return 2
    return 0 if passed == len(RESULTS) and passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
