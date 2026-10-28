#!/usr/bin/env python3
"""ZeroStep MCP 外部服务端到端测试（零依赖，仅标准库；v0.2.0 引入，v0.3.0 回归沿用）。

用途：验证「主服务 -> mcp_client -> 外部汇率/风控服务」整条链路，包括
服务健康检查、状态聚合、两个只读工具调用、engine 真实回复话术，以及
服务停掉后的优雅降级。脚本自己拉起 backend/external/fx_server.py 与
risk_server.py 子进程，跑完无论成败都会清理；端口默认优先 8300/8301，
被占用时自动改用空闲端口（通过 MCP_FX_URL / MCP_RISK_URL 注入）。

用法：
    python scripts/mcp_e2e.py
    python scripts/mcp_e2e.py -v        # 失败时打印服务日志尾部

断言（7 项）：
  1. 两个外部服务 /health 均返回 ok，且 server 字段分别为 fx / risk；
  2. mcp_client.status() 显示 enabled=true、两服务 ok=true、工具清单正确；
  3. call_tool('fx','fx_convert',...) ok=true、source=external-fx、汇率/换算结果合理；
  4. call_tool('risk','risk_screen',...) ok=true、source=external-risk，命中演示名单；
  5. engine 真实对话「1000 元换成美元」回复含「外部实时汇率」「fx_convert」，
     executed 含 fx_convert，审计链 valid；另验证无金额纯汇率查询「查一下美元兑人民币汇率」
     走外部探针（回复「1 USD = x CNY」、不含折算金额段「≈」、不降级），审计链 valid；
  6. 停掉两个服务后 status() 两服务 ok=false / degraded=true，call_tool 返回
     ok=false / degraded=true 且不抛异常（降级不崩溃）；
  7. 服务停掉后 engine 仍可回答汇率，回复含「内置参考价」。

退出码：0 全部通过；1 有失败；2 环境/依赖不可用。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TMP_DIR = os.path.join(ROOT, ".eval_tmp", "mcp_e2e")
LOG_FX = os.path.join(TMP_DIR, "fx_server.log")
LOG_RISK = os.path.join(TMP_DIR, "risk_server.log")

CHECKS = []
PROCS = []


def _check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": detail})
    print("%s  %s  %-52s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


def _port_in_use(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _pick_port(preferred):
    """优先用契约端口（8300/8301），被占用时换空闲端口，避免和已起的服务/CI 冲突。"""
    if not _port_in_use(preferred):
        return preferred
    return _free_port()


def _start_server(script, port, log_path):
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [PY, os.path.join(ROOT, "backend", "external", script), "--port", str(port)],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    )
    PROCS.append((proc, log, script))
    return proc


def _wait_health(proc, port, server_name, timeout=20.0):
    deadline = time.time() + timeout
    last_error = ""
    url = "http://127.0.0.1:%d/health" % int(port)
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("%s 子进程提前退出，exit=%s" % (server_name, proc.returncode))
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if body.get("status") == "ok" and body.get("server") == server_name:
                return body
            last_error = "health 返回异常：%s" % (body,)
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败属正常，继续轮询
            last_error = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(0.25)
    raise RuntimeError("等待 %s 服务（%s）健康检查超时：%s" % (server_name, url, last_error))


def _stop_server(entry):
    proc, log, _script = entry
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    try:
        log.close()
    except Exception:  # noqa: BLE001
        pass


def _stop_all():
    while PROCS:
        _stop_server(PROCS.pop())


def _tail(path, lines=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return "".join(fp.readlines()[-lines:])
    except OSError:
        return "(无日志)"


def _setup_env(fx_port, risk_port):
    os.environ["DEMO_MODE"] = "1"
    os.environ["ZEROSTEP_DISABLE_LLM"] = "1"
    os.environ["MCP_ENABLED"] = "1"
    os.environ["MCP_FX_URL"] = "http://127.0.0.1:%d/rpc" % fx_port
    os.environ["MCP_RISK_URL"] = "http://127.0.0.1:%d/rpc" % risk_port
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    sys.path.insert(0, ROOT)


def _fresh_db():
    db_dir = os.path.join(TMP_DIR, "data")
    shutil.rmtree(db_dir, ignore_errors=True)
    os.makedirs(db_dir, exist_ok=True)
    from app import config, db
    config.DB_PATH = os.path.join(db_dir, "bank.db")
    conn = db.connect()
    db.init_schema(conn)
    db.seed_if_needed(conn)
    return conn


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep MCP 外部服务端到端测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印外部服务日志尾部")
    args = parser.parse_args(argv)

    os.makedirs(TMP_DIR, exist_ok=True)
    fx_port = _pick_port(8300)
    risk_port = _pick_port(8301)
    _setup_env(fx_port, risk_port)

    from app import audit, db, engine, mcp_client  # noqa: E402 - 需先注入环境变量

    conn = None
    try:
        try:
            with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8-sig") as fp:
                version_text = fp.read().strip()
        except OSError:
            version_text = "?"
        print("ZeroStep v%s MCP 外部服务 E2E（fx:%d risk:%d）" % (version_text, fx_port, risk_port))
        print("-" * 80)
        # ---------- 1. 启动两个外部服务 ----------
        fx_proc = _start_server("fx_server.py", fx_port, LOG_FX)
        risk_proc = _start_server("risk_server.py", risk_port, LOG_RISK)
        fx_health = _wait_health(fx_proc, fx_port, "fx")
        risk_health = _wait_health(risk_proc, risk_port, "risk")
        code_ok = True
        detail = ""
        try:
            pair = {"fx": (fx_health, "fx_convert"), "risk": (risk_health, "risk_screen")}
            for name, (body, tool) in pair.items():
                if body.get("status") != "ok":
                    code_ok = False
                    detail += "%s status!=ok; " % name
                if tool not in (body.get("tools") or []):
                    code_ok = False
                    detail += "%s tools 缺 %s; " % (name, tool)
        except Exception as exc:  # noqa: BLE001
            code_ok = False
            detail = str(exc)
        _check("mcp-1", "两个外部服务 /health 正常（fx:8300 risk:8301 或空闲端口）", code_ok, detail)

        # ---------- 2. 状态聚合 ----------
        try:
            st = mcp_client.status()
            servers = {s.get("name"): s for s in st.get("servers") or []}
            ok = (st.get("enabled") is True
                  and servers.get("fx", {}).get("ok") is True
                  and servers.get("risk", {}).get("ok") is True
                  and "fx_convert" in (servers.get("fx", {}).get("tools") or [])
                  and "risk_screen" in (servers.get("risk", {}).get("tools") or []))
            _check("mcp-2", "mcp_client.status() enabled 且两服务 ok、工具清单正确", ok,
                   "enabled=%s fx.ok=%s risk.ok=%s" % (st.get("enabled"),
                                                       servers.get("fx", {}).get("ok"),
                                                       servers.get("risk", {}).get("ok")))
        except Exception as exc:  # noqa: BLE001
            _check("mcp-2", "mcp_client.status() enabled 且两服务 ok、工具清单正确", False, repr(exc))

        # ---------- 3. fx_convert 真实调用 ----------
        try:
            res = mcp_client.call_tool("fx", "fx_convert",
                                       {"base": "CNY", "quote": "USD", "amount_cents": 100000})
            data = res.get("data") or {}
            rate = data.get("rate")
            converted = data.get("converted_cents")
            ok = (res.get("ok") is True and res.get("source") == "external-fx"
                  and isinstance(rate, (int, float)) and 0.05 < float(rate) < 1.0
                  and isinstance(converted, int) and converted > 0)
            _check("mcp-3", "call_tool fx_convert 成功（source=external-fx，汇率/换算合理）", ok,
                   "ok=%s source=%s rate=%s converted_cents=%s" % (res.get("ok"), res.get("source"), rate, converted))
        except Exception as exc:  # noqa: BLE001
            _check("mcp-3", "call_tool fx_convert 成功（source=external-fx，汇率/换算合理）", False, repr(exc))

        # ---------- 4. risk_screen 真实调用 ----------
        try:
            res = mcp_client.call_tool("risk", "risk_screen",
                                       {"payee_name": "张三", "payee_account_tail": "2233",
                                        "amount_cents": 600000})
            data = res.get("data") or {}
            ok = (res.get("ok") is True and res.get("source") == "external-risk"
                  and data.get("hit") is True and data.get("list_name") == "演示风控名单"
                  and int(data.get("score") or 0) >= 60)
            _check("mcp-4", "call_tool risk_screen 成功（source=external-risk，命中演示名单）", ok,
                   "ok=%s source=%s hit=%s score=%s list=%s" % (res.get("ok"), res.get("source"),
                                                                data.get("hit"), data.get("score"),
                                                                data.get("list_name")))
        except Exception as exc:  # noqa: BLE001
            _check("mcp-4", "call_tool risk_screen 成功（source=external-risk，命中演示名单）", False, repr(exc))

        # ---------- 5. engine 真实对话走外部汇率 ----------
        conn = _fresh_db()
        try:
            sid = engine.create_session(conn)["session_id"]
            result = engine.run_turn(conn, sid, "1000 元换成美元")
            reply = str(result.get("reply") or "")
            executed = [e.get("tool") for e in (result.get("executed") or []) if isinstance(e, dict)]
            audit_info = audit.verify(conn)
            # 纯汇率查询（用户没给金额）：v0.2.0 以 1 元探针换 rate，回复不含折算金额段。
            sid2 = engine.create_session(conn)["session_id"]
            result2 = engine.run_turn(conn, sid2, "查一下美元兑人民币汇率")
            reply2 = str(result2.get("reply") or "")
            audit_info2 = audit.verify(conn)
            probe_ok = (result2.get("status") == "ok" and "外部实时汇率" in reply2
                        and "1 USD =" in reply2 and "≈" not in reply2
                        and "内置参考价" not in reply2
                        and audit_info2.get("valid") is True)
            ok = (result.get("status") == "ok" and "外部实时汇率" in reply
                  and "fx_convert" in reply and "fx_convert" in executed
                  and audit_info.get("valid") is True and probe_ok)
            _check("mcp-5", "engine 对话走外部汇率（含无金额纯汇率 probe 路径）且审计链 valid", ok,
                   "status=%s executed=%s audit=%s | probe_status=%s reply2=%s"
                   % (result.get("status"), executed,
                      {"valid": audit_info.get("valid"), "checked": audit_info.get("checked")},
                      result2.get("status"), reply2[:60]))
        except Exception as exc:  # noqa: BLE001
            _check("mcp-5", "engine 对话走外部汇率（含无金额纯汇率 probe 路径）且审计链 valid", False, repr(exc))

        # ---------- 6. 停服务后优雅降级 ----------
        _stop_all()
        time.sleep(0.5)
        try:
            st = mcp_client.status()
            servers = {s.get("name"): s for s in st.get("servers") or []}
            all_down = all(servers.get(n, {}).get("ok") is False
                           and servers.get(n, {}).get("degraded") is True for n in ("fx", "risk"))
            call = mcp_client.call_tool("fx", "fx_convert",
                                        {"base": "CNY", "quote": "USD", "amount_cents": 100000})
            ok = all_down and call.get("ok") is False and call.get("degraded") is True
            _check("mcp-6", "停服务后 status/call_tool 优雅降级（不抛异常）", ok,
                   "fx.ok=%s risk.ok=%s call.ok=%s degraded=%s"
                   % (servers.get("fx", {}).get("ok"), servers.get("risk", {}).get("ok"),
                      call.get("ok"), call.get("degraded")))
        except Exception as exc:  # noqa: BLE001
            _check("mcp-6", "停服务后 status/call_tool 优雅降级（不抛异常）", False, repr(exc))

        # ---------- 7. 停服务后 engine 回退内置参考价 ----------
        try:
            sid = engine.create_session(conn)["session_id"]
            result = engine.run_turn(conn, sid, "1000 元换成美元")
            reply = str(result.get("reply") or "")
            ok = result.get("status") == "ok" and "内置参考价" in reply and "0.1403" in reply
            _check("mcp-7", "停服务后 engine 汇率查询回退内置参考价且不报错", ok,
                   "status=%s reply=%s" % (result.get("status"), reply[:80]))
        except Exception as exc:  # noqa: BLE001
            _check("mcp-7", "停服务后 engine 汇率查询回退内置参考价且不报错", False, repr(exc))

    finally:
        _stop_all()
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    passed = sum(1 for c in CHECKS if c["ok"])
    total = len(CHECKS)
    print("-" * 80)
    print("结果：%d/%d 项通过" % (passed, total))
    failed = [c for c in CHECKS if not c["ok"]]
    if failed and args.verbose:
        for c in failed:
            print("\n[FAIL %s] %s" % (c["id"], c["detail"]))
        print("\n--- fx_server.log 尾部 ---\n" + _tail(LOG_FX))
        print("--- risk_server.log 尾部 ---\n" + _tail(LOG_RISK))
    return 0 if total and passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
