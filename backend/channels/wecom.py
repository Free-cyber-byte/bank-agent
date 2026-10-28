"""企业微信回调适配器（v0.3.2，零依赖标准库）。

把企业微信「回调模式」的加解密/验签/去重/被动回包适配成 ZeroStep 的渠道层调用：
- GET  /wecom：官方 URL 验证，验签后解密 echostr，返回纯文本明文；
- POST /wecom：验签 + AES 解密 XML，取 Content 后转发主服务，被动加密回包；
- 渠道层只做协议适配，不做任何业务判定；转发复用同目录 server.py 的
  会话映射、幂等缓存、两步确认与 messages.jsonl 日志；
- 绝不向企业微信回传 approval_id / nonce / thread_id / confirmation.token 等内部凭据；
- 转发主服务 5 秒硬超时（WECOM_TIMEOUT 可调），超时/不可达一律回可读中文文案。

启动：
    python backend/channels/wecom.py                 # 默认 127.0.0.1:8401
    python backend/channels/wecom.py --port 8401 --host 127.0.0.1
    python backend/channels/wecom.py --selftest      # 全离线自测（9 项）

环境变量（每次请求现读，配合 importlib.reload 可热切换）：
    WECOM_ENABLED / WECOM_PORT / WECOM_TOKEN / WECOM_AES_KEY / WECOM_CORP_ID /
    WECOM_TIMEOUT / WECOM_DEDUPE_SECONDS；WECOM_TOKEN 为空时回退 CHANNEL_TOKEN。
主服务地址优先级：ZEROSTEP_MAIN_URL > MAIN_URL > ZEROSTEP_API_URL > http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

BASE = Path(__file__).resolve().parent
BACKEND = BASE.parent


def _prepend(path: str) -> None:
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


_prepend(str(BACKEND))
_prepend(str(BASE))

from app import config  # noqa: E402
import server as channel_server  # noqa: E402  同目录渠道层：复用会话/幂等/两步确认/日志
import wecom_crypto  # noqa: E402

UNAVAILABLE_REPLY = "渠道服务暂时不可用，请稍后再试"
TIMEOUT_REPLY = "处理超时，请稍后再试"
FALLBACK_REPLY = "服务暂时不可用，请稍后再试"
DEFAULT_PORT = 8401
DEFAULT_TIMEOUT = 5.0
DEFAULT_DEDUPE_SECONDS = 300
MAX_BODY = 1024 * 1024
_PLACEHOLDER = "__WECOM_PLACEHOLDER__"

_DEDUPE_LOCK = threading.Lock()
_DEDUPE: Dict[str, Tuple[float, str]] = {}
_DEDUPE_HITS = 0
_PORT = 0
_TS = str(int(time.time()))


# ---------------- 配置读取（每次现读，支持 importlib.reload） ----------------
def _cfg(name: str, default: Any = None) -> Any:
    return getattr(config, name, default)


def _enabled() -> bool:
    return bool(_cfg("WECOM_ENABLED", True))


def _token() -> str:
    """回调验签 Token：WECOM_TOKEN 优先，未配置时回退 CHANNEL_TOKEN。"""
    primary = str(_cfg("WECOM_TOKEN", "") or "").strip()
    return primary or str(_cfg("CHANNEL_TOKEN", "") or "").strip()


def _aes_key() -> str:
    return str(_cfg("WECOM_AES_KEY", "") or "").strip()


def _corp_id() -> str:
    return str(_cfg("WECOM_CORP_ID", "") or "").strip()


def _port() -> int:
    try:
        return int(_cfg("WECOM_PORT", DEFAULT_PORT) or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _timeout() -> float:
    try:
        value = float(_cfg("WECOM_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _dedupe_seconds() -> int:
    try:
        value = int(_cfg("WECOM_DEDUPE_SECONDS", DEFAULT_DEDUPE_SECONDS) or DEFAULT_DEDUPE_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_DEDUPE_SECONDS
    return max(1, value)


def _now_iso() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 - 时间格式化失败不影响主流程
        return time.strftime("%Y-%m-%dT%H:%M:%S")


def _configured() -> bool:
    return bool(_token() and _aes_key() and _corp_id())


# ---------------- 去重（MsgId 5 分钟窗口） ----------------
def _dedupe_prune(now: Optional[float] = None) -> None:
    deadline = (now if now is not None else time.time()) - _dedupe_seconds()
    stale = [key for key, (ts, _) in _DEDUPE.items() if ts < deadline]
    for key in stale:
        _DEDUPE.pop(key, None)


def _dedupe_get(msg_id: str) -> Optional[str]:
    if not msg_id:
        return None
    global _DEDUPE_HITS
    with _DEDUPE_LOCK:
        _dedupe_prune()
        hit = _DEDUPE.get(msg_id)
        if hit:
            _DEDUPE_HITS += 1
            return hit[1]
        return None


def _dedupe_put(msg_id: str, reply: str) -> None:
    if not msg_id:
        return
    with _DEDUPE_LOCK:
        _dedupe_prune()
        if len(_DEDUPE) > 2000:
            _DEDUPE.clear()
        _DEDUPE[msg_id] = (time.time(), str(reply or ""))


def dedupe_size() -> int:
    with _DEDUPE_LOCK:
        return len(_DEDUPE)


def clear_dedupe() -> None:
    global _DEDUPE_HITS
    with _DEDUPE_LOCK:
        _DEDUPE.clear()
        _DEDUPE_HITS = 0


def dedupe_hits() -> int:
    """去重命中次数（供 /health 与自测观测；被动回包本身没有 cached 字段）。"""
    with _DEDUPE_LOCK:
        return _DEDUPE_HITS


# ---------------- 转发主服务（复用渠道层，5 秒硬超时） ----------------
def _reply_text(result: Dict[str, Any]) -> str:
    """把渠道层白名单结果转成一条可读文本；绝不拼接内部凭据字段。"""
    if not isinstance(result, dict):
        return UNAVAILABLE_REPLY
    status = str(result.get("status") or "")
    if result.get("needs_confirmation"):
        preview = str(result.get("preview") or "").strip()
        hint = str(result.get("hint") or "").strip()
        lines = ["【需要二次确认】"]
        if preview:
            lines.append(preview)
        if hint:
            lines.append(hint)
        return "\n".join(lines)
    text = str(result.get("reply") or "").strip()
    if text:
        return text
    if status == "error":
        return UNAVAILABLE_REPLY
    return UNAVAILABLE_REPLY


def _forward_to_main(user_id: str, text: str) -> Dict[str, Any]:
    """带硬超时地调用渠道层 handle_message（会话/幂等/两步确认/日志全部复用）。"""
    timeout = _timeout()
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = channel_server.handle_message(user_id, text)
        except Exception:  # noqa: BLE001 - 渠道层不把内部异常抛给企业微信
            try:
                box["result"] = channel_server._unavailable_response()
            except Exception:  # noqa: BLE001
                box["result"] = {"reply": UNAVAILABLE_REPLY, "status": "error"}

    thread = threading.Thread(target=worker, name="wecom-forward", daemon=True)
    thread.start()
    thread.join(timeout)
    result = box.get("result")
    if result is None:
        return {"reply": TIMEOUT_REPLY, "status": "timeout"}
    return result


# ---------------- XML 工具 ----------------
def _xml_text(root: Any, tag: str) -> str:
    try:
        return str(root.findtext(tag) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _cdata(text: str) -> str:
    return str(text or "").replace("]]>", "]]]]><![CDATA[>")


def build_text_reply_xml(to_user: str, from_user: str, content: str, create_time: Optional[int] = None) -> str:
    """构造明文被动回复 XML（text 类型）。"""
    stamp = int(create_time if create_time is not None else time.time())
    return (
        "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
        "<FromUserName><![CDATA[%s]]></FromUserName>"
        "<CreateTime>%d</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[%s]]></Content></xml>"
    ) % (_cdata(to_user), _cdata(from_user), stamp, _cdata(content))


def _new_nonce() -> str:
    return ("%d%06d" % (int(time.time()), int((time.time() * 1000000) % 1000000)))[:16]


# ---------------- 验签 / 解密 ----------------
def _verify(token: str, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
    if not (token and timestamp and nonce and encrypt and signature):
        return False
    try:
        return wecom_crypto.verify_signature(token, timestamp, nonce, encrypt, signature)
    except Exception:  # noqa: BLE001
        return False


def _decrypt_message(encrypt: str) -> str:
    """解密 XML 中的 Encrypt，返回明文消息 XML 文本。"""
    raw = wecom_crypto.wecom_decrypt(encrypt, _corp_id(), _aes_key())
    return raw.decode("utf-8", "ignore")


def _encrypt_reply(plain_xml: str) -> str:
    """加密被动回复，返回完整外层 XML。"""
    token = _token()
    timestamp = str(int(time.time()))
    nonce = _new_nonce()
    body, _encrypt = wecom_crypto.wrap_reply(plain_xml, _corp_id(), _aes_key(), token, timestamp, nonce)
    return body


# ---------------- 业务主流程（不含业务判定） ----------------
def handle_wecom_message(msg_xml: str) -> str:
    """处理一条解密后的企业微信消息 XML，返回明文被动回复 XML。"""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(msg_xml)
    to_user = _xml_text(root, "ToUserName")
    from_user = _xml_text(root, "FromUserName")
    msg_type = _xml_text(root, "MsgType").lower()
    content = _xml_text(root, "Content")
    msg_id = _xml_text(root, "MsgId")

    if msg_type != "text":
        reply = "暂时只支持文本消息，请直接回复文字指令（例如：查一下我的余额）。"
    elif not content:
        reply = "消息内容为空，请直接回复文字指令。"
    else:
        hit = _dedupe_get(msg_id)
        if hit is not None:
            reply = hit
        else:
            result = _forward_to_main(from_user or "wecom_user", content)
            reply = _reply_text(result)
            _dedupe_put(msg_id, reply)
    reply_to = from_user or to_user or "wecom_user"
    reply_from = to_user or _corp_id() or "ZeroStep"
    return build_text_reply_xml(reply_to, reply_from, reply, int(time.time()))


# ---------------- HTTP 入口 ----------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ZeroStepWeCom/" + str(getattr(config, "VERSION", "1.0.0"))

    def log_message(self, fmt, *args):  # 安静一点，只保留 4xx/5xx
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("%s - %s " % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _text(self, status: int, text: str) -> None:
        self._send(status, str(text or "").encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _guards(self) -> Optional[str]:
        if not _enabled():
            self._text(503, "企业微信渠道已停用")
            return "disabled"
        if not _configured():
            self._text(503, "企业微信渠道未配置（缺少 Token / EncodingAESKey / CorpID）")
            return "missing_config"
        return None

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/health":
                return self._json(200, health())
            if path != "/wecom":
                return self._text(404, "NOT_FOUND")
            if self._guards() is not None:
                return
            query = parse_qs(parsed.query)
            signature = (query.get("msg_signature") or [""])[0].strip()
            timestamp = (query.get("timestamp") or [""])[0].strip()
            nonce = (query.get("nonce") or [""])[0].strip()
            echostr = (query.get("echostr") or [""])[0].strip()
            if not (signature and timestamp and nonce and echostr):
                return self._text(400, "缺少 msg_signature / timestamp / nonce / echostr 参数")
            if not _verify(_token(), timestamp, nonce, echostr, signature):
                return self._text(403, "签名校验失败")
            plain = wecom_crypto.decode_echostr(echostr, _corp_id(), _aes_key())
            return self._text(200, plain)
        except Exception:  # noqa: BLE001 - 回调入口绝不向企业微信抛堆栈
            return self._text(500, "服务暂时不可用")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path != "/wecom":
            return self._text(404, "NOT_FOUND")
        try:
            if self._guards() is not None:
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return self._text(400, "请求体为空")
            if length > MAX_BODY:
                return self._text(413, "请求体过大")
            raw = self.rfile.read(length)
            query = parse_qs(parsed.query)
            signature = (query.get("msg_signature") or [""])[0].strip()
            timestamp = (query.get("timestamp") or [""])[0].strip()
            nonce = (query.get("nonce") or [""])[0].strip()
            import xml.etree.ElementTree as ET

            try:
                root = ET.fromstring(raw.decode("utf-8", "ignore"))
            except ET.ParseError:
                return self._text(400, "请求体不是合法 XML")
            encrypt = _xml_text(root, "Encrypt")
            if not encrypt:
                return self._text(400, "XML 缺少 Encrypt 节点")
            if not _verify(_token(), timestamp, nonce, encrypt, signature):
                return self._text(403, "签名校验失败")
            try:
                msg_xml = _decrypt_message(encrypt)
            except ValueError:
                return self._text(400, "消息解密失败")
            try:
                reply_xml = handle_wecom_message(msg_xml)
            except Exception:  # noqa: BLE001 - 内部异常降级成可读文案
                reply_xml = build_text_reply_xml("wecom_user", _corp_id() or "ZeroStep", FALLBACK_REPLY)
            return self._send(200, _encrypt_reply(reply_xml).encode("utf-8"), "text/xml; charset=utf-8")
        except Exception:  # noqa: BLE001 - 任何异常都不能把堆栈抛给渠道方
            try:
                reply_xml = build_text_reply_xml("wecom_user", _corp_id() or "ZeroStep", FALLBACK_REPLY)
                return self._send(200, _encrypt_reply(reply_xml).encode("utf-8"), "text/xml; charset=utf-8")
            except Exception:  # noqa: BLE001
                return self._text(200, "success")


class WeComServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str = "127.0.0.1", port: Optional[int] = None) -> WeComServer:
    """创建企业微信回调服务（port=0 时由系统分配，便于自测）。"""
    global _PORT
    actual_port = _port() if port is None else int(port)
    httpd = WeComServer((host, actual_port), _Handler)
    _PORT = int(httpd.server_address[1])
    return httpd


def health() -> Dict[str, Any]:
    """健康检查：只暴露配置是否就绪，绝不返回 Token / AESKey / CorpID 明文。"""
    return {
        "ok": True,
        "service": "wecom",
        "enabled": _enabled(),
        "token_set": bool(_token()),
        "aes_key_set": bool(_aes_key()),
        "corp_id_set": bool(_corp_id()),
        "dedupe_size": dedupe_size(),
        "dedupe_hits": dedupe_hits(),
        "port": int(_PORT or _port()),
        "main_url": channel_server._main_url(),
        "ts": _now_iso(),
    }


# ---------------- 自测（全离线，9 项） ----------------
def _http_request(method: str, url: str, body: Optional[bytes] = None,
                  timeout: float = 5.0) -> Tuple[int, str, str]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8", "ignore")


def _run_selftest() -> int:
    """全离线自测：AES/PKCS7/报文/验签/GET/POST/去重/不可达/超时。返回 0 表示全通过。"""
    results: list = []

    def check(name: str, fn) -> None:
        started = time.time()
        try:
            fn()
            elapsed = (time.time() - started) * 1000
            results.append(True)
            print("PASS  %s  (%.1f ms)" % (name, elapsed))
        except Exception as exc:  # noqa: BLE001 - 自测把失败原因打出来即可
            elapsed = (time.time() - started) * 1000
            results.append(False)
            print("FAIL  %s  (%.1f ms)  | %s: %s" % (name, elapsed, type(exc).__name__, exc))

    import base64

    tmp_dir = tempfile.mkdtemp(prefix="wecom_selftest_")
    token = "ZeroStepSelfTestToken"
    corp_id = "ww_selftest_corp_0001"
    aes_key = base64.b64encode(os.urandom(32)).decode("ascii")[:43]
    config.WECOM_TOKEN = token
    config.WECOM_AES_KEY = aes_key
    config.WECOM_CORP_ID = corp_id
    config.WECOM_ENABLED = True
    config.WECOM_TIMEOUT = 2.0
    config.CHANNEL_SESSIONS_FILE = Path(tmp_dir) / "sessions.json"
    config.CHANNEL_MESSAGE_LOG = Path(tmp_dir) / "messages.jsonl"
    channel_server._SESSIONS = None
    channel_server._PENDING.clear()
    channel_server._CACHE.clear()
    clear_dedupe()

    # FIPS-197 Appendix C：key=000102...0f，明文 00112233...eeff -> 密文 69c4e0d8...c55a
    appendix_c_key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    appendix_c_plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    appendix_c_cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    # FIPS-197 Appendix B：key=2b7e1516...f3c，明文 3243f6a8...0734 -> 密文 3925841d...0b32
    appendix_b_key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    appendix_b_plain = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
    appendix_b_cipher = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")

    def case_aes() -> None:
        got_c = wecom_crypto.encrypt_block(appendix_c_key, appendix_c_plain)
        assert got_c == appendix_c_cipher, "FIPS-197 Appendix C 加密不一致：%s" % got_c.hex()
        back_c = wecom_crypto.decrypt_block(appendix_c_key, appendix_c_cipher)
        assert back_c == appendix_c_plain, "FIPS-197 Appendix C 解密不一致"
        got_b = wecom_crypto.encrypt_block(appendix_b_key, appendix_b_plain)
        assert got_b == appendix_b_cipher, "FIPS-197 Appendix B 加密不一致：%s" % got_b.hex()
        back_b = wecom_crypto.decrypt_block(appendix_b_key, appendix_b_cipher)
        assert back_b == appendix_b_plain, "FIPS-197 Appendix B 解密不一致"

    def case_pkcs7() -> None:
        for size in (0, 1, 15, 16, 31, 32, 33, 64):
            data = bytes(range(size))
            padded = wecom_crypto.pkcs7_pad(data, 32)
            assert len(padded) % 32 == 0, "补位后长度非法"
            assert wecom_crypto.pkcs7_unpad(padded, 32) == data, "去补位结果不一致 size=%d" % size
            assert padded[-1] == len(padded) - size, "补位字节数值非法 size=%d" % size
        try:
            wecom_crypto.pkcs7_unpad(b"\x01\x02\x03\x04", 32)
        except ValueError:
            pass
        else:
            raise AssertionError("非法补位未被拒绝")

    def case_envelope() -> None:
        message = "<xml>你好 ZeroStep</xml>".encode("utf-8")
        encrypt = wecom_crypto.wecom_encrypt(message, corp_id, aes_key, random16=b"\x07" * 16)
        assert isinstance(encrypt, str) and encrypt
        assert wecom_crypto.wecom_decrypt(encrypt, corp_id, aes_key) == message, "封装/解封装往返不一致"
        try:
            wecom_crypto.wecom_decrypt(encrypt, "ww_wrong_corp", aes_key)
        except ValueError:
            pass
        else:
            raise AssertionError("corp_id 不一致未被拒绝")

    def case_signature() -> None:
        encrypt = wecom_crypto.wecom_encrypt(b"<xml/>", corp_id, aes_key)
        ts, nonce = "1700000000", "nonce_abc"
        signature = wecom_crypto.wecom_signature(token, ts, nonce, encrypt)
        assert wecom_crypto.verify_signature(token, ts, nonce, encrypt, signature) is True
        assert wecom_crypto.verify_signature(token, ts, "nonce_tampered", encrypt, signature) is False
        expected = __import__("hashlib").sha1(
            "".join(sorted([token, ts, nonce, encrypt])).encode("utf-8")).hexdigest()
        assert signature == expected, "签名算法不是 sha1(sorted 拼接)"

    httpd = make_server("127.0.0.1", 0)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, name="wecom-selftest", daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:%d" % port

    def build_encrypted_query(message_xml: str, msg_id: str, stamp: str = "1700000000",
                              nonce: str = "nonce123") -> str:
        encrypt = wecom_crypto.wecom_encrypt(message_xml.encode("utf-8"), corp_id, aes_key)
        signature = wecom_crypto.wecom_signature(token, stamp, nonce, encrypt)
        return "%s/wecom?msg_signature=%s&timestamp=%s&nonce=%s&encrypt=%s" % (
            base_url, signature, stamp, nonce, quote(encrypt))

    def case_get_verify() -> None:
        echo_plain = "echo_plain_text_2026"
        echostr = wecom_crypto.wecom_encrypt(echo_plain.encode("utf-8"), corp_id, aes_key)
        stamp, nonce = "1700000001", "nonce_get"
        signature = wecom_crypto.wecom_signature(token, stamp, nonce, echostr)
        url = "%s/wecom?msg_signature=%s&timestamp=%s&nonce=%s&echostr=%s" % (
            base_url, signature, stamp, nonce, quote(echostr))
        status, content_type, body = _http_request("GET", url)
        assert status == 200, "GET 验签应 200，实际 %d" % status
        assert "text/plain" in content_type, "GET 响应类型应为 text/plain，实际 %s" % content_type
        assert body == echo_plain, "GET 回显内容不一致：%r" % body
        bad_url = url.replace("nonce_get", "nonce_bad")
        status, _, _ = _http_request("GET", bad_url)
        assert status == 403, "篡改 nonce 后应 403，实际 %d" % status

    def build_msg_xml(from_user: str, content: str, msg_id: str) -> str:
        return (
            "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
            "<FromUserName><![CDATA[%s]]></FromUserName>"
            "<CreateTime>1700000002</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            "<Content><![CDATA[%s]]></Content>"
            "<MsgId>%s</MsgId><AgentID>1000002</AgentID></xml>"
        ) % (corp_id, from_user, content, msg_id)

    def post_encrypted(message_xml: str, stamp: str = "1700000003", nonce: str = "nonce_post") -> Tuple[int, str, str]:
        encrypt = wecom_crypto.wecom_encrypt(message_xml.encode("utf-8"), corp_id, aes_key)
        signature = wecom_crypto.wecom_signature(token, stamp, nonce, encrypt)
        url = "%s/wecom?msg_signature=%s&timestamp=%s&nonce=%s" % (base_url, signature, stamp, nonce)
        body = ("<xml><Encrypt><![CDATA[%s]]></Encrypt></xml>" % encrypt).encode("utf-8")
        return _http_request("POST", url, body=body)

    def decrypt_reply(body: str) -> str:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(body)
        encrypt = _xml_text(root, "Encrypt")
        assert encrypt, "被动回复缺少 Encrypt"
        inner = wecom_crypto.wecom_decrypt(encrypt, corp_id, aes_key).decode("utf-8", "ignore")
        return inner

    real_handle = channel_server.handle_message

    def case_post_decrypt() -> None:
        fake = lambda user_id, text: {"reply": "已收到：%s" % text, "status": "ok", "cached": False}
        channel_server.handle_message = fake
        try:
            status, content_type, body = post_encrypted(build_msg_xml("zhangsan", "查一下我的余额", "msg_10001"))
            assert status == 200, "POST 应 200，实际 %d" % status
            assert "text/xml" in content_type, "POST 响应类型应为 text/xml，实际 %s" % content_type
            inner = decrypt_reply(body)
            import xml.etree.ElementTree as ET
            root = ET.fromstring(inner)
            assert _xml_text(root, "Content") == "已收到：查一下我的余额", "被动回复内容不一致：%r" % _xml_text(root, "Content")
            assert _xml_text(root, "ToUserName") == "zhangsan", "被动回复 ToUserName 应为提问用户"
        finally:
            channel_server.handle_message = real_handle

    def case_dedupe() -> None:
        calls = {"n": 0}

        def fake(user_id: str, text: str) -> Dict[str, Any]:
            calls["n"] += 1
            return {"reply": "去重回复-%d" % calls["n"], "status": "ok", "cached": False}

        channel_server.handle_message = fake
        try:
            url_msg = build_msg_xml("lisi", "查一下我的余额", "msg_20002")
            status1, _, body1 = post_encrypted(url_msg)
            status2, _, body2 = post_encrypted(url_msg)
            assert status1 == 200 and status2 == 200
            content1 = _xml_text(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring(decrypt_reply(body1)), "Content")
            content2 = _xml_text(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring(decrypt_reply(body2)), "Content")
            assert content1 == content2, "同一 MsgId 两次回复不一致：%r vs %r" % (content1, content2)
            assert calls["n"] == 1, "同一 MsgId 重复转发主服务 %d 次（应为 1）" % calls["n"]
            assert dedupe_hits() == 1, "去重命中计数应为 1，实际 %d" % dedupe_hits()
        finally:
            channel_server.handle_message = real_handle

    def case_unreachable() -> None:
        old_main = os.environ.get("ZEROSTEP_MAIN_URL")
        old_timeout = config.WECOM_TIMEOUT
        os.environ["ZEROSTEP_MAIN_URL"] = "http://127.0.0.1:9"
        config.WECOM_TIMEOUT = 3.0
        try:
            status, _, body = post_encrypted(build_msg_xml("wangwu", "查一下我的余额", "msg_30003"))
            assert status == 200, "不可达时应仍返回 200 被动回复，实际 %d" % status
            content = _xml_text(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring(decrypt_reply(body)), "Content")
            assert content == UNAVAILABLE_REPLY, "不可达文案不一致：%r" % content
        finally:
            config.WECOM_TIMEOUT = old_timeout
            if old_main is None:
                os.environ.pop("ZEROSTEP_MAIN_URL", None)
            else:
                os.environ["ZEROSTEP_MAIN_URL"] = old_main

    def case_timeout() -> None:
        def slow(user_id: str, text: str) -> Dict[str, Any]:
            time.sleep(1.0)
            return {"reply": "迟到的回复", "status": "ok", "cached": False}

        old_timeout = config.WECOM_TIMEOUT
        channel_server.handle_message = slow
        config.WECOM_TIMEOUT = 0.3
        try:
            started = time.time()
            status, _, body = post_encrypted(build_msg_xml("zhaoliu", "查一下我的余额", "msg_40004"))
            elapsed = time.time() - started
            assert status == 200, "超时时应仍返回 200，实际 %d" % status
            content = _xml_text(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring(decrypt_reply(body)), "Content")
            assert content == TIMEOUT_REPLY, "超时文案不一致：%r" % content
            assert elapsed < 1.0, "超时未生效，响应耗时 %.2fs" % elapsed
        finally:
            config.WECOM_TIMEOUT = old_timeout
            channel_server.handle_message = real_handle

    check("1) AES-128 命中 FIPS-197 Appendix C + Appendix B 两组官方向量", case_aes)
    check("2) PKCS#7 补位/去补位（32 字节块）", case_pkcs7)
    check("3) 企微报文封装/解封装往返（随机 43 位 EncodingAESKey）", case_envelope)
    check("4) 回调验签通过 + 篡改后失败", case_signature)
    check("5) GET /wecom 验签解密 echostr 返回明文", case_get_verify)
    check("6) POST /wecom 验签解密 XML 并加密回包", case_post_decrypt)
    check("7) 同一 MsgId 去重，主服务只调用一次", case_dedupe)
    check("8) 主服务不可达 → 友好中文文案", case_unreachable)
    check("9) 转发超时 → 5 秒硬超时降级文案", case_timeout)

    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(tmp_dir, ignore_errors=True)
    passed = sum(1 for item in results if item)
    print("")
    print("结果：%d/%d 通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


# ---------------- 启动 ----------------
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="ZeroStep 企业微信回调适配器（加密回调 -> /api/chat）")
    parser.add_argument("--port", type=int, default=_port())
    parser.add_argument("--host", default=os.environ.get("WECOM_HOST", "127.0.0.1"))
    parser.add_argument("--selftest", action="store_true", help="运行全离线自测（9 项）")
    args = parser.parse_args(argv)
    if args.selftest:
        return _run_selftest()
    httpd = make_server(args.host, args.port)
    state = "已配置" if _configured() else "未配置（回调将返回 503）"
    print("ZeroStep 企业微信回调服务已在 http://%s:%d 启动（主服务：%s，配置：%s，开关：%s）" % (
        args.host, httpd.server_address[1], channel_server._main_url(), state,
        "开" if _enabled() else "关"))
    print("  GET  /wecom  URL 验证    POST /wecom 消息回调    GET /health 健康检查")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
