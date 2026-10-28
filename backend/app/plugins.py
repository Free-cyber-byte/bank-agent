"""ZeroStep 插件注册表（v0.3.0，仿 DSH 的 capability 注册表，零依赖标准库）。

设计要点：
- 统一注册：7 个内置能力（policy/audit/mcp-fx/mcp-risk/intent-llm/skill-pack/channel-im）
  用同一份元数据描述「提供什么 / 依赖什么 / 怎么工作 / 能不能停」；
- 可停用：core 类（权限分级、审计链）是安全底线，toggleable=False，永不允许停用；
  其余插件状态持久化到 config.PLUGINS_FILE（函数内现读，支持 importlib.reload 热切换）；
- 运行态：mcp 类插件启用但外部探测不可达时，status 降级为 "degraded"；探测失败绝不抛异常，
  最坏情况按 "enabled" 展示，保证 /api/plugins 永远可用；
- 审计：set_enabled 成功后写一条 event_type 恰好为 "plugin_toggled" 的审计记录（失败只 log）；
- 本模块不直接联网：mcp 探测复用 mcp_client.status() 的一次结果，避免重复请求。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

# 插件大类元数据（顺序固定，供前端做筛选/徽章）
KINDS: List[Dict[str, str]] = [
    {"id": "core", "name": "核心能力"},
    {"id": "mcp", "name": "外部服务"},
    {"id": "model", "name": "模型通道"},
    {"id": "skill", "name": "技能包"},
    {"id": "channel", "name": "渠道"},
]

# 内置插件注册表（顺序固定）。provides 三个子字段始终存在，缺省为空列表。
REGISTRY: List[Dict[str, Any]] = [
    {
        "id": "policy",
        "name": "权限分级与审批",
        "kind": "core",
        "toggleable": False,
        "description": "对转账、还款等敏感动作做风险分级、二次确认与 TOTP 强验证，是资金安全的第一道闸门。",
        "how": "内置核心能力：按 policy 规则给出风险等级与审批单，敏感动作必须二次确认；不允许停用。",
        "provides": {"tools": [], "skills": [], "routes": ["/api/approval", "/api/demo/totp"]},
        "requires": [],
        "env": [],
        "probe": "",
    },
    {
        "id": "audit",
        "name": "哈希链审计",
        "kind": "core",
        "toggleable": False,
        "description": "每次工具调用、审批与回复都写入 SHA-256 哈希链，可校验、可回放、可导出。",
        "how": "内置核心能力：所有决策与工具调用写审计链，verify 可发现任何篡改；不允许停用。",
        "provides": {"tools": [], "skills": [], "routes": ["/api/audit", "/api/events"]},
        "requires": [],
        "env": [],
        "probe": "",
    },
    {
        "id": "mcp-fx",
        "name": "汇率服务",
        "kind": "mcp",
        "toggleable": True,
        "description": "通过 MCP 调用外部汇率服务，只用于查询与展示；不可用时回退内置参考价，绝不影响资金动作。",
        "how": "启用时优先调用外部 fx_convert 拿实时参考汇率；停用后不发起任何外部请求，直接按内置参考价估算并在回复里标注。",
        "provides": {"tools": ["fx_convert"], "skills": [], "routes": ["/api/mcp/status"]},
        "requires": [],
        "env": ["MCP_ENABLED", "MCP_FX_URL", "MCP_TIMEOUT_MS", "MCP_FX_FALLBACK_RATE"],
        "probe": "fx",
    },
    {
        "id": "mcp-risk",
        "name": "反欺诈风控",
        "kind": "mcp",
        "toggleable": True,
        "description": "转账前调用外部风险名单与评分，只加严不放松；停用后跳过外部核查，policy 安全规则保持生效。",
        "how": "启用时对 create_transfer 做外部风险筛查，命中名单或高风险即拦截；停用后跳过外部核查（审计备注），权限分级与审批照旧。",
        "provides": {"tools": ["risk_screen"], "skills": [], "routes": []},
        "requires": [],
        "env": ["MCP_ENABLED", "MCP_RISK_URL", "MCP_RISK_BLOCK_SCORE"],
        "probe": "risk",
    },
    {
        "id": "intent-llm",
        "name": "模型意图通道",
        "kind": "model",
        "toggleable": True,
        "description": "用大模型解析自然语言意图；未配置 API Key 或停用时自动回退到规则解析，安全判定始终由代码完成。",
        "how": "启用且配置 Key 时由模型解析意图并注入技能卡提示；停用后强制走规则解析，风控、审批与金额校验逻辑不变。",
        "provides": {"tools": ["llm.parse"], "skills": [], "routes": ["/api/llm/mode", "/api/llm/status"]},
        "requires": [],
        "env": ["DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"],
        "probe": "",
    },
    {
        "id": "skill-pack",
        "name": "技能卡包",
        "kind": "skill",
        "toggleable": True,
        "description": "六张领域技能卡（转账/账单/理财/卡片/订阅/跨场景），作为模型提示与前端能力目录。",
        "how": "启用时在 /api/skills 展示六张技能卡；停用后技能列表返回空。",
        "provides": {"tools": [], "skills": ["bills", "cards", "cross", "subscriptions", "transfer", "wealth"],
                     "routes": ["/api/skills"]},
        "requires": [],
        "env": [],
        "probe": "",
    },
    {
        "id": "channel-im",
        "name": "IM 渠道",
        "kind": "channel",
        "toggleable": True,
        "description": "把 IM / 短信网关 webhook 适配成 /api/chat 调用，两步确认，且绝不向渠道回传内部凭据。",
        "how": "启用时渠道服务可转发消息并做 10 秒幂等；停用后渠道 webhook 返回 503「渠道插件已停用」，不写消息日志。",
        "provides": {"tools": [], "skills": [], "routes": ["/api/channel/status"]},
        "requires": [],
        "env": ["CHANNEL_TOKEN", "CHANNEL_PORT", "CHANNEL_ENABLED"],
        "probe": "",
    },
]

_IDS = tuple(item["id"] for item in REGISTRY)
_BY_ID: Dict[str, Dict[str, Any]] = {item["id"]: item for item in REGISTRY}
_LOCK = threading.RLock()
_CONN_GETTER = None


class PluginNotFound(Exception):
    """未知插件 id。"""


class PluginLocked(Exception):
    """核心能力不可停用。"""


# ---------------- 连接注入（审计用） ----------------
def bind_conn(getter) -> None:
    """由 server.py 启动时注入「取当前数据库连接」的函数，供审计写入使用。"""
    global _CONN_GETTER
    _CONN_GETTER = getter


def _conn():
    try:
        return _CONN_GETTER() if _CONN_GETTER else None
    except Exception:  # noqa: BLE001 - 取连接失败只影响审计，不影响插件开关
        return None


# ---------------- 持久化 ----------------
def _plugins_file() -> Path:
    """插件状态文件：每次现读 config，支持 QA 用 importlib.reload(config) 切换路径。"""
    path = getattr(config, "PLUGINS_FILE", None)
    if not path:
        path = Path(getattr(config, "DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "plugins.json"
    return Path(path)


def _load_state() -> Dict[str, bool]:
    """读取持久化状态；文件不存在 / 损坏 / 类型不对一律回退「全部启用」。"""
    path = _plugins_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: Dict[str, bool] = {}
    for plugin_id, value in raw.items():
        if plugin_id in _BY_ID and isinstance(value, bool):
            state[str(plugin_id)] = value
    return state


def _save_state(state: Dict[str, bool]) -> bool:
    """临时文件 + os.replace 原子替换；失败只返回 False，不抛异常。"""
    path = _plugins_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        sys.stderr.write("[plugins] 保存插件状态失败：%s\n" % exc)
        return False


# ---------------- 查询 ----------------
def _spec(plugin_id: str) -> Optional[Dict[str, Any]]:
    return _BY_ID.get(str(plugin_id or "").strip())


def is_enabled(plugin_id: str) -> bool:
    """插件是否启用。未知 id 返回 True（保守：不因注册表异常误关能力）。"""
    spec = _spec(plugin_id)
    if spec is None:
        return True
    if not spec["toggleable"]:
        return True
    return bool(_load_state().get(spec["id"], True))


def _mcp_probe() -> Dict[str, Dict[str, Any]]:
    """复用 mcp_client.status() 的一次探测结果；探测失败返回空表（按启用展示）。"""
    try:
        from . import mcp_client
        body = mcp_client.status()
    except Exception:  # noqa: BLE001 - 外部探测失败绝不能让 /api/plugins 500
        return {}
    rows: Dict[str, Dict[str, Any]] = {}
    if isinstance(body, dict):
        for row in body.get("servers") or []:
            if isinstance(row, dict) and row.get("name"):
                rows[str(row["name"])] = row
    return rows


def _status_of(spec: Dict[str, Any], enabled: bool, mcp_rows: Dict[str, Dict[str, Any]]) -> str:
    if not enabled:
        return "disabled"
    probe = str(spec.get("probe") or "")
    if probe:
        row = mcp_rows.get(probe)
        if isinstance(row, dict) and not row.get("ok"):
            return "degraded"
    return "enabled"


def _item(spec: Dict[str, Any], enabled: bool, mcp_rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    provides = spec.get("provides") or {}
    routes = [str(route) for route in (provides.get("routes") or [])]
    return {
        "id": spec["id"],
        "name": spec["name"],
        "kind": spec["kind"],
        "version": str(getattr(config, "VERSION", "") or ""),
        "description": spec["description"],
        "status": _status_of(spec, enabled, mcp_rows),
        "enabled": bool(enabled),
        "toggleable": bool(spec["toggleable"]),
        "provides": {
            "tools": [str(tool) for tool in (provides.get("tools") or [])],
            "skills": [str(skill) for skill in (provides.get("skills") or [])],
            "routes": routes,
        },
        "requires": [str(req) for req in (spec.get("requires") or [])],
        "detail": {"how": spec["how"], "env": [str(name) for name in (spec.get("env") or [])],
                   "endpoints": routes},
        "locked_reason": "" if spec["toggleable"] else "核心能力不可停用",
    }


def all() -> List[Dict[str, Any]]:
    """返回全部插件（含运行态 status）；只做一次 mcp 探测。"""
    state = _load_state()
    mcp_rows = _mcp_probe()
    return [_item(spec, bool(state.get(spec["id"], True)) if spec["toggleable"] else True, mcp_rows)
            for spec in REGISTRY]


def get(plugin_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取一个插件（不触发网络探测）；不存在返回 None。"""
    spec = _spec(plugin_id)
    if spec is None:
        return None
    return _item(spec, is_enabled(spec["id"]), {})


def counts() -> Dict[str, int]:
    """启用/停用计数（不含 mcp 探测，供 toggle 响应使用）。"""
    state = _load_state()
    enabled = 0
    for spec in REGISTRY:
        if not spec["toggleable"] or state.get(spec["id"], True):
            enabled += 1
    return {"total": len(REGISTRY), "enabled": enabled, "disabled": len(REGISTRY) - enabled}


def status() -> Dict[str, Any]:
    """/api/plugins 的完整响应：注册表 + 运行态 + 计数。永不抛异常。"""
    plugins = all()
    enabled_count = sum(1 for plugin in plugins if plugin.get("enabled"))
    return {
        "ok": True,
        "count": len(plugins),
        "enabled_count": enabled_count,
        "kinds": [dict(kind) for kind in KINDS],
        "plugins": plugins,
    }


# ---------------- 写操作 ----------------
def _audit_toggle(plugin_id: str, enabled: bool, conn=None) -> None:
    """toggle 成功后写审计（事件名必须恰好 plugin_toggled）；失败只 log 不抛。"""
    try:
        from . import audit
        target = conn if conn is not None else _conn()
        if target is None:
            return
        audit.append(target, "plugin_toggled", tool="plugins.toggle",
                     args={"plugin_id": plugin_id, "enabled": bool(enabled)},
                     result={"ok": True, "plugin_id": plugin_id, "enabled": bool(enabled)},
                     risk_level="low")
    except Exception as exc:  # noqa: BLE001 - 审计失败不能阻断插件开关
        sys.stderr.write("[plugins] 写审计失败：%s\n" % exc)


def set_enabled(plugin_id: str, enabled: bool, conn=None) -> Dict[str, Any]:
    """启用/停用一个插件并持久化；未知 id 抛 PluginNotFound，core 插件抛 PluginLocked。"""
    spec = _spec(plugin_id)
    if spec is None:
        raise PluginNotFound(str(plugin_id))
    if not spec["toggleable"]:
        raise PluginLocked(spec["id"])
    enabled = bool(enabled)
    with _LOCK:
        state = _load_state()
        state[spec["id"]] = enabled
        _save_state(state)  # 落盘失败也不影响本次进程内生效（只 log）
    _audit_toggle(spec["id"], enabled, conn=conn)
    return _item(spec, enabled, {})


def reset() -> bool:
    """恢复默认「全部启用」：删除状态文件；删除失败时写入空状态。"""
    with _LOCK:
        path = _plugins_file()
        try:
            if path.exists():
                path.unlink()
            return True
        except OSError as exc:
            sys.stderr.write("[plugins] 重置插件状态失败：%s\n" % exc)
            return _save_state({})
