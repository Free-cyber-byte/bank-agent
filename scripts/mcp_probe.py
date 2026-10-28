#!/usr/bin/env python3
"""ZeroStep MCP 服务自检工具（v0.6.0，零依赖，仅标准库）。

用途：接入真实银行开放平台时的第一步。读当前 MCP_SERVERS 配置，对每个服务
做一次「初始化 → tools/list」探测（只读，绝不调用任何工具），打印工具清单，
并标出「看起来会动钱」的工具，方便决定哪些写进 tools 白名单、哪些必须排除。

典型用法：
    set MCP_SERVERS=[{"name":"bank", ...}]
    python scripts/mcp_probe.py                 # 探测全部已配置服务
    python scripts/mcp_probe.py bank            # 只探测指定服务
    python scripts/mcp_probe.py --json          # 机器可读（便于粘贴给评审/排障）

退出码：0 全部探测成功；1 至少一个服务失败；2 配置/依赖不可用。
"""
import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app import mcp_client, mcp_registry  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 2


def _targets(name):
    specs = mcp_registry.specs()
    if not name:
        return specs
    spec = mcp_registry.find(name)
    return [spec] if spec is not None else []


def _print_row(row):
    mark = "OK  " if row.get("ok") else "FAIL"
    auth = row.get("auth") or {}
    bits = "%s / %s / auth=%s" % (row.get("transport", "http"), row.get("url", "") or "-", auth.get("type", "none"))
    if auth.get("configured") is not None:
        bits += "(已配置)" if auth.get("configured") else "(未配置)"
    if row.get("session"):
        bits += " / session"
    print("[%s] %s（%s）" % (mark, row.get("name"), row.get("label") or row.get("name")))
    print("       %s" % bits)
    if row.get("error"):
        print("       错误：%s" % row["error"])
        return
    tools = row.get("tools") or []
    # 写风险标注与写闸（mcp_registry.write_allowed / looks_write_tool）共用同一套
    # 分词判定：bank_transfer / funds.transfer / bankTransfer / remit / wire 等都能标出，
    # 不依赖服务端 discover 的 writes 字段是否完整。
    writes = {str(tool) for tool in (row.get("writes") or [])}
    writes.update(tool for tool in tools if mcp_registry.looks_write_tool(tool))
    print("       工具 %d 个%s" % (len(tools), "" if tools else "（服务未声明任何工具）"))
    for tool in tools:
        print("         - %-34s %s" % (tool, "写风险：勿加入白名单" if tool in writes else "只读"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="探测 MCP 服务工具清单（只读，不执行工具）")
    parser.add_argument("server", nargs="?", help="服务名；省略则探测全部已配置服务")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    targets = _targets(args.server)
    if not targets:
        print("未找到服务 %r：请检查 MCP_SERVERS 配置（或服务名拼写）" % args.server, file=sys.stderr)
        return EXIT_ENV

    rows = []
    for spec in targets:
        row = mcp_client.discover(spec["name"])
        rows.append(row)
        if not args.json:
            _print_row(row)

    if args.json:
        body = {
            "enabled": bool(mcp_client.enabled()),
            "count": len(rows),
            "ok": all(row.get("ok") for row in rows),
            "servers": rows,
        }
        print(json.dumps(body, ensure_ascii=False, indent=2))

    failed = [row["name"] for row in rows if not row.get("ok")]
    if args.json:
        return EXIT_OK if not failed else EXIT_FAIL
    print()
    if failed:
        print("结果：%d 个服务探测失败 -> %s" % (len(failed), ", ".join(failed)))
        return EXIT_FAIL
    print("结果：%d/%d 个服务探测成功" % (len(rows), len(rows)))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
