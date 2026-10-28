"""银行开放平台 MCP 演示服务（v0.6.0，零依赖，MCP JSON-RPC over HTTP）。

真实银行不会把核心系统直接暴露给外部；对外提供的是「开放平台」形式的 MCP/HTTP
接口，通常带 OAuth2 / API Key、会话、只读查询权限。本文件把这类接口的**协议形态**
做成本地可跑的演示服务，用于验证 ZeroStep 的通用 MCP 接入层（鉴权、会话、SSE、只读边界）。

启动：
    python backend/external/bank_mcp_server.py --port 8310
    python backend/external/bank_mcp_server.py --port 8310 --oauth        # 同时开放 /oauth/token
    python backend/external/bank_mcp_server.py --port 8310 --no-sse       # 用普通 JSON 响应

接口：
    POST /mcp          JSON-RPC：initialize / notifications/initialized / tools/list / tools/call
    POST /oauth/token  client_credentials 取 Bearer token（--oauth 时启用）
    GET  /health       健康检查 + 会话/调用计数（脚本断言用）

鉴权：默认要求 Authorization: Bearer <--token>；--oauth 时也接受 /oauth/token 签发的 token。
会话：initialize 返回 Mcp-Session-Id，tools/list 与 tools/call 必须携带该头（模拟真实会话）。
响应：tools/call 默认以 text/event-stream（SSE）返回，覆盖 streamable-http 传输形态。

边界：本服务只返回**演示数据**；bank_transfer 永远返回错误且不执行任何资金动作，
主服务对写操作也一律拒绝（客户端根本不转发），双重保证演示不会真的动钱。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app import config  # noqa: E402

_DEFAULT_CLIENT_ID = "demo-bank-client"
_DEFAULT_CLIENT_SECRET = "demo-bank-secret"

_TOOLS = [
    {
        "name": "bank_account_summary",
        "description": "查询演示账户余额与户名摘要（只读，不返回完整卡号）。",
        "inputSchema": {
            "type": "object",
            "properties": {"account_no": {"type": "string", "description": "账户号；缺省用演示账户"}},
        },
    },
    {
        "name": "bank_quote",
        "description": "银行牌价换算（只读，演示固定牌价）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "源币种，如 CNY"},
                "to": {"type": "string", "description": "目标币种，如 USD"},
                "amount": {"type": "number", "description": "金额（元）"},
            },
            "required": ["from", "to", "amount"],
        },
    },
    {
        "name": "bank_transfer",
        "description": "转账（写操作；演示服务一律拒绝，主服务也不会转发该调用）。",
        "inputSchema": {
            "type": "object",
            "properties": {"payee": {"type": "string"}, "amount": {"type": "number"}},
        },
    },
]

_RATES = {"CNY": 1.0, "USD": 0.1403, "HKD": 1.0921, "EUR": 0.1287, "JPY": 20.85}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _account_summary(account_no: Any) -> Dict[str, Any]:
    raw = str(account_no or "6222020200001234")
    masked = raw[:4] + "****" + raw[-4:] if len(raw) >= 8 else "*" * len(raw)
    return {
        "account_no_masked": masked,
        "holder": "张*",
        "balance_cents": 1286500,
        "currency": "CNY",
        "account_type": "演示借记卡",
        "as_of": _now_iso(),
        "source": "external-bank",
    }


def _quote(from_code: Any, to_code: Any, amount: Any) -> Dict[str, Any]:
    source = str(from_code or "").strip().upper()
    target = str(to_code or "").strip().upper()
    if source not in _RATES:
        raise ValueError("不支持的源币种：%s" % (source or "空"))
    if target not in _RATES:
        raise ValueError("不支持的目标币种：%s" % (target or "空"))
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValueError("amount 必须是数字")
    if value <= 0:
        raise ValueError("amount 必须大于 0")
    rate = _RATES[source] / _RATES[target]
    return {
        "from": source,
        "to": target,
        "amount": round(value, 2),
        "rate": round(rate, 6),
        "converted": round(value * rate, 2),
        "as_of": _now_iso(),
        "source": "external-bank",
        "disclaimer": "演示牌价，不构成真实报价",
    }


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "bank_account_summary":
        data = _account_summary(arguments.get("account_no"))
    elif name == "bank_quote":
        data = _quote(arguments.get("from"), arguments.get("to"), arguments.get("amount"))
    elif name == "bank_transfer":
        raise _RpcError(-32000, "演示服务不执行任何资金动作（写操作被双重拦截）")
    else:
        raise _RpcError(-32601, "工具不存在：%s" % name)
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "structuredContent": data,
        "isError": False,
    }


def handle_rpc(request: Any, server: Any) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；通知返回 None（HTTP 层回 202）。"""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = request.get("id")
    method = str(request.get("method") or "")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "notifications/initialized":
        return None
    try:
        if method == "initialize":
            result: Dict[str, Any] = {
                "protocolVersion": str(params.get("protocolVersion") or "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "zerostep-demo-bank-mcp", "version": "0.6.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _TOOLS}
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            result = _call_tool(name, arguments)
        else:
            raise _RpcError(-32601, "方法不存在：%s" % method)
    except _RpcError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
    except (ValueError, TypeError) as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)[:160]}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class _Stats:
    def __init__(self) -> None:
        self.sessions = 0
        self.calls = 0
        self.write_calls = 0
        self.tokens = 0

    def snapshot(self) -> Dict[str, int]:
        return {"sessions": self.sessions, "calls": self.calls,
                "write_calls": self.write_calls, "tokens": self.tokens}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ZeroStepDemoBank/0.6.0"

    def log_message(self, fmt, *args):  # 安静一点，只保留错误行
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("[bank-mcp] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- 输出
    def _send(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_raw(status, body, "application/json; charset=utf-8", headers)

    def _send_raw(self, status: int, body: bytes, content_type: str,
                  headers: Optional[Dict[str, str]] = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端已超时断开，不打印堆栈

    def _send_sse(self, payload: Any) -> None:
        frame = "event: message\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
        self._send_raw(200, frame.encode("utf-8"), "text/event-stream; charset=utf-8")

    # ---------------------------------------------------------------- 鉴权
    def _bearer(self) -> str:
        raw = str(self.headers.get("Authorization") or "").strip()
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return ""

    def _authorized(self) -> bool:
        token = self._bearer()
        if not token:
            return False
        if token == self.server.token:
            return True
        return token in self.server.issued_tokens

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/health":
            return self._send(404, {"error": "NOT_FOUND"})
        self._send(200, {
            "status": "ok",
            "server": "bank",
            "name": "银行开放平台 MCP 演示服务",
            "port": int(self.server.server_address[1]),
            "tools": [tool["name"] for tool in _TOOLS],
            "auth": {"bearer_required": True, "oauth": bool(self.server.oauth)},
            "sse": bool(self.server.sse),
            "counts": self.server.stats.snapshot(),
            "ts": _now_iso(),
        })

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/oauth/token":
            return self._oauth_token()
        if path != "/mcp":
            return self._send(404, {"error": "NOT_FOUND"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1024 * 1024:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "请求体为空或过大"}})
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send(200, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}})
            method = str(request.get("method") or "") if isinstance(request, dict) else ""

            if not self._authorized():
                return self._send(401, {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                                        "error": {"code": -32001, "message": "invalid_token"}},
                                  {"WWW-Authenticate": 'Bearer realm="bank-mcp"'})
            if self.server.slow > 0:
                time.sleep(self.server.slow)

            session_headers: Dict[str, str] = {}
            if method == "initialize":
                session_id = uuid.uuid4().hex
                self.server.sessions.add(session_id)
                self.server.stats.sessions += 1
                session_headers["Mcp-Session-Id"] = session_id
            elif method not in ("notifications/initialized", "ping"):
                session_id = str(self.headers.get("Mcp-Session-Id") or "")
                if not session_id or session_id not in self.server.sessions:
                    return self._send(400, {"jsonrpc": "2.0", "id": request.get("id"),
                                            "error": {"code": -32002, "message": "missing_or_invalid_session"}},
                                      {"Mcp-Session-Id": session_id} if session_id else None)

            if method == "tools/call":
                self.server.stats.calls += 1
                params = request.get("params") if isinstance(request.get("params"), dict) else {}
                if str(params.get("name") or "") == "bank_transfer":
                    self.server.stats.write_calls += 1

            response = handle_rpc(request, self.server)
            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                for key, value in session_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                return
            if method == "tools/call" and self.server.sse:
                return self._send_sse_with_headers(response, session_headers)
            self._send(200, response, session_headers)
        except Exception as exc:  # noqa: BLE001  单个请求出错不影响服务
            self._send(500, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32603, "message": str(exc)[:160]}})

    def _send_sse_with_headers(self, payload: Any, headers: Dict[str, str]) -> None:
        frame = "event: message\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
        self._send_raw(200, frame.encode("utf-8"), "text/event-stream; charset=utf-8", headers)

    # ---------------------------------------------------------------- OAuth2
    def _oauth_token(self) -> None:
        if not self.server.oauth:
            return self._send(404, {"error": "oauth_disabled"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
        except (ValueError, OSError):
            raw = ""
        content_type = str(self.headers.get("Content-Type") or "")
        fields: Dict[str, str] = {}
        if "application/json" in content_type:
            try:
                parsed = json.loads(raw or "{}")
                if isinstance(parsed, dict):
                    fields = {str(k): str(v) for k, v in parsed.items()}
            except ValueError:
                fields = {}
        else:
            fields = {key: values[0] for key, values in parse_qs(raw).items() if values}
        if fields.get("grant_type") != "client_credentials":
            return self._send(400, {"error": "unsupported_grant_type"})
        if fields.get("client_id") != self.server.client_id or \
                fields.get("client_secret") != self.server.client_secret:
            return self._send(401, {"error": "invalid_client"})
        token = "bank-oauth-" + uuid.uuid4().hex
        self.server.issued_tokens.add(token)
        self.server.stats.tokens += 1
        self._send(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600,
                         "scope": fields.get("scope") or "bank.read"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    token = "demo-bank-token"
    oauth = False
    sse = True
    slow = 0.0
    client_id = _DEFAULT_CLIENT_ID
    client_secret = _DEFAULT_CLIENT_SECRET

    def __init__(self, address, handler, token: str = "demo-bank-token", oauth: bool = False,
                 sse: bool = True, slow: float = 0.0,
                 client_id: str = _DEFAULT_CLIENT_ID, client_secret: str = _DEFAULT_CLIENT_SECRET):
        super().__init__(address, handler)
        self.token = str(token or "demo-bank-token")
        self.oauth = bool(oauth)
        self.sse = bool(sse)
        self.slow = float(slow)
        self.client_id = str(client_id or _DEFAULT_CLIENT_ID)
        self.client_secret = str(client_secret or _DEFAULT_CLIENT_SECRET)
        self.sessions: set = set()
        self.issued_tokens: set = set()
        self.stats = _Stats()


def make_server(host: str = "127.0.0.1", port: int = 8310, token: str = "demo-bank-token",
                oauth: bool = False, sse: bool = True, slow: float = 0.0) -> _Server:
    """创建银行 MCP 演示服务实例（selftest / e2e 用进程内启动）。"""
    return _Server((host, int(port)), _Handler, token=token, oauth=oauth, sse=sse, slow=slow)


def main() -> None:
    parser = argparse.ArgumentParser(description="ZeroStep 银行开放平台 MCP 演示服务")
    parser.add_argument("--port", type=int, default=8310, help="监听端口，默认 8310")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--token", default="demo-bank-token", help="Bearer token（演示默认值）")
    parser.add_argument("--oauth", action="store_true", help="同时开放 /oauth/token")
    parser.add_argument("--no-sse", action="store_true", help="tools/call 用普通 JSON 响应")
    parser.add_argument("--slow", type=float, default=0.0, help="每个 /mcp 请求延迟 N 秒")
    args = parser.parse_args()
    httpd = make_server(args.host, args.port, token=args.token,
                        oauth=bool(args.oauth), sse=not args.no_sse, slow=args.slow)
    print("银行 MCP 演示服务已启动：http://%s:%d/mcp （健康检查 /health）" % (args.host, httpd.server_address[1]))
    print("Bearer token：%s%s" % (args.token, "；OAuth2 端点 /oauth/token" if args.oauth else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n银行 MCP 演示服务已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
