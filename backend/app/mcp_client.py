"""ZeroStep MCP 客户端（v0.2.0，零依赖，仅标准库）。

职责边界：把「外部只读服务」包装成安全的 MCP 工具调用，本模块只负责协议与容错：
- 只实现 JSON-RPC 2.0 的最小子集：initialize -> notifications/initialized ->
  tools/list（按 MCP_TOOLS_CACHE_SECONDS 缓存）-> tools/call；
- 工具白名单只认 tools/list 返回的名字，调用前逐一校验；
- 任何网络 / 协议 / 解析异常都在本模块内部吃掉，对外只返回结构化结果，绝不抛异常；
- 外部数据一律按「数据」对待：只允许补充展示或收紧风控，不允许放宽任何限制。

降级约定：连不上、超时（连接 2 秒 / 读 10 秒）、JSON-RPC 报错、工具不在白名单时，
call_tool 返回 ok=False、degraded=True、source="degraded"，调用方（engine）据此回退到
内置能力；服务恢复后下一次调用自动恢复，无需重启主服务。
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import config

_JSONRPC = "2.0"
_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "zerostep-backend", "version": "0.2.0"}
_SERVER_NAMES = ("fx", "risk")
# 兼容 call_tool(tool, args) 两参写法，engine 用哪种接法都行
_TOOL_SERVERS = {"fx_convert": "fx", "risk_screen": "risk"}


class _ServerState:
    """单个 MCP 服务在本进程内的状态：工具清单缓存 + 最近一次调用结果。"""

    __slots__ = ("name", "url", "tools", "tools_at", "last_error", "last_latency_ms", "last_ok")

    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self.url = url
        self.tools: List[Dict[str, Any]] = []
        self.tools_at = 0.0
        self.last_error = ""
        self.last_latency_ms = 0
        self.last_ok = False

    def reset(self, url: str) -> None:
        self.url = url
        self.tools = []
        self.tools_at = 0.0


_LOCK = threading.RLock()
_STATES: Dict[str, _ServerState] = {}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def enabled() -> bool:
    """MCP 总开关：每次调用动态读 config，运行中改环境变量并 reload 后立即生效。"""
    return bool(getattr(config, "MCP_ENABLED", True))


def _server_urls() -> Dict[str, str]:
    return {
        "fx": str(getattr(config, "MCP_FX_URL", "") or ""),
        "risk": str(getattr(config, "MCP_RISK_URL", "") or ""),
    }


def _state(server: str) -> _ServerState:
    """取服务状态；URL 变化（运行改配置或自测）时清空工具缓存。"""
    url = _server_urls().get(server, "")
    with _LOCK:
        state = _STATES.get(server)
        if state is None:
            state = _ServerState(server, url)
            _STATES[server] = state
        elif state.url != url:
            state.reset(url)
        return state


def _timeout_seconds(timeout_ms: Optional[int] = None) -> Tuple[float, float]:
    """返回 (连接超时秒, 读超时秒)。默认取配置 2s / 10s，自测可传 timeout_ms 缩短。"""
    read_ms = int(timeout_ms if timeout_ms is not None else getattr(config, "MCP_TIMEOUT_MS", 10000))
    connect_ms = int(getattr(config, "MCP_CONNECT_TIMEOUT_MS", 2000))
    if timeout_ms is not None:
        connect_ms = min(connect_ms, max(1, read_ms))
    return max(0.05, connect_ms / 1000.0), max(0.05, read_ms / 1000.0)


def _describe_error(exc: BaseException) -> str:
    """把底层异常翻译成稳定的中文原因，避免把堆栈或内部细节透给调用方。"""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "读取超时（服务无响应）"
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝，服务可能未启动"
    if isinstance(exc, socket.gaierror):
        return "服务地址无法解析"
    if isinstance(exc, ConnectionResetError):
        return "连接被重置"
    if isinstance(exc, http.client.HTTPException):
        return "HTTP 协议异常"
    if isinstance(exc, OSError):
        return "网络不可达：%s" % (exc.strerror or exc,)
    return "%s: %s" % (type(exc).__name__, str(exc)[:120])


def _http_post(url: str, payload: Dict[str, Any], connect_s: float, read_s: float) -> Tuple[int, str]:
    """POST 一段 JSON，返回 (HTTP 状态码, 响应体文本)。连接与读取使用不同超时。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("MCP 服务地址不合法：%s" % url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, port, timeout=connect_s)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(read_s)  # 连上之后切换为读超时，避免慢响应拖死线程
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
        })
        resp = conn.getresponse()
        return int(resp.status), resp.read().decode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _rpc(url: str, method: str, params: Optional[Dict[str, Any]], connect_s: float, read_s: float,
         notify: bool = False) -> Any:
    """发送一条 JSON-RPC 消息并返回 result；notify=True 表示发通知、不读 result。"""
    payload: Dict[str, Any] = {"jsonrpc": _JSONRPC, "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = 1
    status, text = _http_post(url, payload, connect_s, read_s)
    if notify:
        if status >= 400:
            raise RuntimeError("HTTP %d" % status)
        return None
    if status != 200:
        raise RuntimeError("HTTP %d" % status)
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        raise RuntimeError("响应不是合法 JSON")
    if not isinstance(data, dict):
        raise RuntimeError("响应格式不合法")
    error = data.get("error")
    if isinstance(error, dict):
        raise RuntimeError(str(error.get("message") or error.get("code") or "JSON-RPC 错误")[:160])
    return data.get("result")


def _initialize(url: str, connect_s: float, read_s: float) -> None:
    """完成一次 MCP 握手：initialize + notifications/initialized。"""
    _rpc(url, "initialize", {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": _CLIENT_INFO,
    }, connect_s, read_s)
    _rpc(url, "notifications/initialized", None, connect_s, read_s, notify=True)


def _fetch_tools(url: str, connect_s: float, read_s: float) -> List[Dict[str, Any]]:
    _initialize(url, connect_s, read_s)
    result = _rpc(url, "tools/list", {}, connect_s, read_s)
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise RuntimeError("tools/list 未返回工具清单")
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


def _tools(server: str, connect_s: float, read_s: float, force: bool = False) -> List[Dict[str, Any]]:
    """取工具清单：命中 60 秒缓存直接返回，否则重新握手拉取（网络操作放在锁外）。"""
    state = _state(server)
    ttl = float(getattr(config, "MCP_TOOLS_CACHE_SECONDS", 60) or 60)
    with _LOCK:
        if not force and state.tools and (time.time() - state.tools_at) < ttl:
            return list(state.tools)
        url = state.url
    tools = _fetch_tools(url, connect_s, read_s)
    with _LOCK:
        state.tools = tools
        state.tools_at = time.time()
    return list(tools)


def _unwrap_tool_result(result: Any) -> Optional[Dict[str, Any]]:
    """从 MCP tools/call 结果中取出结构化数据；兼容 structuredContent 与 text JSON 两种返回。"""
    if not isinstance(result, dict):
        return None
    if result.get("isError"):
        message = ""
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("text"):
                message = str(item.get("text"))[:160]
                break
        raise RuntimeError(message or "工具执行失败")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip().startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _result(server: str, tool: str, started: float, ok: bool, data: Optional[Dict[str, Any]] = None,
            error: str = "", source: str = "degraded") -> Dict[str, Any]:
    """统一构造返回值，并把结果写回服务状态（在锁内更新）。"""
    latency = int((time.time() - started) * 1000)
    if server in _SERVER_NAMES:
        state = _state(server)
        with _LOCK:
            state.last_ok = ok
            state.last_error = "" if ok else error
            state.last_latency_ms = latency
    return {
        "ok": bool(ok),
        "data": data if ok else None,
        "latency_ms": latency,
        "degraded": not ok,
        "source": source if ok else "degraded",
        "error": error,
        "server": server,
        "tool": tool,
    }


def status() -> Dict[str, Any]:
    """返回 MCP 服务状态。每次调用都实时探测（initialize + tools/list），不抛异常。"""
    urls = _server_urls()
    if not enabled():
        servers = []
        for name in _SERVER_NAMES:
            servers.append({
                "name": name, "url": urls.get(name, ""), "ok": False, "latency_ms": 0,
                "tools": [], "last_error": "MCP 未启用（MCP_ENABLED=0）", "degraded": True,
            })
        return {"enabled": False, "servers": servers, "checked_at": _now_iso()}

    connect_s, read_s = _timeout_seconds()
    servers = []
    for name in _SERVER_NAMES:
        state = _state(name)
        started = time.time()
        tools: List[Dict[str, Any]] = []
        error = ""
        try:
            tools = _tools(name, connect_s, read_s, force=True)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = _describe_error(exc)
            with _LOCK:
                state.tools = []
                state.tools_at = 0.0
        latency = int((time.time() - started) * 1000)
        with _LOCK:
            state.last_ok = ok
            state.last_error = error
            state.last_latency_ms = latency
        servers.append({
            "name": name,
            "url": state.url,
            "ok": ok,
            "latency_ms": latency,
            "tools": [str(t.get("name")) for t in tools],
            "last_error": error,
            "degraded": not ok,
        })
    return {"enabled": True, "servers": servers, "checked_at": _now_iso()}


def call_tool(server: str, tool: Any = None, args: Optional[Dict[str, Any]] = None,
              timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """调用白名单内的 MCP 工具，永不抛异常。

    两种写法等价：call_tool("fx", "fx_convert", {...}) 或 call_tool("fx_convert", {...})。
    timeout_ms 仅用于自测/排障，缺省走配置的连接 2 秒、读 10 秒。
    """
    if isinstance(tool, dict) and args is None and server in _TOOL_SERVERS:
        server, tool, args = _TOOL_SERVERS[server], server, tool
    server_name = str(server or "")
    tool_name = str(tool or "")
    arguments = dict(args or {})
    started = time.time()

    if server_name not in _SERVER_NAMES:
        return _result(server_name, tool_name, started, False, error="未知的 MCP 服务：%s" % server_name)
    if not tool_name:
        return _result(server_name, tool_name, started, False, error="缺少工具名")
    if not enabled():
        return _result(server_name, tool_name, started, False, error="MCP 未启用（MCP_ENABLED=0）")

    connect_s, read_s = _timeout_seconds(timeout_ms)
    try:
        tools = _tools(server_name, connect_s, read_s)
        allowed = {str(t.get("name")) for t in tools}
        if tool_name not in allowed:
            return _result(server_name, tool_name, started, False,
                           error="工具不在白名单：%s" % tool_name)
        state = _state(server_name)
        result = _rpc(state.url, "tools/call", {"name": tool_name, "arguments": arguments}, connect_s, read_s)
        data = _unwrap_tool_result(result)
        if data is None:
            raise RuntimeError("工具返回内容为空")
        source = str(data.get("source") or "mcp")
        return _result(server_name, tool_name, started, True, data=data, source=source)
    except Exception as exc:  # noqa: BLE001
        return _result(server_name, tool_name, started, False, error=_describe_error(exc))


def reset_cache() -> None:
    """清空工具清单缓存（自测/排障用）。"""
    with _LOCK:
        _STATES.clear()
