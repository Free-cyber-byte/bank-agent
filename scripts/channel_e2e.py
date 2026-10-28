#!/usr/bin/env python3
"""ZeroStep 渠道（Channel）端到端测试（零依赖，仅标准库；v0.2.0 引入，v0.3.0 回归沿用）。

用途：验证「IM 渠道服务 -> 主服务 /api/chat -> 审批两步确认」整条链路：
健康检查、Token 鉴权、查询自动执行、转账需二次确认、确认后执行、
响应白名单（绝不泄露 approval_id / nonce / thread_id）、10 秒幂等缓存、
收发消息落盘 messages.jsonl、主服务 /api/channel/status 状态页数据，
以及 v0.2.0 新路由 /api/mcp/status、/api/skills、/api/skills/{id} 的只读数据契约。

隔离策略：把 backend/、frontend/、skills/ 复制到 .eval_tmp/channel_e2e/ 下再启动
两个服务，使用独立数据库与独立消息日志，不碰仓库里的 backend/data/bank.db，
避免与并行开发/演示用的主服务争用同一个 SQLite 审计链。

用法：
    python scripts/channel_e2e.py
    python scripts/channel_e2e.py -v     # 失败时打印两个服务的日志尾部

端口：主服务 8013、渠道 8413（被占用时自动换空闲端口）。
退出码：0 全部通过；1 有失败；2 环境不可用。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
WORK = os.path.join(ROOT, ".eval_tmp", "channel_e2e")
APP = os.path.join(WORK, "app")
LOGS = os.path.join(WORK, "logs")
LOG_MAIN = os.path.join(LOGS, "main_server.log")
LOG_CHANNEL = os.path.join(LOGS, "channel_server.log")

TOKEN = "zerostep-demo-token"
USER = "chan_qa_001"
CHECKS = []
PROCS = []


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
    return preferred if not _port_in_use(preferred) else _free_port()


def _check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": detail})
    print("%s  %s  %-56s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


def _prepare_app():
    """把运行所需的代码复制到隔离目录，得到全新的数据库与消息日志。"""
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(LOGS, exist_ok=True)

    def ignore(_dir, names):
        return [n for n in names if n in ("__pycache__", "data", "messages.jsonl", "sessions.json")]

    src_backend = os.path.join(ROOT, "backend")
    dst_backend = os.path.join(APP, "backend")
    shutil.copytree(src_backend, dst_backend, ignore=ignore)
    for name in ("frontend", "skills"):
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(APP, name), ignore=ignore)
    # 版本纪律：config.VERSION 读仓库根 VERSION 文件，隔离副本必须一并复制该文件，
    # 否则 _read_version() 会回退到旧默认值 "1.0.0"，/api/health 版本与实际不符。
    version_src = os.path.join(ROOT, "VERSION")
    if os.path.exists(version_src):
        shutil.copy2(version_src, os.path.join(APP, "VERSION"))
    os.makedirs(os.path.join(dst_backend, "data"), exist_ok=True)


def _start(script_rel, env_extra, log_path):
    env = dict(os.environ)
    env.update(env_extra)
    env["DEMO_MODE"] = "1"
    env["ZEROSTEP_DISABLE_LLM"] = "1"
    log = open(log_path, "wb")
    proc = subprocess.Popen([PY, script_rel], cwd=APP, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    PROCS.append((proc, log))
    return proc


def _wait_http(url, timeout=25.0, expect=None):
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if expect is None or expect(body):
                return body
            last_error = "返回不符合预期：%s" % (body,)
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败属正常
            last_error = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(0.25)
    raise RuntimeError("等待 %s 超时：%s" % (url, last_error))


def _post(port, path, payload, token=None, timeout=20.0):
    """POST JSON，返回 (HTTP 状态码, 响应 dict)；4xx/5xx 也返回响应体而不抛异常。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    if token is not None:
        req.add_header("X-Channel-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _get_json(port, path, timeout=5.0):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _keys_deep(obj):
    """递归收集所有 dict 的键，用于响应白名单断言。"""
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.append(str(key))
            found.extend(_keys_deep(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_keys_deep(item))
    return found


def _tail(path, lines=20):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return "".join(fp.readlines()[-lines:])
    except OSError:
        return "(无日志)"


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 渠道端到端测试")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印服务日志尾部")
    args = parser.parse_args(argv)

    main_port = _pick_port(8013)
    channel_port = _pick_port(8413)
    conn = None
    try:
        try:
            with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8-sig") as fp:
                version_text = fp.read().strip()
        except OSError:
            version_text = "?"
        print("ZeroStep v%s 渠道 E2E（主服务:%d 渠道:%d）" % (version_text, main_port, channel_port))
        print("-" * 80)
        _prepare_app()
        if not os.path.exists(os.path.join(APP, "backend", "channels", "server.py")):
            print("未找到 backend/channels/server.py，渠道服务尚未交付。")
            return 2

        _start(os.path.join("backend", "server.py"),
               {"PORT": str(main_port), "CHANNEL_PORT": str(channel_port)}, LOG_MAIN)
        _start(os.path.join("backend", "channels", "server.py"),
               {"CHANNEL_PORT": str(channel_port), "CHANNEL_TOKEN": TOKEN,
                "ZEROSTEP_MAIN_URL": "http://127.0.0.1:%d" % main_port}, LOG_CHANNEL)

        # ---------- c1 两个服务健康检查 ----------
        try:
            main_health = _wait_http("http://127.0.0.1:%d/api/health" % main_port,
                                     expect=lambda b: b.get("status") == "ok")
            chan_health = _wait_http("http://127.0.0.1:%d/health" % channel_port,
                                     expect=lambda b: b.get("status") == "ok")
            # 版本纪律：/api/health 的 version 必须来自仓库根 VERSION 文件。
            try:
                with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8-sig") as fp:
                    version_file = fp.read().strip()
            except OSError:
                version_file = ""
            main_version = str(main_health.get("version") or "")
            ver_ok = bool(version_file) and main_version == version_file
            _check("chn-1", "主服务 /api/health 与渠道 /health 均正常（version 与仓库根 VERSION 一致）",
                   ver_ok, "main=%s version=%s VERSION=%s channel=%s"
                   % (main_health.get("status"), main_version, version_file or "?",
                      chan_health.get("status")))
        except Exception as exc:  # noqa: BLE001
            _check("chn-1", "主服务 /api/health 与渠道 /health 均正常（version 与仓库根 VERSION 一致）",
                   False, repr(exc))

        # ---------- c2 Token 鉴权 ----------
        try:
            status_missing, body_missing = _post(channel_port, "/message",
                                                 {"user_id": USER, "text": "看看我这个月的账单"})
            status_wrong, body_wrong = _post(channel_port, "/message",
                                             {"user_id": USER, "text": "看看我这个月的账单"},
                                             token="wrong-token")
            ok = (status_missing == 401 and body_missing.get("error") == "UNAUTHORIZED"
                  and status_wrong == 401 and body_wrong.get("error") == "UNAUTHORIZED")
            _check("chn-2", "缺少/错误 X-Channel-Token 均返回 401 UNAUTHORIZED", ok,
                   "missing=%s wrong=%s" % (status_missing, status_wrong))
        except Exception as exc:  # noqa: BLE001
            _check("chn-2", "缺少/错误 X-Channel-Token 均返回 401 UNAUTHORIZED", False, repr(exc))

        # ---------- c3 查询类请求自动执行 ----------
        query = "看看我这个月的账单"
        try:
            st, body = _post(channel_port, "/message", {"user_id": USER, "text": query}, token=TOKEN)
            ok = (st == 200 and body.get("status") == "ok" and not body.get("needs_confirmation")
                  and "支出" in str(body.get("reply") or "") and body.get("cached") is False)
            _check("chn-3", "渠道查询「看看我这个月的账单」自动执行且回复含支出汇总", ok,
                   "http=%s status=%s reply=%s" % (st, body.get("status"), str(body.get("reply"))[:60]))
        except Exception as exc:  # noqa: BLE001
            _check("chn-3", "渠道查询「看看我这个月的账单」自动执行且回复含支出汇总", False, repr(exc))

        # ---------- c4 10 秒幂等缓存 ----------
        try:
            st2, body2 = _post(channel_port, "/message", {"user_id": USER, "text": query}, token=TOKEN)
            ok = st2 == 200 and body2.get("cached") is True
            _check("chn-4", "同一 user_id+text 10 秒内重复请求命中幂等缓存（cached=true）", ok,
                   "cached=%s status=%s" % (body2.get("cached"), body2.get("status")))
        except Exception as exc:  # noqa: BLE001
            _check("chn-4", "同一 user_id+text 10 秒内重复请求命中幂等缓存（cached=true）", False, repr(exc))

        # ---------- c5 转账两步确认 ----------
        try:
            st, first = _post(channel_port, "/message", {"user_id": USER, "text": "给妈妈转 800 元"},
                              token=TOKEN)
            pending_ok = (st == 200 and first.get("needs_confirmation") is True
                          and first.get("status") == "awaiting_approval"
                          and str(first.get("hint") or "").find("确认") >= 0
                          and str(first.get("preview") or "").strip() != "")
            secrets_ok = not any(k in ("approval_id", "nonce", "thread_id") for k in _keys_deep(first))
            st3, second = _post(channel_port, "/message", {"user_id": USER, "text": "确认"}, token=TOKEN)
            done_ok = (st3 == 200 and second.get("status") == "ok"
                       and "转账" in str(second.get("reply") or ""))
            secrets_ok2 = not any(k in ("approval_id", "nonce", "thread_id") for k in _keys_deep(second))
            _check("chn-5", "转账先回确认卡片，回「确认」后执行；回包不含 approval_id/nonce/thread_id",
                   pending_ok and done_ok and secrets_ok and secrets_ok2,
                   "pending=%s done=%s reply=%s" % (pending_ok, done_ok, str(second.get("reply"))[:50]))
        except Exception as exc:  # noqa: BLE001
            _check("chn-5", "转账先回确认卡片，回「确认」后执行；回包不含 approval_id/nonce/thread_id", False, repr(exc))

        # ---------- c6 主服务 /api/channel/status ----------
        try:
            cs = _get_json(main_port, "/api/channel/status")
            ok = (cs.get("enabled") is True and int(cs.get("port") or 0) == channel_port
                  and isinstance(cs.get("webhook_url"), str) and cs.get("webhook_url")
                  and isinstance(cs.get("recent"), list) and len(cs.get("recent")) <= 5)
            _check("chn-6", "/api/channel/status 有渠道状态与最近消息（recent <= 5 条）", ok,
                   "enabled=%s port=%s recent=%d" % (cs.get("enabled"), cs.get("port"),
                                                     len(cs.get("recent") or [])))
        except Exception as exc:  # noqa: BLE001
            _check("chn-6", "/api/channel/status 有渠道状态与最近消息（recent <= 5 条）", False, repr(exc))

        # ---------- c7 messages.jsonl 收发落盘 ----------
        try:
            log_path = os.path.join(APP, "backend", "channels", "messages.jsonl")
            rows = []
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if record.get("user_id") == USER:
                            rows.append(record)
            directions = {r.get("direction") for r in rows}
            ok = len(rows) >= 8 and directions == {"in", "out"}
            _check("chn-7", "messages.jsonl 按收发各落一行（in/out 均有）", ok,
                   "行数=%d directions=%s" % (len(rows), sorted(d for d in directions if d)))
        except Exception as exc:  # noqa: BLE001
            _check("chn-7", "messages.jsonl 按收发各落一行（in/out 均有）", False, repr(exc))

        # ---------- c8 主服务 /api/mcp/status ----------
        try:
            mcp = _get_json(main_port, "/api/mcp/status")
            servers = mcp.get("servers") or []
            names = {str(s.get("name")) for s in servers if isinstance(s, dict)}
            ok = (mcp.get("ok") is True and isinstance(mcp.get("enabled"), bool)
                  and {"fx", "risk"} <= names
                  and all(isinstance(s.get("ok"), bool) for s in servers if isinstance(s, dict)))
            _check("chn-8", "主服务 /api/mcp/status 返回 enabled/servers(fx,risk)/ok", ok,
                   "enabled=%s servers=%s" % (mcp.get("enabled"), sorted(names)))
        except Exception as exc:  # noqa: BLE001
            _check("chn-8", "主服务 /api/mcp/status 返回 enabled/servers(fx,risk)/ok", False, repr(exc))

        # ---------- c9 主服务 /api/skills 与 /api/skills/{id} ----------
        try:
            sk = _get_json(main_port, "/api/skills")
            cards = sk.get("skills") or []
            ids = {str(c.get("id")) for c in cards if isinstance(c, dict)}
            one = _get_json(main_port, "/api/skills/transfer")
            unknown_code = None
            try:
                _get_json(main_port, "/api/skills/not_exist_card")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    try:
                        body = json.loads(exc.read().decode("utf-8", "replace"))
                        err = body.get("error")
                        unknown_code = err.get("code") if isinstance(err, dict) else body.get("code")
                    except Exception:  # noqa: BLE001
                        unknown_code = "?"
            ok = (int(sk.get("count") or 0) == 6 and len(cards) == 6 and len(ids) == 6
                  and (one.get("skill") or {}).get("id") == "transfer"
                  and unknown_code == "SKILL_NOT_FOUND")
            _check("chn-9", "主服务 /api/skills 六卡、transfer 详情、未知卡 404", ok,
                   "count=%s ids=%d unknown=%s" % (sk.get("count"), len(ids), unknown_code))
        except Exception as exc:  # noqa: BLE001
            _check("chn-9", "主服务 /api/skills 六卡、transfer 详情、未知卡 404", False, repr(exc))

    except Exception as exc:  # noqa: BLE001
        print("运行异常：%s: %s" % (type(exc).__name__, exc))
        if args.verbose:
            import traceback
            traceback.print_exc()
    finally:
        _stop_all()
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _finish(args)


def _finish(args):
    passed = sum(1 for c in CHECKS if c["ok"])
    total = len(CHECKS)
    print("-" * 80)
    print("结果：%d/%d 项通过" % (passed, total))
    if total == 0:
        return 2
    if passed != total and args.verbose:
        for c in CHECKS:
            if not c["ok"]:
                print("\n[FAIL %s] %s" % (c["id"], c["detail"]))
        print("\n--- main_server.log 尾部 ---\n" + _tail(LOG_MAIN))
        print("--- channel_server.log 尾部 ---\n" + _tail(LOG_CHANNEL))
    return 0 if passed == total else 1


def _stop_all():
    while PROCS:
        proc, log = PROCS.pop()
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


if __name__ == "__main__":
    sys.exit(main())
