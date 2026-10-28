"""外部汇率演示服务（v0.2.0，零依赖，MCP JSON-RPC over HTTP 子集）。

启动：
    python backend/external/fx_server.py --port 8300
    python backend/external/fx_server.py --port 8300 --slow 12   # 模拟慢响应，排查读超时

接口：
    POST /rpc     JSON-RPC 2.0：initialize / notifications/initialized / tools/list / tools/call
    GET  /health  健康检查（主服务与脚本探活用）

边界：本服务只提供「演示用固定牌价」，不是真实报价；返回值只是数据，
主服务不得据此放宽任何风控或金额限制。牌价定义为 1 CNY = X 外币的固定表 + 极小抖动。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app import config  # noqa: E402

_DEFAULT_RATES = {"USD": 0.1403, "HKD": 1.0921, "EUR": 0.1287, "JPY": 20.85}

_TOOLS = [{
    "name": "fx_convert",
    "description": "按演示固定牌价把金额从一种货币换算为另一种货币（金额单位：分）。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "源币种，如 CNY"},
            "to": {"type": "string", "description": "目标币种，如 USD"},
            "amount": {"type": "number", "description": "源币种金额，单位元（可为小数）"},
            "amount_cents": {"type": "integer", "description": "兼容口径：金额，单位分"},
        },
        "required": ["from", "to"],
    },
}]


class _RpcError(Exception):
    """JSON-RPC 层错误，统一在 handle_rpc 内转成 error 字段。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rates() -> Dict[str, float]:
    """取演示牌价表（1 CNY = X 外币），配置缺失时用内置表，保证独立启动可跑。"""
    raw = getattr(config, "MCP_FX_FALLBACK_RATE", None) or _DEFAULT_RATES
    out: Dict[str, float] = {}
    for code, per_cny in raw.items():
        try:
            value = float(per_cny)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[str(code).upper()] = value
    return out or dict(_DEFAULT_RATES)


def _cny_per_unit() -> Dict[str, float]:
    """换算表：1 单位某外币 = X CNY。"""
    table: Dict[str, float] = {"CNY": 1.0}
    for code, per_cny in _rates().items():
        table[code] = 1.0 / per_cny
    return table


def convert(from_code: Any, to_code: Any, amount: Any = None, amount_cents: Any = None) -> Dict[str, Any]:
    """执行一次演示换算：只做数学换算，不涉及任何真实交易。

    主口径（engine 实际调用）：from / to 币种 + amount（单位元，可为小数）；
    兼容口径：amount_cents（整数分）。返回值同时携带两套金额字段，便于不同调用方取用。
    """
    source_code = str(from_code or "").strip().upper()
    target_code = str(to_code or "").strip().upper()
    table = _cny_per_unit()
    if source_code not in table:
        raise ValueError("不支持的源币种：%s" % (source_code or "空"))
    if target_code not in table:
        raise ValueError("不支持的目标币种：%s" % (target_code or "空"))
    if amount is not None:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("amount 必须是数字（单位元）")
        if float(amount) <= 0:
            raise ValueError("amount 必须大于 0")
        cents = int(round(float(amount) * 100))
    elif amount_cents is not None:
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise ValueError("amount_cents 必须是整数（分）")
        if amount_cents <= 0:
            raise ValueError("amount_cents 必须大于 0")
        cents = amount_cents
    else:
        raise ValueError("缺少金额：请传 amount（元）或 amount_cents（分）")
    jitter = 1.0 + random.uniform(-0.002, 0.002)
    rate = table[source_code] / table[target_code] * jitter
    converted_cents = int(round(cents * rate))
    converted = round(converted_cents / 100.0, 2)
    as_of = _now_iso()
    return {
        "from": source_code,
        "to": target_code,
        "amount": round(cents / 100.0, 2),
        "rate": round(rate, 6),
        "converted": converted,
        "converted_cents": converted_cents,
        "amount_cents": cents,
        "as_of": as_of,
        "base": source_code,
        "quote": target_code,
        "source": "external-fx",
        "ts": as_of,
    }


def handle_rpc(request: Any) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；返回 None 表示这是一条通知、不需要响应体。"""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = request.get("id")
    method = str(request.get("method") or "")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "notifications/initialized":
        return None
    try:
        if method == "initialize":
            result: Dict[str, Any] = {
                "protocolVersion": str(params.get("protocolVersion") or "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "zerostep-fx-mcp", "version": "0.2.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _TOOLS}
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(-32602, "arguments 必须是对象")
            if name != "fx_convert":
                raise _RpcError(-32601, "工具不存在：%s" % name)
            data = convert(
                arguments.get("from") if arguments.get("from") is not None else arguments.get("base"),
                arguments.get("to") if arguments.get("to") is not None else arguments.get("quote"),
                arguments.get("amount"),
                arguments.get("amount_cents"),
            )
            result = {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data,
                "isError": False,
            }
        else:
            raise _RpcError(-32601, "方法不存在：%s" % method)
    except _RpcError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
    except (ValueError, TypeError) as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)[:160]}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ZeroStepFx/0.2.0"

    def log_message(self, fmt, *args):  # 安静一点，只保留错误行
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("[fx] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端已超时断开，不打印堆栈

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/health":
            return self._send(404, {"error": "NOT_FOUND"})
        self._send(200, {
            "status": "ok",
            "server": "fx",
            "name": "外部汇率演示服务",
            "port": int(self.server.server_address[1]),
            "tools": [tool["name"] for tool in _TOOLS],
            "ts": _now_iso(),
        })

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/rpc":
            return self._send(404, {"error": "NOT_FOUND"})
        try:
            slow = float(getattr(self.server, "slow", 0.0) or 0.0)
            if slow > 0:
                time.sleep(slow)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 1024 * 1024:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "请求体为空或过大"}})
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send(200, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}})
            response = handle_rpc(request)
            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, response)
        except Exception as exc:  # noqa: BLE001  单个请求出错不影响服务
            self._send(500, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32603, "message": str(exc)[:160]}})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    slow = 0.0


def make_server(host: str = "127.0.0.1", port: int = 8300, slow: float = 0.0) -> _Server:
    """创建外部汇率服务实例（selftest 用进程内启动）。"""
    httpd = _Server((host, int(port)), _Handler)
    httpd.slow = float(slow)
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="ZeroStep 外部汇率演示 MCP 服务")
    parser.add_argument("--port", type=int, default=8300, help="监听端口，默认 8300")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--slow", type=float, default=0.0, help="每个 /rpc 请求延迟 N 秒（模拟慢服务）")
    args = parser.parse_args()
    httpd = make_server(args.host, args.port, args.slow)
    print("外部汇率服务已启动：http://%s:%d/rpc （健康检查 /health）" % (args.host, httpd.server_address[1]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n外部汇率服务已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
