"""外部 MCP 服务自测：进程内起 fx / risk 两个服务，覆盖 5 个场景。

运行：
    python backend/external/selftest.py

输出 PASS / FAIL 与 5/5 汇总；退出码 0 表示全部通过。
场景：1 initialize 健康检查  2 tools/list 与 60s 缓存  3 tools/call 数据正确
      4 慢服务读超时降级  5 服务关闭后降级（均不抛异常）
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Tuple

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app import config, mcp_client  # noqa: E402
from external import fx_server, risk_server  # noqa: E402

_RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, fn: Callable[[], None]) -> None:
    """执行一个场景：断言失败记 FAIL 但不中断，保证 5 个场景都跑完。"""
    try:
        fn()
    except AssertionError as exc:
        _RESULTS.append((name, False, str(exc)[:160] or "断言失败"))
    except Exception as exc:  # noqa: BLE001
        _RESULTS.append((name, False, "%s: %s" % (type(exc).__name__, exc)))
    else:
        _RESULTS.append((name, True, ""))


def _start(module: Any, port: int = 0, slow: float = 0.0) -> Tuple[Any, str]:
    """进程内启动一个服务，返回 (httpd, /rpc 地址)；端口 0 表示自动分配。"""
    httpd = module.make_server("127.0.0.1", port, slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/rpc" % httpd.server_address[1]


def _servers_by_name(status: dict) -> dict:
    return {item.get("name"): item for item in status.get("servers") or []}


def scenario_initialize() -> None:
    """场景 1：两个服务都能完成 initialize 握手并暴露工具。"""
    status = mcp_client.status()
    assert status.get("enabled") is True, "MCP 应处于启用状态"
    servers = _servers_by_name(status)
    assert set(servers) == {"fx", "risk"}, "status 应同时返回 fx 与 risk"
    for name in ("fx", "risk"):
        info = servers[name]
        assert info["ok"] is True, "%s 服务不可用：%s" % (name, info.get("last_error"))
        assert info["degraded"] is False
        assert int(info["latency_ms"]) >= 0
    assert "fx_convert" in servers["fx"]["tools"], "fx 工具清单缺少 fx_convert"
    assert "risk_screen" in servers["risk"]["tools"], "risk 工具清单缺少 risk_screen"


def scenario_tools_list() -> None:
    """场景 2：tools/list 拉取成功，且 60 秒内命中缓存不重复握手。"""
    first = mcp_client.call_tool("fx_convert", {"from": "CNY", "to": "USD", "amount": 100.0})
    assert first["ok"] is True, first.get("error")
    cached_at = mcp_client._state("fx").tools_at
    assert cached_at > 0, "工具清单未被缓存"
    second = mcp_client.call_tool("fx_convert", {"from": "CNY", "to": "USD", "amount": 100.0})
    assert second["ok"] is True, second.get("error")
    assert mcp_client._state("fx").tools_at == cached_at, "60 秒内应复用工具清单缓存"
    assert time.time() - cached_at < 60


def scenario_call() -> None:
    """场景 3：tools/call 返回结构与数据正确，且兼容两参写法。"""
    fx = mcp_client.call_tool("fx_convert", {"from": "CNY", "to": "USD", "amount": 1000.0})
    assert fx["ok"] is True, fx.get("error")
    assert fx["degraded"] is False
    assert fx["source"] == "external-fx", "fx 成功时 source 应为 external-fx"
    data = fx["data"] or {}
    assert data.get("from") == "CNY" and data.get("to") == "USD"
    assert float(data.get("amount") or 0) == 1000.0
    assert 0.0 < float(data.get("rate") or 0.0) < 1.0, "CNY→USD 汇率应在 0 到 1 之间"
    converted = data.get("converted")
    assert isinstance(converted, (int, float)) and converted > 0
    assert abs(float(converted) - 1000.0 * float(data["rate"])) < 1.0
    assert data.get("as_of"), "data 应带 as_of"
    assert data.get("source") == "external-fx", "data 内也应带 source=external-fx"

    risk = mcp_client.call_tool("risk_screen", {
        "payee_name": "张伟", "payee_account": "6214830212346688",
        "amount": 6000.0, "user_id": "u001",
    })
    assert risk["ok"] is True, risk.get("error")
    assert risk["source"] == "external-risk"
    risk_data = risk["data"] or {}
    assert risk_data.get("hit") is True, "张* + 大额应命中演示风控"
    assert risk_data.get("risk_level") == "high", "高风险应返回 risk_level=high"
    assert risk_data.get("blocked") is True, "高风险应 blocked=true"
    assert risk_data.get("list_name") == "演示风控名单"
    assert isinstance(risk_data.get("score"), int) and risk_data["score"] > 0
    assert isinstance(risk_data.get("reasons"), list) and risk_data["reasons"]
    assert isinstance(risk_data.get("hits"), list) and risk_data["hits"]
    assert risk_data.get("advice"), "应返回 advice 建议"

    low = mcp_client.call_tool("risk_screen", {"payee_name": "妈妈", "amount": 800.0})
    assert low["ok"] is True, low.get("error")
    low_data = low["data"] or {}
    assert low_data.get("risk_level") == "low" and low_data.get("blocked") is False

    legacy = mcp_client.call_tool("fx", "fx_convert", {"base": "CNY", "quote": "JPY", "amount_cents": 10000})
    assert legacy["ok"] is True, legacy.get("error")
    assert (legacy["data"] or {}).get("converted_cents", 0) > 0


def scenario_timeout(slow_url: str) -> None:
    """场景 4：慢服务触发读超时，返回 ok=False 而不是抛异常。"""
    original = config.MCP_FX_URL
    config.MCP_FX_URL = slow_url
    try:
        started = time.time()
        result = mcp_client.call_tool("fx", "fx_convert",
                                      {"from": "CNY", "to": "USD", "amount": 100.0},
                                      timeout_ms=300)
        elapsed = time.time() - started
        assert result["ok"] is False, "慢服务应降级"
        assert result["degraded"] is True
        assert result["data"] is None
        assert "超时" in (result["error"] or ""), "error 应说明超时：%s" % result["error"]
        assert elapsed < 3.0, "读超时应快速返回，实际 %.2fs" % elapsed
    except Exception as exc:  # noqa: BLE001
        raise AssertionError("超时场景不应抛异常：%s" % exc)
    finally:
        config.MCP_FX_URL = original


def scenario_close(httpds: List[Any]) -> None:
    """场景 5：服务关闭后 status 显示离线、call_tool 降级，且都不抛异常。"""
    for httpd in httpds:
        httpd.shutdown()
        httpd.server_close()
    status = mcp_client.status()
    assert status.get("enabled") is True
    for item in status.get("servers") or []:
        assert item["ok"] is False, "%s 关闭后仍显示在线" % item["name"]
        assert item["degraded"] is True
        assert item["last_error"], "离线时应有可读的 last_error"
    result = mcp_client.call_tool("fx_convert", {"from": "CNY", "to": "USD", "amount": 100.0})
    assert result["ok"] is False and result["degraded"] is True and result["data"] is None
    assert result["error"]


def main() -> int:
    fx_httpd, fx_url = _start(fx_server)
    risk_httpd, risk_url = _start(risk_server)
    slow_httpd, slow_url = _start(fx_server, slow=1.5)
    config.MCP_FX_URL = fx_url
    config.MCP_RISK_URL = risk_url
    mcp_client.reset_cache()

    check("1/5 initialize 握手与工具暴露", scenario_initialize)
    check("2/5 tools/list 拉取与 60s 缓存", scenario_tools_list)
    check("3/5 tools/call 换算/风控数据", scenario_call)
    check("4/5 慢服务读超时降级", lambda: scenario_timeout(slow_url))
    check("5/5 服务关闭后离线降级", lambda: scenario_close([fx_httpd, risk_httpd, slow_httpd]))

    print("外部服务自测（fx/risk 进程内）")
    for name, ok, detail in _RESULTS:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else " -> " + detail))
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print("结果：%d/%d 通过" % (passed, len(_RESULTS)))
    return 0 if passed == len(_RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
