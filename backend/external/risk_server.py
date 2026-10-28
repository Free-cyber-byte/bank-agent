"""外部风控演示服务（v0.2.0，零依赖，MCP JSON-RPC over HTTP 子集）。

启动：
    python backend/external/risk_server.py --port 8301
    python backend/external/risk_server.py --port 8301 --slow 10   # 模拟慢响应

接口：
    POST /rpc     JSON-RPC 2.0：initialize / notifications/initialized / tools/list / tools/call
    GET  /health  健康检查

边界：本服务只是「演示用名单 + 阈值」，返回的 hit/score/reasons 仅为参考数据；
主服务只能据此收紧风控（增加复核/拒绝），不得据此放宽任何金额、权限或审批限制。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 演示通讯录（命中这些收款人视为「熟客」，其余按陌生收款人处理）
_KNOWN_PAYEES = ("妈妈", "爸爸", "房东", "老同学", "张伟")
_LIST_NAME = "演示风控名单"
_AMOUNT_LIMIT_CENTS = 5000 * 100   # 单笔超过 5000 元触发大额提醒

_TOOLS = [{
    "name": "risk_screen",
    "description": "对一笔拟转账做演示风控初筛，返回是否命中、命中名单、风险分与原因列表。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "payee_name": {"type": "string", "description": "收款人姓名"},
            "payee_account": {"type": "string", "description": "收款账号（只回显尾号）"},
            "payee_account_tail": {"type": "string", "description": "收款账号尾号（兼容口径）"},
            "amount": {"type": "number", "description": "转账金额，单位元（可为小数）"},
            "amount_cents": {"type": "integer", "description": "兼容口径：转账金额，单位分"},
            "user_id": {"type": "string", "description": "发起用户 id（只用于日志口径）"},
        },
        "required": ["payee_name"],
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


def screen(payee_name: Any, payee_account_tail: Any, amount_cents: Any, user_id: Any = "") -> Dict[str, Any]:
    """三条演示规则：金额异常 / 陌生收款人 / 命中演示名单（张*）。

    对外同时给出 risk_level + blocked + hits + advice：engine 只在
    blocked=true 或 risk_level=="high" 时拦截转账，其余情况仅记录不阻断。
    """
    name = str(payee_name or "").strip()
    tail = str(payee_account_tail or "").strip()
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise ValueError("amount_cents 必须是整数（分）")
    if amount_cents <= 0:
        raise ValueError("amount_cents 必须大于 0")

    reasons: List[str] = []
    score = 0
    list_name = ""
    if amount_cents > _AMOUNT_LIMIT_CENTS:
        score += 40
        reasons.append("单笔金额超过 5000 元（演示大额规则）")
    if not name:
        score += 20
        reasons.append("缺少收款人姓名，信息不完整")
    elif name not in _KNOWN_PAYEES:
        score += 30
        reasons.append("陌生收款人：通讯录中无历史交易记录")
    if name.startswith("张"):
        score += 60
        list_name = _LIST_NAME
        reasons.append("命中演示风控名单（张*）")
    score = min(100, score)
    if score >= 60:
        risk_level = "high"
    elif score >= 30:
        risk_level = "medium"
    else:
        risk_level = "low"
    blocked = risk_level == "high"
    advice = {
        "high": "风险较高，建议拦截：请先电话核实收款人身份再操作",
        "medium": "存在风险提示，建议核对收款人信息并考虑小额试转",
        "low": "暂未发现明显风险，仍请核对收款人姓名与账号",
    }[risk_level]
    return {
        "risk_level": risk_level,
        "blocked": blocked,
        "hits": reasons,
        "advice": advice,
        "hit": score > 0,
        "list_name": list_name,
        "score": score,
        "reasons": reasons,
        "payee_name": name[:20],
        "payee_account_tail": tail[-4:],
        "amount_cents": amount_cents,
        "amount": round(amount_cents / 100.0, 2),
        "user_id": str(user_id or "")[:32],
        "source": "external-risk",
        "ts": _now_iso(),
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
                "serverInfo": {"name": "zerostep-risk-mcp", "version": "0.2.0"},
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
            if name != "risk_screen":
                raise _RpcError(-32601, "工具不存在：%s" % name)
            amount = arguments.get("amount")
            if amount is not None:
                if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    raise _RpcError(-32602, "amount 必须是数字（单位元）")
                amount_cents = int(round(float(amount) * 100))
            else:
                amount_cents = arguments.get("amount_cents")
            data = screen(
                arguments.get("payee_name"),
                arguments.get("payee_account_tail") if arguments.get("payee_account_tail") is not None
                else arguments.get("payee_account"),
                amount_cents,
                arguments.get("user_id"),
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
    server_version = "ZeroStepRisk/0.2.0"

    def log_message(self, fmt, *args):  # 安静一点，只保留错误行
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("[risk] %s - %s\n" % (self.address_string(), fmt % args))

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
            "server": "risk",
            "name": "外部风控演示服务",
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


def make_server(host: str = "127.0.0.1", port: int = 8301, slow: float = 0.0) -> _Server:
    """创建外部风控服务实例（selftest 用进程内启动）。"""
    httpd = _Server((host, int(port)), _Handler)
    httpd.slow = float(slow)
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="ZeroStep 外部风控演示 MCP 服务")
    parser.add_argument("--port", type=int, default=8301, help="监听端口，默认 8301")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--slow", type=float, default=0.0, help="每个 /rpc 请求延迟 N 秒（模拟慢服务）")
    args = parser.parse_args()
    httpd = make_server(args.host, args.port, args.slow)
    print("外部风控服务已启动：http://%s:%d/rpc （健康检查 /health）" % (args.host, httpd.server_address[1]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n外部风控服务已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
