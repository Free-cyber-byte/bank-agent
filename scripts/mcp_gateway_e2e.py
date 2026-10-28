#!/usr/bin/env python3
"""ZeroStep 通用 MCP 接入端到端测试（v0.6.0，零依赖，仅标准库）。

用途：验证「主服务 -> mcp_registry -> mcp_client -> 银行开放平台演示服务」整条链路，
重点覆盖 v0.6.0 新增能力：MCP_SERVERS 多服务注册表、Bearer / OAuth2 鉴权、
streamable-http 会话（Mcp-Session-Id）与 SSE 响应、只读治理（写操作/白名单/未知工具）、
密钥不出现在 status、坏配置回退、MCP_ENABLED=0 全降级。

脚本在进程内启动 backend/external/bank_mcp_server.py（随机端口），跑完必清理；
不改任何真实外部依赖，也不执行任何资金动作。

用法：
    python scripts/mcp_gateway_e2e.py

退出码：0 全部通过；1 有失败；2 环境不可用。
"""
import json
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app import config, mcp_client, mcp_registry, plugins  # noqa: E402
from external import bank_mcp_server  # noqa: E402

CHECKS = []


def _check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": detail})
    print("%s  %s  %-56s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


def _free_port():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _health(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/health" % int(port), timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _rows(body):
    return {row.get("name"): row for row in (body.get("servers") or [])}


class _ConfigGuard:
    """临时改 config / 环境变量，退出时恢复，避免测试之间串状态。"""

    def __init__(self, servers=None, enabled=None, allow_write=None):
        self.servers = servers
        self.enabled = enabled
        self.allow_write = allow_write

    def __enter__(self):
        self.saved = (config.MCP_SERVERS, config.MCP_ENABLED, config.MCP_ALLOW_WRITE_TOOLS)
        if self.servers is not None:
            config.MCP_SERVERS = self.servers
        if self.enabled is not None:
            config.MCP_ENABLED = self.enabled
        if self.allow_write is not None:
            config.MCP_ALLOW_WRITE_TOOLS = self.allow_write
        mcp_client.reset_cache()
        return self

    def __exit__(self, *exc):
        config.MCP_SERVERS, config.MCP_ENABLED, config.MCP_ALLOW_WRITE_TOOLS = self.saved
        mcp_client.reset_cache()
        return False


def main():
    server = bank_mcp_server.make_server("127.0.0.1", _free_port(), token="demo-bank-token", oauth=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    os.environ["BANK_MCP_TOKEN"] = "demo-bank-token"
    os.environ["BANK_MCP_BAD_TOKEN"] = "wrong-token"
    os.environ["BANK_MCP_SECRET"] = "demo-bank-secret"
    time.sleep(0.15)

    def bearer_spec(name, token_env, tools=None):
        return {
            "name": name,
            "label": "演示银行（%s）" % name,
            "url": base + "/mcp",
            "transport": "streamable-http",
            "tools": tools if tools is not None else ["bank_account_summary"],
            "write_tools": ["bank_transfer"],
            "auth": {"type": "bearer", "token": "env:" + token_env},
        }

    oauth_spec = {
        "name": "bank-oauth",
        "label": "演示银行（OAuth2）",
        "url": base + "/mcp",
        "transport": "streamable-http",
        "tools": ["bank_account_summary"],
        "auth": {
            "type": "oauth2",
            "token_url": base + "/oauth/token",
            "client_id": "demo-bank-client",
            "client_secret": "env:BANK_MCP_SECRET",
        },
    }

    try:
        # 1) 未配置 MCP_SERVERS：保持 v0.5.x 的 fx/risk 内置服务
        with _ConfigGuard(servers=""):
            default = mcp_registry.load()
            names = [spec["name"] for spec in default["servers"]]
            _check("gw-1", "未配置 MCP_SERVERS 时仍是 fx/risk 两个内置服务",
                   names == ["fx", "risk"] and default["error"] == ""
                   and all(spec["transport"] == "http" and spec["legacy"] for spec in default["servers"]),
                   "names=%s" % names)

        # 2) 注册表解析：多服务、顺序、字段归一
        specs = [bearer_spec("bank", "BANK_MCP_TOKEN"),
                 bearer_spec("bank-bad", "BANK_MCP_BAD_TOKEN"),
                 oauth_spec]
        with _ConfigGuard(servers=json.dumps(specs, ensure_ascii=False)):
            loaded = mcp_registry.load()
            names = [spec["name"] for spec in loaded["servers"]]
            bank = [spec for spec in loaded["servers"] if spec["name"] == "bank"][0]
            _check("gw-2", "MCP_SERVERS 解析出 3 个服务且字段归一",
                   names == ["bank", "bank-bad", "bank-oauth"] and loaded["error"] == ""
                   and bank["transport"] == "streamable-http" and bank["read_only"] is True
                   and bank["tools"] == ["bank_account_summary"],
                   "names=%s" % names)

            # 3) status：bank 在线、Bearer 已配置、会话已建立、工具清单来自 tools/list（SSE 链路）
            status = mcp_client.status()
            rows = _rows(status)
            bank_row = rows.get("bank") or {}
            _check("gw-3", "status 显示 bank 在线、Bearer 已配置、会话已建立",
                   status.get("enabled") is True and bank_row.get("ok") is True
                   and bank_row.get("auth", {}).get("type") == "bearer"
                   and bank_row.get("auth", {}).get("configured") is True
                   and bank_row.get("session") is True
                   and "bank_account_summary" in (bank_row.get("tools") or [])
                   and bank_row.get("transport") == "streamable-http",
                   "ok=%s session=%s tools=%s" % (bank_row.get("ok"), bank_row.get("session"),
                                                  bank_row.get("tools")))

            # 4) 只读查询：经会话 + SSE 拿到结构化数据
            result = mcp_client.call_tool("bank", "bank_account_summary", {"account_no": "6222020200001234"})
            data = result.get("data") or {}
            _check("gw-4", "bank_account_summary 经 SSE 返回结构化账户摘要",
                   result.get("ok") is True and result.get("source") == "external-bank"
                   and data.get("balance_cents") == 1286500 and data.get("holder") == "张*"
                   and data.get("account_no_masked", "").endswith("1234")
                   and result.get("read_only") is True,
                   "source=%s balance=%s" % (result.get("source"), data.get("balance_cents")))

            counts = _health(port)["counts"]
            _check("gw-5", "服务端确实收到会话与调用（不是本地伪造）",
                   counts.get("sessions", 0) >= 2 and counts.get("calls", 0) >= 1,
                   "counts=%s" % counts)

            # 6) 写操作：客户端在转发前拒绝，服务端 write_calls 必须保持 0
            before = _health(port)["counts"]
            denied = mcp_client.call_tool("bank", "bank_transfer", {"payee": "李*", "amount": 100})
            after = _health(port)["counts"]
            _check("gw-6", "bank_transfer 被只读边界拒绝且未转发到银行服务",
                   denied.get("ok") is False and "写操作" in (denied.get("error") or "")
                   and denied.get("degraded") is False
                   and after.get("write_calls", 0) == 0
                   and after.get("calls", 0) == before.get("calls", 0),
                   "error=%s write_calls=%s" % ((denied.get("error") or "")[:40], after.get("write_calls")))

            # 7) 白名单：不在配置清单内的工具直接拒绝（连 tools/list 都不依赖）
            before = _health(port)["counts"]
            blocked = mcp_client.call_tool("bank", "bank_quote", {"from": "CNY", "to": "USD", "amount": 100})
            after = _health(port)["counts"]
            _check("gw-7", "白名单外工具 bank_quote 被拒绝且未触达服务",
                   blocked.get("ok") is False and "允许清单" in (blocked.get("error") or "")
                   and after.get("calls", 0) == before.get("calls", 0),
                   "error=%s" % (blocked.get("error") or "")[:60])

            # 8) 错误 Bearer token：鉴权失败被识别为降级，不是崩溃
            bad_row = rows.get("bank-bad") or {}
            _check("gw-8", "错误 token 返回鉴权失败并标降级",
                   bad_row.get("ok") is False and bad_row.get("degraded") is True
                   and "鉴权失败" in (bad_row.get("last_error") or ""),
                   "last_error=%s" % (bad_row.get("last_error") or "")[:60])

            # 9) OAuth2：token 自动获取并缓存，调用成功
            oauth_row = rows.get("bank-oauth") or {}
            oauth_call = mcp_client.call_tool("bank-oauth", "bank_account_summary", {})
            _check("gw-9", "OAuth2 client_credentials 取 token 并成功调用",
                   oauth_row.get("ok") is True
                   and oauth_row.get("auth", {}).get("type") == "oauth2"
                   and oauth_row.get("auth", {}).get("configured") is True
                   and oauth_row.get("auth", {}).get("token_cached") is True
                   and oauth_call.get("ok") is True,
                   "ok=%s token_cached=%s" % (oauth_row.get("ok"),
                                              oauth_row.get("auth", {}).get("token_cached")))

            # 10) 密钥不泄露：status JSON 不得出现 token/secret 明文
            blob = json.dumps(mcp_client.status(), ensure_ascii=False)
            _check("gw-10", "status 不含任何 token / client_secret 明文",
                   "demo-bank-token" not in blob and "demo-bank-secret" not in blob
                   and "bank-oauth-" not in blob and "wrong-token" not in blob,
                   "len=%d" % len(blob))

            # 11) 两参老写法（按工具名找服务）仍可用
            legacy = mcp_client.call_tool("bank_account_summary", {"account_no": "6222020200009999"})
            _check("gw-11", "两参写法 call_tool(tool, args) 兼容",
                   legacy.get("ok") is True and legacy.get("server") == "bank",
                   "server=%s" % legacy.get("server"))

            # 12) 插件能力页动态出现 mcp-bank / mcp-bank-oauth
            page = plugins.status()
            ids = [item["id"] for item in page.get("plugins") or []]
            extra = [item for item in page.get("plugins") or [] if item["id"] == "mcp-bank"]
            _check("gw-12", "能力页动态挂载 mcp-bank 插件",
                   page.get("count") == 10 and "mcp-bank" in ids and "mcp-fx" in ids and "mcp-bank-oauth" in ids
                   and extra and extra[0].get("kind") == "mcp" and extra[0].get("enabled") is True
                   and extra[0].get("detail", {}).get("transport") == "streamable-http",
                   "count=%s ids=%s" % (page.get("count"), ids))

        # 13) MCP_ENABLED=0：全部降级且不发起请求
        with _ConfigGuard(servers=json.dumps([bearer_spec("bank", "BANK_MCP_TOKEN")]), enabled=False):
            body = mcp_client.status()
            row = (body.get("servers") or [{}])[0]
            call = mcp_client.call_tool("bank", "bank_account_summary", {})
            _check("gw-13", "MCP_ENABLED=0 时 status/call 全部降级且无网络请求",
                   body.get("enabled") is False and row.get("ok") is False
                   and "未启用" in (row.get("last_error") or "")
                   and call.get("ok") is False and call.get("degraded") is True,
                   "error=%s" % (row.get("last_error") or "")[:40])

        # 14) 全局写开关也不能放行显式 write_tools（硬拉黑）
        with _ConfigGuard(servers=json.dumps([bearer_spec("bank", "BANK_MCP_TOKEN")]), allow_write=True):
            denied = mcp_client.call_tool("bank", "bank_transfer", {})
            _check("gw-14", "MCP_ALLOW_WRITE_TOOLS=1 也不能放行 write_tools 硬拉黑",
                   denied.get("ok") is False and "写操作" in (denied.get("error") or ""),
                   "error=%s" % (denied.get("error") or "")[:50])

        # 15) 坏 JSON：回退 fx/risk + config_error，不抛异常
        with _ConfigGuard(servers="{不是合法 JSON"):
            body = mcp_client.status()
            names = [row.get("name") for row in body.get("servers") or []]
            _check("gw-15", "坏 MCP_SERVERS 回退 fx/risk 并给出 config_error",
                   names == ["fx", "risk"] and "config_error" in body
                   and "不是合法 JSON" in (body.get("config_error") or ""),
                   "names=%s" % names)

        # 16) 部分非法配置：合法项保留，非法项原因写进 error
        mixed = [bearer_spec("bank", "BANK_MCP_TOKEN"), {"name": "no-url", "url": ""}]
        with _ConfigGuard(servers=json.dumps(mixed, ensure_ascii=False)):
            loaded = mcp_registry.load()
            names = [spec["name"] for spec in loaded["servers"]]
            _check("gw-16", "部分非法配置保留合法服务并说明原因",
                   names == ["bank"] and "url" in loaded["error"],
                   "names=%s error=%s" % (names, (loaded["error"] or "")[:40]))

        # 17) 未知服务 / 未知工具：明确报错，不抛异常
        with _ConfigGuard(servers=json.dumps([bearer_spec("bank", "BANK_MCP_TOKEN")])):
            unknown_server = mcp_client.call_tool("nope", "x", {})
            unknown_tool = mcp_client.call_tool("bank", "nope", {})
            _check("gw-17", "未知服务 / 未知工具返回可读错误且不抛异常",
                   unknown_server.get("ok") is False and "未知的 MCP 服务" in (unknown_server.get("error") or "")
                   and unknown_tool.get("ok") is False
                   and ("未声明工具" in (unknown_tool.get("error") or "")
                        or "允许清单" in (unknown_tool.get("error") or "")),
                   "server_error=%s tool_error=%s" % ((unknown_server.get("error") or "")[:24],
                                                      (unknown_tool.get("error") or "")[:24]))
    finally:
        server.shutdown()
        server.server_close()
        os.environ.pop("BANK_MCP_TOKEN", None)
        os.environ.pop("BANK_MCP_BAD_TOKEN", None)
        os.environ.pop("BANK_MCP_SECRET", None)

    failed = [item for item in CHECKS if not item["ok"]]
    print("\n结果：%d/%d 通过" % (len(CHECKS) - len(failed), len(CHECKS)))
    if failed:
        print("失败项：" + "、".join(item["id"] for item in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
