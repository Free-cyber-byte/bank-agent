"""MCP 服务注册表（v0.6.0）。

把「一个或多个外部 MCP 服务」从环境变量 MCP_SERVERS 解析成标准化描述，
供 mcp_client（调用/探测）与 plugins（能力页）共用。零依赖，仅标准库。

设计约定：
- 解析永不抛异常：配置坏了就回退 fx/risk 内置服务，并在 error 里写明原因，
  由 /api/mcp/status 暴露，界面不会因为一段坏 JSON 白屏。
- 密钥只以 env:VAR / ${VAR} 间接引用，status 里只出现 auth.type 与 configured 布尔，
  不出现任何明文（演示/评审时可直接投屏）。
- 写操作默认拒绝：write_tools 显式拉黑；未显式声明 read_only=False 的服务一律按只读处理。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_TRANSPORTS = ("http", "streamable-http")
_AUTH_TYPES = ("none", "headers", "bearer", "oauth2")
_LEGACY_LABELS = {"fx": "汇率服务", "risk": "反欺诈风控"}

# 只用于「配置里没写 read_only=False 时」的兜底判断；显式 write_tools 优先级更高。
# v0.6.1：不再只看整体前缀（bank_transfer 会被漏判），改为「分词 + 驼峰拆分」后按词判断。
# v0.6.2：补上复核发现的漏判词（card_spend / send_money / funds_move / xfer / authorize /
# apply_loan / close_account / post_entry / book_entry / set_limit 等），原则是「宁可误拦只读，
# 不可漏放写」。
_WRITE_WORDS = frozenset({
    "transfer", "transfers", "remit", "remittance", "wire", "pay", "payment", "payments",
    "payout", "withdraw", "withdrawal", "deposit", "refund", "purchase", "buy", "sell",
    "trade", "repay", "repayment", "redeem", "subscribe", "cancel", "revoke", "delete",
    "remove", "update", "modify", "create", "submit", "execute", "recharge", "topup",
    "settle", "settlement", "charge", "debit", "credit", "disburse", "disbursement",
    # v0.6.2 补充
    "spend", "send", "move", "xfer", "authorize", "authorise", "approve", "loan",
    "close", "open", "post", "book", "import", "upload", "bind", "unbind", "set",
    "limit", "freeze", "unfreeze", "hold", "release", "grant", "revoke", "issue",
    # v0.6.2 补充。注意：convert / exchange / swap / invest / allocate 这类「换算/配置」类
    # 动词刻意不进来——内置只读服务 fx 的工具名就叫 fx_convert，进词表会把只读汇率查询判成
    # 写操作（实测 mcp_e2e 4/7、gw-19 失败）。真正的资金动词（transfer / purchase / withdraw /
    # repay / redeem / charge …）已在上方覆盖，写风险不靠这几个歧义词兜底。
    "spend", "send", "move", "xfer", "authorize", "authorise", "approve", "loan",
    "close", "open", "post", "book", "import", "upload", "bind", "unbind", "set",
    "limit", "freeze", "unfreeze", "hold", "release", "grant", "revoke", "issue",
    "mint", "burn", "adjust",
    "reconcile", "write", "save", "insert", "patch", "put", "sign", "confirm",
    "commit", "apply", "order", "reserve", "booking", "entry", "record", "clear",
})
# 兼容连写（banktransfer / prepayment / overpay）：长度 >=5 的词做前后缀包含判断，
# 太短的词（set / pay）只做整词匹配，避免 asset / offset 这类误伤。
_WRITE_STEMS = frozenset(word for word in _WRITE_WORDS if len(word) >= 5)
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _env_name(value: Any) -> str:
    """把 env:VAR / ${VAR} / 明文统一解析成环境变量名（明文返回空串，表示不是环境变量引用）。"""
    raw = _text(value)
    if raw.startswith("env:"):
        return raw[4:].strip()
    match = _VAR_RE.match(raw)
    return match.group(1) if match else ""


def resolve_value(value: Any) -> str:
    """解析密钥引用：env:VAR / ${VAR} 读环境变量，其余原样返回（仅支持显式配置的明文）。"""
    raw = _text(value)
    name = _env_name(raw)
    if name:
        return os.environ.get(name, "")
    return raw


def _legacy_servers() -> List[Dict[str, Any]]:
    """v0.5.x 的内置 fx/risk：结构不变，只是套上统一描述。"""
    timeout = int(getattr(config, "MCP_TIMEOUT_MS", 10000))
    connect = int(getattr(config, "MCP_CONNECT_TIMEOUT_MS", 2000))
    rows = [
        ("fx", getattr(config, "MCP_FX_URL", "http://127.0.0.1:8300/rpc"), ["fx_convert"]),
        ("risk", getattr(config, "MCP_RISK_URL", "http://127.0.0.1:8301/rpc"), ["risk_screen"]),
    ]
    return [
        {
            "name": name,
            "label": _LEGACY_LABELS[name],
            "url": url,
            "transport": "http",
            "tools": list(tools),
            "write_tools": [],
            "read_only": True,
            "timeout_ms": timeout,
            "connect_timeout_ms": connect,
            "auth": {"type": "none"},
            "legacy": True,
        }
        for name, url, tools in rows
    ]


def _normalize_auth(raw: Any) -> Tuple[Dict[str, Any], str]:
    if raw is None or raw == "":
        return {"type": "none"}, ""
    if isinstance(raw, str):
        # 简写：直接给 token = Bearer 鉴权
        return {"type": "bearer", "token": _text(raw)}, ""
    if not isinstance(raw, dict):
        return {"type": "none"}, "auth 必须是对象或字符串"
    kind = _text(raw.get("type") or "none").lower()
    if kind not in _AUTH_TYPES:
        return {"type": "none"}, "不支持的 auth.type：%s" % kind
    if kind == "none":
        return {"type": "none"}, ""
    if kind == "headers":
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict):
            return {"type": "none"}, "auth.headers 必须是对象"
        clean = {str(k): _text(v) for k, v in headers.items() if _text(k) and _text(v)}
        return {"type": "headers", "headers": clean}, ""
    if kind == "bearer":
        token = raw.get("token") or raw.get("token_env") or ""
        if not _env_name(token) and not _text(token):
            return {"type": "none"}, "auth.type=bearer 缺少 token / token_env"
        return {"type": "bearer", "token": _text(token)}, ""
    # oauth2（client_credentials，演示银行服务已支持）
    auth = {
        "type": "oauth2",
        "token_url": _text(raw.get("token_url")),
        "client_id": _text(raw.get("client_id")),
        "client_secret": _text(raw.get("client_secret") or raw.get("client_secret_env")),
        "scope": _text(raw.get("scope")),
        "grant_type": _text(raw.get("grant_type") or "client_credentials"),
    }
    if not auth["token_url"] or not auth["client_id"] or not auth["client_secret"]:
        return {"type": "none"}, "auth.type=oauth2 需要 token_url / client_id / client_secret"
    return auth, ""


def _normalize(item: Any, index: int) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(item, dict):
        return None, "第 %d 项不是对象" % (index + 1)
    name = _text(item.get("name") or "mcp%d" % (index + 1)).lower()
    if not _NAME_RE.match(name):
        return None, "服务名不合法（需小写字母开头，仅 a-z0-9_-，≤32 字符）：%s" % name
    url = _text(item.get("url"))
    if not url.startswith(("http://", "https://")):
        return None, "%s 缺少合法 url（http/https）" % name
    transport = _text(item.get("transport") or "http").lower()
    if transport in ("streamable", "streamable_http", "streamablehttp"):
        transport = "streamable-http"
    if transport not in _TRANSPORTS:
        return None, "%s 的 transport 只支持 http / streamable-http" % name
    tools_raw = item.get("tools") or []
    if not isinstance(tools_raw, list):
        return None, "%s 的 tools 必须是数组" % name
    tools = [_text(t) for t in tools_raw if _text(t)]
    write_raw = item.get("write_tools") or []
    if not isinstance(write_raw, list):
        return None, "%s 的 write_tools 必须是数组" % name
    write_tools = [_text(t) for t in write_raw if _text(t)]
    auth, auth_error = _normalize_auth(item.get("auth"))
    if auth_error:
        return None, "%s 的 %s" % (name, auth_error)

    def _ms(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(100, min(parsed, 120000))

    return {
        "name": name,
        "label": _text(item.get("label")) or name,
        "url": url,
        "transport": transport,
        "tools": tools,
        "write_tools": write_tools,
        "read_only": item.get("read_only", True) is not False,
        "timeout_ms": _ms(item.get("timeout_ms"), int(getattr(config, "MCP_TIMEOUT_MS", 10000))),
        "connect_timeout_ms": _ms(
            item.get("connect_timeout_ms"), int(getattr(config, "MCP_CONNECT_TIMEOUT_MS", 2000))
        ),
        "auth": auth,
        "legacy": False,
    }, ""


def load() -> Dict[str, Any]:
    """返回 {"servers": [...], "error": ""}；永不抛异常。

    v0.6.1：内置 fx/risk 始终保留（v0.5.x 兼容），MCP_SERVERS 里的服务是「追加」
    而不是「替换」——否则一旦配置了银行服务，汇率/风控插件就会从能力页消失、
    引擎也会失去 fx_convert / risk_screen。配置项与内置服务同名时以配置为准，
    并在 error 里说明（避免静默改变内置行为）。
    """
    legacy = _legacy_servers()
    legacy_names = {spec["name"] for spec in legacy}
    raw = _text(getattr(config, "MCP_SERVERS", "") or os.environ.get("MCP_SERVERS", ""))
    if not raw:
        return {"servers": legacy, "error": ""}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return {"servers": legacy, "error": "MCP_SERVERS 不是合法 JSON，已回退 fx/risk：%s" % exc}
    items = parsed.get("servers") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list) or not items:
        return {"servers": legacy, "error": "MCP_SERVERS 必须是服务数组，已回退 fx/risk"}
    limit = max(1, int(getattr(config, "MCP_MAX_SERVERS", 8)))
    servers: List[Dict[str, Any]] = list(legacy)
    errors: List[str] = []
    seen = set(legacy_names)
    for index, item in enumerate(items[:limit]):
        spec, error = _normalize(item, index)
        if error:
            errors.append(error)
            continue
        name = spec["name"]
        if name in legacy_names:
            # 同名以 MCP_SERVERS 配置为准（例如把 fx 指向自建汇率服务），但必须留痕。
            servers = [spec if existing["name"] == name else existing for existing in servers]
            errors.append("服务名 %s 与内置服务同名，已按 MCP_SERVERS 配置覆盖" % name)
            continue
        if name in seen:
            errors.append("服务名重复：%s" % name)
            continue
        seen.add(name)
        servers.append(spec)
    return {"servers": servers, "error": "；".join(errors)}


def specs() -> List[Dict[str, Any]]:
    return load()["servers"]


def find(name: str) -> Optional[Dict[str, Any]]:
    key = _text(name)
    for spec in specs():
        if spec["name"] == key:
            return spec
    return None


def auth_configured(auth: Dict[str, Any]) -> bool:
    kind = (auth or {}).get("type", "none")
    if kind == "none":
        return False
    if kind == "headers":
        return any(resolve_value(v) for v in (auth.get("headers") or {}).values())
    if kind == "bearer":
        return bool(resolve_value(auth.get("token")))
    if kind == "oauth2":
        return bool(
            resolve_value(auth.get("token_url"))
            and resolve_value(auth.get("client_id"))
            and resolve_value(auth.get("client_secret"))
        )
    return False


def auth_summary(auth: Dict[str, Any]) -> Dict[str, Any]:
    """可公开的鉴权描述：只有类型与「是否已配置」，绝不含密钥。"""
    kind = (auth or {}).get("type", "none")
    return {"type": kind, "configured": auth_configured(auth or {})}


def _write_tokens(tool: Any) -> List[str]:
    """把工具名拆成小写词：bank_transfer / bankTransfer / bank.transfer -> [bank, transfer]。"""
    name = _text(tool)
    if not name:
        return []
    name = _CAMEL_RE.sub("_", name)  # bankTransfer -> bank_Transfer
    return [token for token in _SEPARATOR_RE.split(name.lower()) if token]


def looks_write_tool(tool: Any) -> bool:
    """按分词判断是否写风险工具：整词命中或长词前后缀包含都算写。"""
    for token in _write_tokens(tool):
        if token in _WRITE_WORDS:
            return True
        if any(token.startswith(stem) or token.endswith(stem) for stem in _WRITE_STEMS):
            return True
    return False


def is_write_tool(spec: Dict[str, Any], tool: str) -> bool:
    """显式 write_tools 直接判写；只读服务里名字像写操作的工具也判写（防御性）。"""
    name = _text(tool)
    if name in {str(t) for t in spec.get("write_tools") or []}:
        return True
    return looks_write_tool(name)


def write_allowed(spec: Dict[str, Any], tool: str) -> bool:
    """只读四道闸里的第 2 道：写操作硬拦（先于白名单，见 docs/mcp.md §11.3）。

    1) write_tools 显式拉黑：无论白名单 / read_only / 全局开关，一律拒绝；
    2) 名字分词后像资金动作（bank_transfer、funds.transfer、bankTransfer、remit、
       wire、buy、sell、trade、repay …）：同样一律拒绝——不依赖服务声明，也不
       因为 tools 留空（[] 表示不设白名单）就整段跳过；
    3) 只有 read_only=False 的服务，在 MCP_ALLOW_WRITE_TOOLS=1 时，非写风险工具
       才可能放行（默认仍拒绝）。
    """
    name = _text(tool)
    if name in {str(t) for t in spec.get("write_tools") or []}:
        return False
    if looks_write_tool(name):
        return False
    if spec.get("read_only", True):
        return True
    return bool(getattr(config, "MCP_ALLOW_WRITE_TOOLS", False))
