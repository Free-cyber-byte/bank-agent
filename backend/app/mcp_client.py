"""ZeroStep MCP 客户端（v0.6.0，零依赖，仅标准库）。

一句话：把「外部银行/渠道提供的只读能力」按 JSON-RPC over HTTP 接进来，任何一个
服务连不上、超时、鉴权失败、返回垃圾，都只让对应能力降级，绝不影响资金主流程。

v0.6.0 相比 v0.5.x 的能力：
- 多服务：服务清单来自 mcp_registry（MCP_SERVERS 环境变量声明任意个服务），
  不再把 fx/risk 写死在代码里；
- 鉴权：none / 静态 headers / bearer token / OAuth2 client_credentials（token 自动缓存与刷新）；
- 传输：http（一次请求一连接）与 streamable-http（Mcp-Session-Id 会话 + SSE 响应）；
- 只读治理：只调用服务 tools/list 声明过的工具；写操作默认拒绝（显式 write_tools 硬拉黑，
  名字像转账/支付/创建的工具在只读服务里也拒绝），MCP_ALLOW_WRITE_TOOLS=1 才放开；
- 登记：/api/mcp/status 对每个服务给出 label / transport / auth 摘要（不含任何密钥）/ 会话状态。

安全边界（评审可逐条验证）：
1. 密钥只从环境变量读（配置里写 env:VAR 或 ${VAR}），status 与日志只出现类型和布尔；
2. Engine 只用 MCP 的「查」能力（汇率、风控、账户摘要），写操作永远走本系统
   policy → approval → second_factor 链路，MCP 不参与资金动作；
3. 所有返回都是 dict（ok/data/error/degraded），调用方不需要 try/except。
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import config, mcp_registry

_JSONRPC = "2.0"
_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "zerostep-backend", "version": "0.6.0"}
_TOOLS_CACHE_SECONDS = 60
_DEFAULT_SOURCE = "external-%s"

_LOCK = threading.RLock()


class _ServerState:
    """单个外部服务的运行态：工具清单缓存、会话、OAuth token、最近一次健康信息。"""

    __slots__ = (
        "name", "fingerprint", "tools", "tools_at", "last_error", "last_latency_ms",
        "last_ok", "session_id", "token", "token_exp", "initialized",
    )

    def __init__(self, name: str, url: str, fingerprint: str) -> None:
        self.name = name
        self.fingerprint = fingerprint
        self.tools: List[Dict[str, Any]] = []
        self.tools_at = 0.0
        self.last_error = ""
        self.last_latency_ms = 0
        self.last_ok = False
        self.session_id = ""
        self.token = ""
        self.token_exp = 0.0
        self.initialized = False


_STATES: Dict[str, _ServerState] = {}


def _iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def enabled() -> bool:
    return bool(getattr(config, "MCP_ENABLED", True))


def _spec_fingerprint(spec: Dict[str, Any]) -> str:
    keys = ("url", "transport", "auth", "tools", "write_tools", "read_only", "timeout_ms")
    return json.dumps({key: spec.get(key) for key in keys}, sort_keys=True, default=str)


def _state(server: Any) -> _ServerState:
    """按服务名取运行态；配置变化（url/鉴权/白名单）时自动换新，避免串用旧会话或旧 token。"""
    if isinstance(server, dict):
        spec = server
    else:
        spec = mcp_registry.find(str(server))
        if spec is None:
            spec = {"name": str(server), "url": "", "transport": "http", "auth": {"type": "none"}}
    fingerprint = _spec_fingerprint(spec)
    with _LOCK:
        state = _STATES.get(spec["name"])
        if state is None or state.fingerprint != fingerprint:
            state = _ServerState(spec["name"], spec.get("url", ""), fingerprint)
            _STATES[spec["name"]] = state
        return state


def reset_cache() -> None:
    """清空工具清单缓存、会话与 OAuth token（测试与配置热切换用）。"""
    with _LOCK:
        _STATES.clear()


# --------------------------------------------------------------------------- HTTP


def _resolve_timeouts(spec: Dict[str, Any], timeout_ms: Optional[int]) -> Tuple[float, float]:
    connect_ms = int(spec.get("connect_timeout_ms") or getattr(config, "MCP_CONNECT_TIMEOUT_MS", 2000))
    read_ms = int(timeout_ms or spec.get("timeout_ms") or getattr(config, "MCP_TIMEOUT_MS", 10000))
    return max(0.1, connect_ms / 1000.0), max(0.1, read_ms / 1000.0)


def _rpc_headers(spec: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Connection": "close",
    }
    if spec.get("transport") == "streamable-http":
        headers["MCP-Protocol-Version"] = _PROTOCOL_VERSION
    return headers


def _auth_headers(spec: Dict[str, Any], state: _ServerState, timeout_s: float) -> Dict[str, str]:
    auth = spec.get("auth") or {}
    kind = auth.get("type", "none")
    if kind == "none":
        return {}
    if kind == "headers":
        resolved = {str(k): mcp_registry.resolve_value(v) for k, v in (auth.get("headers") or {}).items()}
        return {key: value for key, value in resolved.items() if value}
    if kind == "bearer":
        token = mcp_registry.resolve_value(auth.get("token"))
        return {"Authorization": "Bearer " + token} if token else {}
    if kind == "oauth2":
        token = _oauth_token(spec, state, timeout_s)
        return {"Authorization": "Bearer " + token} if token else {}
    return {}


def _oauth_token(spec: Dict[str, Any], state: _ServerState, timeout_s: float) -> str:
    """client_credentials 取 token；过期前 MCP_OAUTH_REFRESH_SKEW 秒自动换新。"""
    auth = spec.get("auth") or {}
    skew = int(getattr(config, "MCP_OAUTH_REFRESH_SKEW", 60))
    now = time.time()
    with _LOCK:
        if state.token and now < state.token_exp - skew:
            return state.token
    token_url = mcp_registry.resolve_value(auth.get("token_url"))
    client_id = mcp_registry.resolve_value(auth.get("client_id"))
    client_secret = mcp_registry.resolve_value(auth.get("client_secret"))
    if not token_url or not client_id or not client_secret:
        raise RuntimeError("OAuth2 鉴权配置不完整（token_url / client_id / client_secret）")
    body = urllib.parse.urlencode({
        "grant_type": auth.get("grant_type") or "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        **({"scope": auth["scope"]} if auth.get("scope") else {}),
    }).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=min(timeout_s, 10.0)) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("OAuth2 取 token 失败：%s" % exc) from exc
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("OAuth2 响应缺少 access_token")
    try:
        expires_in = int(payload.get("expires_in") or 300)
    except (TypeError, ValueError):
        expires_in = 300
    with _LOCK:
        state.token = token
        state.token_exp = now + max(1, expires_in)
    return token


def _post(spec: Dict[str, Any], payload: Dict[str, Any], headers: Dict[str, str],
          connect_s: float, read_s: float) -> Tuple[int, List[Tuple[str, str]], str]:
    """POST 一帧 JSON-RPC，返回 (状态码, 响应头, 响应体文本)。"""
    parts = urllib.parse.urlsplit(spec.get("url") or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RuntimeError("服务地址不合法：%s" % spec.get("url"))
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=connect_s)
    try:
        conn.connect()
        conn.sock.settimeout(read_s)
        conn.request("POST", path, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, response.getheaders(), raw.decode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _header(headers: List[Tuple[str, str]], name: str) -> str:
    target = name.lower()
    for key, value in headers:
        if str(key).lower() == target:
            return str(value)
    return ""


def _parse_sse(text: str) -> List[Dict[str, Any]]:
    """把 text/event-stream 响应拆成若干 JSON 负载（MCP 每个事件一行 data:）。"""
    payloads: List[Dict[str, Any]] = []
    data_lines: List[str] = []

    def flush() -> None:
        if not data_lines:
            return
        chunk = "\n".join(data_lines)
        data_lines.clear()
        try:
            parsed = json.loads(chunk)
        except ValueError:
            return
        if isinstance(parsed, dict):
            payloads.append(parsed)

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    return payloads


def _decode_response(text: str, content_type: str, request_id: Any) -> Dict[str, Any]:
    if "text/event-stream" in (content_type or "").lower() or text.lstrip().startswith(("event:", "data:")):
        payloads = _parse_sse(text)
    else:
        payloads = []
    if not payloads:
        try:
            parsed = json.loads(text or "{}")
        except ValueError:
            raise RuntimeError("服务响应不是合法 JSON：%s" % text[:120])
        if isinstance(parsed, dict):
            payloads = [parsed]
    for payload in payloads:
        if payload.get("id") == request_id and ("result" in payload or "error" in payload):
            return payload
    for payload in reversed(payloads):
        if "result" in payload or "error" in payload:
            return payload
    raise RuntimeError("服务响应里没有 JSON-RPC 结果")


def _rpc(spec: Dict[str, Any], state: _ServerState, method: str, params: Any,
         timeout_ms: Optional[int] = None, notify: bool = False, retry_auth: bool = True) -> Any:
    connect_s, read_s = _resolve_timeouts(spec, timeout_ms)
    request_id = "%s-%d" % (method.replace("/", "-"), int(time.time() * 1000) % 1000000)
    payload: Dict[str, Any] = {"jsonrpc": _JSONRPC, "method": method, "params": params or {}}
    if not notify:
        payload["id"] = request_id
    headers = _rpc_headers(spec)
    headers.update(_auth_headers(spec, state, read_s))
    with _LOCK:
        session_id = state.session_id
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        status, resp_headers, text = _post(spec, payload, headers, connect_s, read_s)
    except socket.timeout as exc:
        label = "连接超时（%.1fs）" % connect_s if "connect" in str(exc).lower() else "读取超时（%.1fs）" % read_s
        raise RuntimeError(label) from exc
    except (ConnectionError, OSError) as exc:
        raise RuntimeError("连接失败：%s" % exc) from exc

    new_session = _header(resp_headers, "Mcp-Session-Id")
    if new_session:
        with _LOCK:
            state.session_id = new_session

    if status in (401, 403):
        auth = spec.get("auth") or {}
        if auth.get("type") == "oauth2" and retry_auth:
            with _LOCK:
                state.token = ""
                state.token_exp = 0.0
            return _rpc(spec, state, method, params, timeout_ms=timeout_ms, notify=notify, retry_auth=False)
        raise RuntimeError("鉴权失败（HTTP %d）：请检查 token / OAuth 配置" % status)
    if status >= 400:
        raise RuntimeError("服务返回 HTTP %d：%s" % (status, text.strip()[:160]))
    if notify:
        return None
    content_type = _header(resp_headers, "Content-Type")
    frame = _decode_response(text, content_type, request_id)
    if frame.get("error"):
        error = frame["error"] if isinstance(frame["error"], dict) else {"message": frame["error"]}
        raise RuntimeError("服务错误：%s" % (error.get("message") or error))
    return frame.get("result")


def _initialize(spec: Dict[str, Any], state: _ServerState, timeout_ms: Optional[int]) -> Dict[str, Any]:
    result = _rpc(spec, state, "initialize", {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": _CLIENT_INFO,
    }, timeout_ms=timeout_ms) or {}
    _rpc(spec, state, "notifications/initialized", {}, timeout_ms=timeout_ms, notify=True)
    with _LOCK:
        state.initialized = True
    return result


def _fetch_tools(spec: Dict[str, Any], state: _ServerState, timeout_ms: Optional[int]) -> List[Dict[str, Any]]:
    if not state.initialized:
        _initialize(spec, state, timeout_ms)
    result = _rpc(spec, state, "tools/list", {}, timeout_ms=timeout_ms) or {}
    tools = [tool for tool in (result.get("tools") or []) if isinstance(tool, dict) and tool.get("name")]
    with _LOCK:
        state.tools = tools
        state.tools_at = time.time()
    return tools


def _list_tools(spec: Dict[str, Any], state: _ServerState, timeout_ms: Optional[int],
                force: bool = False) -> List[Dict[str, Any]]:
    with _LOCK:
        cached = list(state.tools)
        fresh = bool(state.tools_at) and (time.time() - state.tools_at) < _TOOLS_CACHE_SECONDS
    if cached and fresh and not force:
        return cached
    return _fetch_tools(spec, state, timeout_ms)


def _unwrap(result: Any) -> Dict[str, Any]:
    """把 MCP 的 content/structuredContent 统一成 data 字典；isError 当失败。"""
    if isinstance(result, dict) and result.get("isError"):
        text = ""
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text += str(item.get("text") or "")
        raise RuntimeError("服务执行失败：%s" % (text[:200] or "isError=true"))
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text") or "")
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {key: value for key, value in result.items() if key != "content"}
    if isinstance(result, (int, float, str)):
        return {"value": result}
    return {}


# --------------------------------------------------------------------------- 对外 API


def _source(spec: Dict[str, Any]) -> str:
    return str(spec.get("source") or _DEFAULT_SOURCE % spec["name"])


def _failure(spec: Optional[Dict[str, Any]], server: str, tool: Optional[str], message: str,
             started: float, degraded: bool = True) -> Dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "latency_ms": int((time.time() - started) * 1000),
        "degraded": degraded,
        "source": _source(spec) if spec else "external-%s" % server,
        "error": message,
        "server": server,
        "tool": tool,
    }


def _tool_owner(tool: str) -> str:
    """两参写法 call_tool("fx_convert", args) 用：先从配置白名单，再从工具清单缓存找归属服务。"""
    name = str(tool)
    for spec in mcp_registry.specs():
        if name in {str(t) for t in spec.get("tools") or []}:
            return spec["name"]
    with _LOCK:
        for state in _STATES.values():
            for item in state.tools:
                if item.get("name") == name:
                    return state.name
    return ""


def call_tool(server: str, tool: Any = None, args: Optional[Dict[str, Any]] = None,
              timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """调用外部只读 MCP 工具；永远返回 dict，不抛异常。

    兼容两种写法：
      call_tool("fx", "fx_convert", {...})   # 显式服务 + 工具
      call_tool("fx_convert", {...})         # 老写法：按工具名找服务
    """
    started = time.time()
    if isinstance(tool, dict) and args is None:
        owner = _tool_owner(server)
        if owner:
            server, tool, args = owner, server, tool
        else:
            return _failure(None, str(server), None, "未知的 MCP 工具：%s" % server, started, degraded=False)
    tool_name = str(tool or "")
    if not tool_name:
        return _failure(None, str(server), None, "缺少工具名", started, degraded=False)

    spec = mcp_registry.find(str(server))
    if spec is None:
        return _failure(None, str(server), tool_name, "未知的 MCP 服务：%s" % server, started, degraded=False)
    if not enabled():
        return _failure(spec, spec["name"], tool_name, "MCP 未启用（MCP_ENABLED=0）", started)

    state = _state(spec)
    call_args = args or {}
    try:
        # 写治理优先于白名单：显式 write_tools / 名字像资金动作的工具，无论是否在
        # 允许清单里都直接拒绝，避免「白名单写错就把转账放进来」。
        if not mcp_registry.write_allowed(spec, tool_name):
            return _failure(spec, spec["name"], tool_name,
                            "写操作已拒绝：ZeroStep 对 MCP 只读调用（%s 只允许查询类工具）" % spec["name"],
                            started, degraded=False)
        allowed = {str(t) for t in spec.get("tools") or []}
        if allowed and tool_name not in allowed:
            return _failure(spec, spec["name"], tool_name,
                            "工具 %s 不在该服务的允许清单内" % tool_name, started, degraded=False)
        listed = _list_tools(spec, state, timeout_ms)
        if tool_name not in {str(item.get("name")) for item in listed}:
            return _failure(spec, spec["name"], tool_name,
                            "服务未声明工具 %s（tools/list 中不存在）" % tool_name, started, degraded=False)
        if spec.get("transport") == "streamable-http" and not state.initialized:
            _initialize(spec, state, timeout_ms)
        result = _rpc(spec, state, "tools/call", {"name": tool_name, "arguments": call_args},
                      timeout_ms=timeout_ms)
        data = _unwrap(result)
        data.setdefault("source", _source(spec))
        latency = int((time.time() - started) * 1000)
        with _LOCK:
            state.last_ok = True
            state.last_error = ""
            state.last_latency_ms = latency
        return {
            "ok": True,
            "data": data,
            "latency_ms": latency,
            "degraded": False,
            "source": _source(spec),
            "error": "",
            "server": spec["name"],
            "tool": tool_name,
            "read_only": True,
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc) or type(exc).__name__
        with _LOCK:
            state.last_ok = False
            state.last_error = message
            state.last_latency_ms = int((time.time() - started) * 1000)
        return _failure(spec, spec["name"], tool_name, message, started)


def status() -> Dict[str, Any]:
    """各服务健康快照：ok / latency_ms / tools / last_error / degraded，永不抛异常。"""
    loaded = mcp_registry.load()
    checked_at = _iso()
    rows: List[Dict[str, Any]] = []
    for spec in loaded["servers"]:
        started = time.time()
        state = _state(spec)
        auth = mcp_registry.auth_summary(spec.get("auth") or {})
        with _LOCK:
            token_cached = bool(state.token)
            session_id = state.session_id
        row: Dict[str, Any] = {
            "name": spec["name"],
            "label": spec.get("label") or spec["name"],
            "url": spec.get("url", ""),
            "transport": spec.get("transport", "http"),
            "ok": False,
            "latency_ms": 0,
            "tools": [],
            "last_error": "",
            "degraded": True,
            "auth": {**auth, "token_cached": token_cached},
            "session": bool(session_id),
            "read_only": bool(spec.get("read_only", True)),
        }
        if not enabled():
            row["last_error"] = "MCP 未启用（MCP_ENABLED=0）"
            rows.append(row)
            continue
        try:
            tools = _list_tools(spec, state, None, force=True)
            row["tools"] = sorted(str(item.get("name")) for item in tools)
            row["ok"] = True
            row["degraded"] = False
            row["last_error"] = ""
        except Exception as exc:  # noqa: BLE001
            row["last_error"] = str(exc) or type(exc).__name__
        row["latency_ms"] = int((time.time() - started) * 1000)
        with _LOCK:
            row["session"] = bool(state.session_id)
            row["auth"]["token_cached"] = bool(state.token)
            state.last_ok = row["ok"]
            state.last_error = row["last_error"] if not row["ok"] else ""
            state.last_latency_ms = row["latency_ms"]
        rows.append(row)
    body: Dict[str, Any] = {
        "enabled": enabled(),
        "servers": rows,
        "checked_at": checked_at,
    }
    if loaded.get("error"):
        body["config_error"] = loaded["error"]
    return body


def discover(server: Any) -> Dict[str, Any]:
    """探测单个服务的能力清单（只读 tools/list，不调用任何工具）。

    接真实银行开放平台时的第一步自检：确认端点可达、鉴权通过、工具名与
    本地白名单是否对得上，并标出「看起来会动钱」的工具，方便决定要不要
    写进 tools 白名单。永不抛异常，失败原因放 error。
    """
    spec = server if isinstance(server, dict) else mcp_registry.find(str(server))
    if spec is None:
        return {
            "name": str(server),
            "ok": False,
            "tools": [],
            "writes": [],
            "error": "未在 MCP_SERVERS / 内置服务中找到该服务",
        }
    row: Dict[str, Any] = {
        "name": spec["name"],
        "label": spec.get("label") or spec["name"],
        "url": spec.get("url", ""),
        "transport": spec.get("transport", "http"),
        "auth": mcp_registry.auth_summary(spec.get("auth") or {}),
        "read_only": bool(spec.get("read_only", True)),
        "ok": False,
        "tools": [],
        "writes": [],
        "session": False,
        "error": "",
    }
    if not enabled():
        row["error"] = "MCP 未启用（MCP_ENABLED=0）"
        return row
    state = _state(spec)
    try:
        tools = _list_tools(spec, state, None, force=True)
    except Exception as exc:  # noqa: BLE001
        row["error"] = str(exc) or type(exc).__name__
        return row
    names = sorted(str(item.get("name")) for item in tools)
    row["tools"] = names
    row["writes"] = [name for name in names if mcp_registry.is_write_tool(spec, name)]
    row["ok"] = True
    with _LOCK:
        row["auth"] = {**row["auth"], "token_cached": bool(state.token)}
        row["session"] = bool(state.session_id)
    return row
