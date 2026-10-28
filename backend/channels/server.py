"""ZeroStep 渠道适配服务（v0.2.0，零依赖标准库）。

把外部 IM / 短信网关的 webhook 统一适配成主服务的 /api/chat 调用：
- 只做协议适配、会话映射、幂等与日志，不做任何业务判定（限额、风控、审批
  全部由主服务代码层负责）；
- 不向外部渠道回传 approval_id / nonce / thread_id 等内部凭据，只回传
  可读预览与「回复 确认 或 取消」提示；
- 主服务不可达时降级为可读提示，绝不把异常堆栈抛给渠道方。

启动：
    python backend/channels/server.py                 # 默认 127.0.0.1:8400
    python backend/channels/server.py --port 8401 --host 0.0.0.0

环境变量（每次请求现读，方便演示时热切换）：
    CHANNEL_ENABLED / CHANNEL_PORT / CHANNEL_TOKEN / CHANNEL_MAX_BODY /
    CHANNEL_IDEMPOTENT_SECONDS / CHANNEL_MESSAGE_LOG / CHANNEL_SESSIONS_FILE
主服务地址优先级：ZEROSTEP_MAIN_URL > MAIN_URL > ZEROSTEP_API_URL > http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
BACKEND = BASE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import config, plugins  # noqa: E402

DEFAULT_MAIN_URL = "http://127.0.0.1:8000"
UPSTREAM_TIMEOUT = 15.0
HINT = "回复 确认 或 取消"
UNAVAILABLE_REPLY = "渠道服务暂时不可用，请稍后再试"
CONFIRM_WORDS = ("确认", "确定", "同意", "approve", "yes")
REJECT_WORDS = ("取消", "拒绝", "不同意", "reject", "no")
_REMOTE_PREFIXES = ("http://", "https://")

_LOCK = threading.RLock()
_SESSIONS: Optional[Dict[str, str]] = None
_PENDING: Dict[str, Dict[str, Any]] = {}
_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_PORT = 0


class _UpstreamError(Exception):
    """主服务不可用（连接失败、超时或 5xx）。"""


# ---------------- 基础工具 ----------------
def _now_iso() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 - 时间格式化失败不影响主流程
        return time.strftime("%Y-%m-%dT%H:%M:%S")


def _main_url() -> str:
    """主服务地址：每次现读环境变量，支持演示时热切换。"""
    for key in ("ZEROSTEP_MAIN_URL", "MAIN_URL", "ZEROSTEP_API_URL"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_MAIN_URL


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(config, name, default) or default)
    except (TypeError, ValueError):
        return default


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
             timeout: float = UPSTREAM_TIMEOUT) -> Tuple[int, Dict[str, Any]]:
    """调用主服务。网络层失败抛 _UpstreamError；业务错误按状态码原样返回。"""
    url = _main_url()
    if not url.startswith(_REMOTE_PREFIXES):
        raise _UpstreamError("主服务地址不合法：%s" % url)
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection: HTTPConnection
    if parsed.scheme == "https":
        connection = HTTPSConnection(host, port, timeout=timeout)
    else:
        connection = HTTPConnection(host, port, timeout=timeout)
    try:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        data: Dict[str, Any] = {}
        try:
            loaded = json.loads(raw.decode("utf-8")) if raw else {}
            if isinstance(loaded, dict):
                data = loaded
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        return response.status, data
    except (OSError, HTTPException) as exc:
        raise _UpstreamError(str(exc) or type(exc).__name__)
    finally:
        connection.close()


# ---------------- 会话映射 ----------------
def _sessions_path() -> Path:
    return Path(getattr(config, "CHANNEL_SESSIONS_FILE", BASE / "sessions.json"))


def _load_sessions() -> Dict[str, str]:
    path = _sessions_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _sessions() -> Dict[str, str]:
    global _SESSIONS
    with _LOCK:
        if _SESSIONS is None:
            _SESSIONS = _load_sessions()
        return _SESSIONS


def _save_sessions(sessions: Dict[str, str]) -> None:
    path = _sessions_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _remember_session(user_id: str, session_id: str) -> None:
    sessions = _sessions()
    with _LOCK:
        if sessions.get(user_id) == session_id:
            return
        sessions[user_id] = session_id
        _save_sessions(sessions)


def _forget_session(user_id: str) -> None:
    sessions = _sessions()
    with _LOCK:
        if sessions.pop(user_id, None) is not None:
            _save_sessions(sessions)


def _ensure_session(user_id: str) -> str:
    """取渠道用户对应会话；失效或不存在时向主服务新建一个。"""
    existing = _sessions().get(user_id)
    if existing:
        return existing
    _, data = _request("POST", "/api/session", {})
    session_id = str(data.get("session_id") or "")
    if not session_id:
        raise _UpstreamError("主服务未返回 session_id")
    _remember_session(user_id, session_id)
    return session_id


# ---------------- 幂等缓存 ----------------
def _cache_ttl() -> float:
    return float(_int_setting("CHANNEL_IDEMPOTENT_SECONDS", 10))


def _cache_get(user_id: str, text: str) -> Optional[Dict[str, Any]]:
    key = (user_id, text)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            ts, payload = hit
            if now - ts <= _cache_ttl():
                result = dict(payload)
                result["cached"] = True
                return result
            _CACHE.pop(key, None)
    return None


def _cache_put(user_id: str, text: str, payload: Dict[str, Any]) -> None:
    now = time.time()
    with _LOCK:
        if len(_CACHE) > 500:
            _CACHE.clear()
        _CACHE[(user_id, text)] = (now, dict(payload))


# ---------------- 消息日志 ----------------
def _log_message(user_id: str, direction: str, text: str, status: str, cached: bool) -> None:
    path = Path(getattr(config, "CHANNEL_MESSAGE_LOG", BASE / "messages.jsonl"))
    record = {
        "ts": _now_iso(),
        "user_id": user_id,
        "direction": direction,
        "text": str(text or "")[:500],
        "status": str(status or ""),
        "cached": bool(cached),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------- 响应构造（严格白名单，绝不回传内部凭据） ----------------
def _ok_response(reply: str, status: str = "ok", cached: bool = False) -> Dict[str, Any]:
    return {"reply": str(reply or ""), "status": status or "ok", "cached": bool(cached)}


def _awaiting_response(preview: str) -> Dict[str, Any]:
    return {
        "needs_confirmation": True,
        "preview": str(preview or ""),
        "hint": HINT,
        "status": "awaiting_approval",
        "cached": False,
    }


def _unavailable_response() -> Dict[str, Any]:
    return {"reply": UNAVAILABLE_REPLY, "status": "error"}


def _decision(text: str) -> Optional[str]:
    """判断是否是对挂起审批的确认/取消；普通业务句（如「取消我的订阅」）不当作审批。"""
    value = str(text or "").strip().lower().rstrip("。.!！?？~ ")
    if not value:
        return None
    for word in CONFIRM_WORDS:
        if (value == word or value.startswith(word)) and len(value) <= len(word) + 2:
            return "approve"
    for word in REJECT_WORDS:
        if (value == word or value.startswith(word)) and len(value) <= len(word) + 2:
            return "reject"
    return None


# ---------------- 主流程 ----------------
def _handle_chat_result(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """把 /api/chat 结果裁剪成渠道白名单字段。"""
    status = str(data.get("status") or "ok")
    if status == "awaiting_approval":
        approval = data.get("approval") if isinstance(data.get("approval"), dict) else {}
        preview = str(approval.get("preview") or approval.get("title") or "")
        with _LOCK:
            _PENDING[user_id] = {
                "session_id": str(data.get("session_id") or ""),
                "approval_id": str(approval.get("approval_id") or ""),
                "ts": time.time(),
            }
        return _awaiting_response(preview)
    with _LOCK:
        _PENDING.pop(user_id, None)
    if status == "error":
        return _unavailable_response()
    return _ok_response(str(data.get("reply") or ""), status if status in ("ok", "blocked") else "ok")


def _chat_once(user_id: str, text: str) -> Dict[str, Any]:
    session_id = _ensure_session(user_id)
    code, data = _request("POST", "/api/chat", {"session_id": session_id, "text": text})
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    if code == 404 and str(error.get("code") or "") == "SESSION_NOT_FOUND":
        # 主服务重置过数据库：换一个新会话重试一次
        _forget_session(user_id)
        session_id = _ensure_session(user_id)
        code, data = _request("POST", "/api/chat", {"session_id": session_id, "text": text})
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
    if code >= 500:
        raise _UpstreamError("主服务返回 HTTP %d" % code)
    if code >= 400:
        return _ok_response(str(error.get("message") or "请求未生效"), "error")
    return _handle_chat_result(user_id, data)


def _dispatch(user_id: str, text: str) -> Dict[str, Any]:
    decision = _decision(text)
    if decision:
        with _LOCK:
            pending = dict(_PENDING.get(user_id) or {})
        if pending and pending.get("approval_id"):
            payload = {
                "session_id": pending.get("session_id") or "",
                "approval_id": pending.get("approval_id") or "",
                "decision": decision,
                "code": "",
            }
            try:
                code, data = _request("POST", "/api/approval", payload)
            except _UpstreamError:
                return _unavailable_response()
            with _LOCK:
                _PENDING.pop(user_id, None)
            if code >= 500:
                return _unavailable_response()
            if code >= 400:
                error = data.get("error") if isinstance(data.get("error"), dict) else {}
                return _ok_response(str(error.get("message") or "审批未生效"), "error")
            return _handle_chat_result(user_id, data)
    try:
        return _chat_once(user_id, text)
    except _UpstreamError:
        return _unavailable_response()


def handle_message(user_id: str, text: str) -> Dict[str, Any]:
    """处理一条渠道消息：幂等 → 审批确认/取消 → 转发主服务，任何异常都降级。"""
    cached = _cache_get(user_id, text)
    if cached is not None:
        _log_message(user_id, "in", text, str(cached.get("status") or ""), True)
        _log_message(user_id, "out", str(cached.get("reply") or cached.get("hint") or ""),
                     str(cached.get("status") or ""), True)
        return cached
    try:
        result = _dispatch(user_id, text)
    except Exception:  # noqa: BLE001 - 渠道层不把内部异常抛给外部
        result = _unavailable_response()
    _cache_put(user_id, text, result)
    status = str(result.get("status") or "")
    out_text = str(result.get("reply") or result.get("preview") or result.get("hint") or "")
    _log_message(user_id, "in", text, status, False)
    _log_message(user_id, "out", out_text, status, False)
    return result


def _health() -> Dict[str, Any]:
    with _LOCK:
        sessions = len(_sessions())
        pending = len(_PENDING)
        port = _PORT or _int_setting("CHANNEL_PORT", 8400)
    return {
        "status": "ok",
        "enabled": bool(getattr(config, "CHANNEL_ENABLED", False)),
        "plugin_enabled": plugins.is_enabled("channel-im"),
        "port": int(port),
        "main_url": _main_url(),
        "sessions": sessions,
        "pending": pending,
        "ts": _now_iso(),
    }


# ---------------- HTTP 入口 ----------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ZeroStepChannel/" + str(getattr(config, "VERSION", "1.0.0"))

    def log_message(self, fmt, *args):  # 安静一点，只保留 4xx/5xx
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("%s - %s " % (self.address_string(), fmt % args))

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._send(200, _health())
            if path == "/message":
                return self._send(405, {"error": "METHOD_NOT_ALLOWED"})
            return self._send(404, {"error": "NOT_FOUND"})
        except Exception:  # noqa: BLE001
            return self._send(500, {"error": "INTERNAL"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path != "/message":
                return self._send(404, {"error": "NOT_FOUND"})
            # channel-im 插件停用：直接 503，且不写 messages.jsonl
            if not plugins.is_enabled("channel-im"):
                return self._send(503, {"ok": False, "error": "渠道插件已停用"})
            if not bool(getattr(config, "CHANNEL_ENABLED", False)):
                return self._send(503, {"error": "CHANNEL_DISABLED"})
            token = str(self.headers.get("X-Channel-Token") or "")
            expected = str(getattr(config, "CHANNEL_TOKEN", "") or "")
            if not expected or not hmac.compare_digest(token, expected):
                return self._send(401, {"error": "UNAUTHORIZED"})
            length = int(self.headers.get("Content-Length") or 0)
            limit = _int_setting("CHANNEL_MAX_BODY", 32 * 1024)
            if length > limit:
                return self._send(413, {"error": "BODY_TOO_LARGE"})
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send(400, {"error": "BAD_REQUEST", "message": "请求体不是合法 JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "BAD_REQUEST", "message": "请求体必须是 JSON 对象"})
            user_id = str(body.get("user_id") or "").strip()
            text = str(body.get("text") or "").strip()
            if not user_id or not text:
                return self._send(400, {"error": "BAD_REQUEST", "message": "缺少 user_id 或 text"})
            return self._send(200, handle_message(user_id, text))
        except Exception:  # noqa: BLE001 - 渠道层任何异常都不能把堆栈抛给渠道方
            try:
                return self._send(200, _unavailable_response())
            except Exception:  # noqa: BLE001
                return


class ChannelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str = "127.0.0.1", port: Optional[int] = None) -> ChannelServer:
    """创建渠道 HTTP 服务（port=0 时由系统分配，便于自测）。"""
    global _PORT
    actual_port = _int_setting("CHANNEL_PORT", 8400) if port is None else int(port)
    httpd = ChannelServer((host, actual_port), _Handler)
    _PORT = int(httpd.server_address[1])
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ZeroStep 渠道适配服务（webhook -> /api/chat）")
    parser.add_argument("--port", type=int, default=_int_setting("CHANNEL_PORT", 8400))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = parser.parse_args(argv)
    httpd = make_server(args.host, args.port)
    token_state = "已配置" if str(getattr(config, "CHANNEL_TOKEN", "") or "") else "未配置"
    print("ZeroStep 渠道服务已在 http://%s:%d 启动（主服务：%s，令牌：%s，渠道开关：%s）" % (
        args.host, httpd.server_address[1], _main_url(), token_state,
        "开" if bool(getattr(config, "CHANNEL_ENABLED", False)) else "关"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
