#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZeroStep LLM 接入加固 e2e：用 Lead 的 OpenAI 兼容桩做确定性验证（无需真实 Key）。

覆盖 9 个场景（task-27 的 8 个 + v0.4.0 场景 9）：
  1) ok 基础链路：intent.source=llm、/api/llm/status 的 stats/last_usage/last_cost_yuan、last_error 为空
  2) 请求体契约：thinking 关闭、max_tokens=800、system 前缀稳定、skill_hint 在 messages[1] 的 user JSON；
     SYSTEM_PROMPT 含 v0.4.0 六类新意图（长度动态读取，不硬编码）
  3) 401：不重试（桩 calls==1）、回退规则、last_error_code==401、hint 含 Key
  4) 429_once：重试 1 次后成功（桩 calls==2、source=llm、stats.retries>=1）
  5) empty_once：空 content 重试后成功（calls==2、source=llm）
  6) 500_always：连续 3 轮失败后熔断（degraded/breaker.open、第 4 轮不再打桩、冷却结束后恢复）
  7) slow：超时回退，单轮总耗时 < 6s、stats.timeouts>=1
  8) POST /api/llm/test：无 Key -> ok=false + hint 含 .env；假 Key+桩 -> ok=true、latency_ms>0、usage 非空
  9) v0.4.0 新能力（模型模式）：六类新意图均 source=llm 且 intent 正确、计划含对应工具；
     「预约转账」类句子不得被模型改判成立即转账、异常扫描不得被改判成查流水（安全护栏，直接调产品
     _normalize 做差分验证，桩无法构造这类改判）

做法：本脚本自己拉起 scripts/llm_stub_server.py 与 backend/server.py（独立端口 8031/8321），
用 env 注入假 Key 与桩地址；每个场景用独立服务进程隔离状态，结束后统一 terminate/kill。
零第三方依赖，只用标准库；不读取 .env 内容，所有 DEEPSEEK_* 均由本脚本显式覆盖。

用法：
    python scripts/llm_e2e.py            # 全量 9 场景
    python scripts/llm_e2e.py --json     # 额外打印 JSON 汇总
    python scripts/llm_e2e.py --only 9   # 只跑某个场景（1-9，可多个逗号分隔）

退出码：0=全部场景通过；1=有失败；2=环境/启动失败。
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # bank-agent
BACKEND = ROOT / "backend"
SCRIPTS = ROOT / "scripts"
STUB_SCRIPT = SCRIPTS / "llm_stub_server.py"
TMP_DIR = ROOT / ".eval_tmp" / "llm_e2e"

PY = sys.executable or "python"
HOST = "127.0.0.1"
APP_PORT = 8031
STUB_PORT = 8321
STUB_BASE = "http://%s:%d" % (HOST, STUB_PORT)
FAKE_KEY = "sk-llm-e2e-stub"

try:  # Windows 控制台默认 GBK，统一成 UTF-8，避免中文/¥ 打印崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老解释器没有 reconfigure 时忽略
    pass


def _product_system_prompt() -> str:
    """只读导入产品模块，取 SYSTEM_PROMPT 用于逐字节比对（不写任何产品文件）。

    config 导入时会读 .env 并可能向 os.environ 补变量，所以导入后立刻还原环境，
    避免后续子进程继承 .env 里的真实 Key 等配置。
    """
    saved = dict(os.environ)
    try:
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        from app import llm as product_llm  # noqa: WPS433 - 延迟只读导入
        return str(product_llm.SYSTEM_PROMPT or "")
    except Exception as exc:  # noqa: BLE001 - 产品模块异常时不阻断环境准备，场景 2 会判 FAIL
        print("[准备] 读取产品 SYSTEM_PROMPT 失败：%s" % exc)
        return ""
    finally:
        os.environ.clear()
        os.environ.update(saved)


SYSTEM_PROMPT = _product_system_prompt()


def _repo_version() -> str:
    """读仓库根 VERSION（banner 用，避免硬编码版本号）。"""
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _product_llm_nlu():
    """只读导入产品 llm/nlu（场景 9 的安全护栏差分验证用），导入后还原环境变量。"""
    saved = dict(os.environ)
    try:
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        from app import llm as product_llm  # noqa: WPS433 - 延迟只读导入
        from app import nlu as product_nlu   # noqa: WPS433 - 延迟只读导入
        return product_llm, product_nlu
    except Exception:  # noqa: BLE001 - 导入失败时由场景断言记 FAIL
        return None, None
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---------------------------------------------------------------- 进程与端口
def _port_free(port: int) -> bool:
    """端口是否可用。

    Windows 上以「没有 LISTENING 进程」为准：主动关闭的服务端连接会留下
    TIME_WAIT（本地端口仍是 8321），此时裸 bind 会失败，但真正的 HTTP 服务
    （HTTPServer 默认 allow_reuse_address）可以正常监听；若改用带 SO_REUSEADDR
    的探测 socket，又会在端口真被监听时误判为空闲（Windows 允许重复绑定）。
    因此 NT 下直接查监听进程，其它平台仍用 bind 探测。
    """
    if os.name == "nt":
        return not _port_pids(port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.3)
        sock.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _port_pids(port: int):
    """用 netstat -ano 找监听该端口的 PID（仅 Windows；其它平台返回空）。"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=10, encoding="utf-8", errors="ignore").stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in line.upper():
            continue
        local = parts[1]
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0 and pid != os.getpid():
            pids.append(pid)
    return pids


def ensure_port(port: int, label: str) -> None:
    """端口被占则清理残留进程；清理失败再等 5 秒，仍失败则报环境错误。"""
    if _port_free(port):
        return
    pids = _port_pids(port)
    print("[准备] %s 端口 %d 被占用（PID: %s），先清理残留进程" % (label, port, pids or "未知"))
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _port_free(port):
            return
        time.sleep(0.2)
    raise RuntimeError("%s 端口 %d 仍被占用，请先手动结束监听该端口的进程" % (label, port))


def _wait_http(url: str, timeout: float = 20.0, name: str = "") -> dict:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.15)
    raise RuntimeError("%s 启动超时：%s（最后错误：%s）" % (name or url, url, last_err))


def _http(method: str, url: str, body=None, timeout: float = 30.0):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400]}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("HTTP %s %s 失败：%s" % (method, url, exc))


def _stop_proc(proc, label: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=6)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass


def _log_tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------- 测试桩
class Stub:
    def __init__(self) -> None:
        self.proc = None
        self.log = TMP_DIR / "stub.log"

    def start(self) -> None:
        ensure_port(STUB_PORT, "LLM 桩")
        env = _clean_env()
        env.update({
            "LLM_STUB_PORT": str(STUB_PORT),
            "LLM_STUB_HOST": HOST,
            "LLM_STUB_DELAY": "3",          # slow 场景的延迟；其它模式不受影响
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        self.log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.log, "wb")
        self.proc = subprocess.Popen([PY, str(STUB_SCRIPT)], cwd=str(ROOT), env=env,
                                     stdout=fh, stderr=subprocess.STDOUT)
        try:
            health = _wait_http(STUB_BASE + "/health", 20, "LLM 桩")
        except RuntimeError:
            tail = _log_tail(self.log)
            raise RuntimeError("LLM 桩启动失败\n" + tail)
        if not health.get("ok"):
            raise RuntimeError("LLM 桩 /health 异常：%s" % (health,))
        self.reset()
        self.mode("ok")
        print("[准备] LLM 桩已启动 %s（pid=%s）" % (STUB_BASE, self.proc.pid))

    def stop(self) -> None:
        _stop_proc(self.proc, "stub")

    def mode(self, mode: str) -> None:
        status, body = _http("POST", STUB_BASE + "/_mode", {"mode": mode}, timeout=5)
        if status != 200 or not body.get("ok"):
            raise RuntimeError("切换桩模式 %s 失败：%s %s" % (mode, status, body))

    def reset(self) -> None:
        _http("POST", STUB_BASE + "/_reset", {}, timeout=5)

    def stats(self) -> dict:
        _status, body = _http("GET", STUB_BASE + "/_stats", timeout=5)
        return body


# ---------------------------------------------------------------- 主服务
def _clean_env() -> dict:
    """复制当前环境，剔除会影响本测试的既有变量（.env 只在缺省时才生效）。"""
    drop_prefix = ("DEEPSEEK_", "LLM_STUB_")
    drop_exact = {"ZEROSTEP_DISABLE_LLM", "PORT", "HOST", "DEMO_MODE", "PLUGINS_FILE",
                  "MCP_ENABLED", "CHANNEL_ENABLED", "PYTHONIOENCODING", "PYTHONUTF8"}
    env = {}
    for key, value in os.environ.items():
        if key.startswith(drop_prefix) or key in drop_exact:
            continue
        env[key] = value
    return env


def _app_env(key: str = FAKE_KEY, extra: dict = None) -> dict:
    env = _clean_env()
    env.update({
        "PORT": str(APP_PORT),
        "HOST": HOST,
        "DEMO_MODE": "1",
        "DEEPSEEK_API_KEY": key,                       # 空串=显式未配置，阻止 .env 补齐
        "DEEPSEEK_BASE_URL": STUB_BASE,
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_TIMEOUT": "2",
        "DEEPSEEK_BUDGET": "3",
        "DEEPSEEK_MAX_TOKENS": "800",
        "DEEPSEEK_THINKING": "disabled",
        "DEEPSEEK_RETRY": "1",
        "DEEPSEEK_RETRY_BACKOFF_MS": "600",
        "DEEPSEEK_BREAKER_THRESHOLD": "3",
        "DEEPSEEK_BREAKER_COOLDOWN": "2",
        "ZEROSTEP_DISABLE_LLM": "0",
        "DEEPSEEK_MODEL_ALIAS": "deepseek-flash",
        "PLUGINS_FILE": str(TMP_DIR / "plugins.json"),
        "MCP_ENABLED": "0",
        "CHANNEL_ENABLED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


@contextlib.contextmanager
def app_server(key: str = FAKE_KEY, extra: dict = None):
    """启动一个隔离的主服务进程，退出时清理；失败时打印日志尾部并抛错。"""
    ensure_port(APP_PORT, "主服务")
    env = _app_env(key, extra)
    log = TMP_DIR / "app.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "wb")
    proc = subprocess.Popen([PY, str(BACKEND / "server.py")], cwd=str(ROOT), env=env,
                            stdout=fh, stderr=subprocess.STDOUT)
    base = "http://%s:%d" % (HOST, APP_PORT)
    try:
        try:
            _wait_http(base + "/api/health", 25, "主服务")
        except RuntimeError:
            raise RuntimeError("主服务启动失败\n" + _log_tail(log))
        try:  # 归一化演示数据与插件开关，避免历史持久化状态影响断言
            _http("POST", base + "/api/admin/reset", {}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        yield base
    finally:
        _stop_proc(proc, "app")


def chat(base: str, session_id: str, text: str, timeout: float = 30.0) -> dict:
    _status, body = _http("POST", base + "/api/chat",
                          {"session_id": session_id, "text": text}, timeout=timeout)
    return body


def session(base: str) -> str:
    _status, body = _http("POST", base + "/api/session", {}, timeout=10)
    return body.get("session_id") or ""


def llm_status(base: str) -> dict:
    _status, body = _http("GET", base + "/api/llm/status", timeout=10)
    return body


class Checks:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.items = []

    def add(self, name: str, ok, detail="") -> None:
        self.items.append({"scenario": self.scenario, "name": name, "ok": bool(ok),
                           "detail": str(detail)[:240]})

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(item["ok"] for item in self.items)


def usage_of(stats: dict) -> dict:
    usage = (stats or {}).get("last_usage") or {}
    return usage if isinstance(usage, dict) else {}


def breaker_of(status: dict) -> dict:
    breaker = (status or {}).get("breaker") or {}
    return breaker if isinstance(breaker, dict) else {}


# ---------------------------------------------------------------- 8 个场景
def scenario_1(stub: Stub) -> Checks:
    c = Checks("1) ok 基础链路")
    stub.mode("ok")
    stub.reset()
    with app_server() as base:
        sid = session(base)
        result = chat(base, sid, "帮我看看这个月账单花了多少")
        intent = result.get("intent") or {}
        status = llm_status(base)
        stats = status.get("stats") or {}
        usage = usage_of(stats)
        cost = stats.get("last_cost_yuan")
        last_error = status.get("last_error")
        c.add("intent.source == llm", intent.get("source") == "llm", intent.get("source"))
        c.add("stats.calls >= 1", int(stats.get("calls") or 0) >= 1, stats.get("calls"))
        c.add("last_usage 各字段 > 0",
              all(int(usage.get(k) or 0) > 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")),
              json.dumps(usage, ensure_ascii=False))
        c.add("last_cost_yuan > 0", isinstance(cost, (int, float)) and cost > 0, cost)
        c.add("last_error 为空", last_error in (None, ""), last_error)
        c.add("intent.latency_ms > 0", int(intent.get("latency_ms") or 0) > 0, intent.get("latency_ms"))
    return c


def scenario_2(stub: Stub) -> Checks:
    c = Checks("2) 请求体契约")
    stub.mode("ok")
    stub.reset()
    with app_server() as base:
        sid = session(base)
        chat(base, sid, "给妈妈转 800 元")                 # 命中 transfer 技能卡
        chat(base, sid, "帮我看看这个月账单花了多少")       # 命中 bills 技能卡
        stats = stub.stats()
        payloads = stats.get("payloads") or []
        c.add("桩记录 >=2 次调用", len(payloads) >= 2, len(payloads))
        if len(payloads) < 2:
            return c
        p0, p1 = payloads[0], payloads[1]
        c.add("thinking 关闭",
              p0.get("thinking") == {"type": "disabled"} and p1.get("thinking") == {"type": "disabled"},
              "%s / %s" % (p0.get("thinking"), p1.get("thinking")))
        c.add("max_tokens == 800", p0.get("max_tokens") == 800 and p1.get("max_tokens") == 800,
              "%s / %s" % (p0.get("max_tokens"), p1.get("max_tokens")))
        m0 = (p0.get("messages") or [{}])[0].get("content") or ""
        m1 = (p1.get("messages") or [{}])[0].get("content") or ""
        c.add("messages[0] system 前缀完全一致", bool(m0) and m0 == m1, "len=%d/%d" % (len(m0), len(m1)))
        if SYSTEM_PROMPT:
            c.add("messages[0] 与 llm.SYSTEM_PROMPT 逐字节相等",
                  m0 == SYSTEM_PROMPT and m1 == SYSTEM_PROMPT,
                  "len=%d/%d expected=%d" % (len(m0), len(m1), len(SYSTEM_PROMPT)))
        else:
            c.add("messages[0] 与 llm.SYSTEM_PROMPT 逐字节相等", False, "未取到产品 SYSTEM_PROMPT")
        if SYSTEM_PROMPT:
            new_intents = ("wealth_redeem", "card_limit", "card_spend",
                           "anomaly_scan", "annual_report", "scheduled_transfer")
            missing = [name for name in new_intents if name not in SYSTEM_PROMPT]
            c.add("SYSTEM_PROMPT 含 v0.4.0 六类新意图（长度动态读取，不硬编码）",
                  not missing, "missing=%s len=%d" % (missing, len(SYSTEM_PROMPT)))
        hints = []
        for idx, payload in enumerate((p0, p1)):
            messages = payload.get("messages") or []
            user_raw = messages[1].get("content") if len(messages) > 1 else ""
            try:
                user = json.loads(user_raw) if isinstance(user_raw, str) else {}
            except json.JSONDecodeError:
                user = {}
            hints.append(str(user.get("skill_hint") or ""))
            c.add("messages[1] user JSON 含 skill_hint（第 %d 次）" % (idx + 1),
                  isinstance(user, dict) and "skill_hint" in user, user_raw[:80])
        c.add("两次调用的技能提示不同且非空",
              bool(hints[0]) and bool(hints[1]) and hints[0] != hints[1],
              [h[:40] for h in hints])
    return c


def scenario_3(stub: Stub) -> Checks:
    c = Checks("3) 401 不重试")
    stub.mode("401")
    stub.reset()
    with app_server() as base:
        sid = session(base)
        result = chat(base, sid, "帮我看看这个月账单花了多少")
        intent = result.get("intent") or {}
        status = llm_status(base)
        calls = int(stub.stats().get("calls") or 0)
        c.add("桩 calls == 1（401 不重试）", calls == 1, calls)
        c.add("source 为 fallback/rule", intent.get("source") in ("fallback", "rule"), intent.get("source"))
        c.add("last_error_code == 401", status.get("last_error_code") == 401, status.get("last_error_code"))
        hint = str(status.get("last_error_hint") or "")
        c.add("last_error_hint 含 Key", "key" in hint.lower(), hint)
        stats = status.get("stats") or {}
        breaker = breaker_of(status)
        c.add("401 后 stats.fail >= 1", int(stats.get("fail") or 0) >= 1, stats.get("fail"))
        c.add("401 单次失败：breaker.failures == 1 且 degraded == false（不提前降级）",
              int(breaker.get("failures") or 0) == 1 and status.get("degraded") is False,
              json.dumps({"failures": breaker.get("failures"), "degraded": status.get("degraded")},
                         ensure_ascii=False))
    return c


def scenario_4(stub: Stub) -> Checks:
    c = Checks("4) 429_once 重试后成功")
    stub.mode("429_once")
    stub.reset()
    with app_server() as base:
        sid = session(base)
        result = chat(base, sid, "帮我看看这个月账单花了多少")
        intent = result.get("intent") or {}
        status = llm_status(base)
        stats = status.get("stats") or {}
        calls = int(stub.stats().get("calls") or 0)
        c.add("桩 calls == 2（重试 1 次）", calls == 2, calls)
        c.add("source == llm", intent.get("source") == "llm", intent.get("source"))
        c.add("stats.retries >= 1", int(stats.get("retries") or 0) >= 1, stats.get("retries"))
    return c


def scenario_5(stub: Stub) -> Checks:
    c = Checks("5) empty_once 空内容重试后成功")
    stub.mode("empty_once")
    stub.reset()
    with app_server() as base:
        sid = session(base)
        result = chat(base, sid, "帮我看看这个月账单花了多少")
        intent = result.get("intent") or {}
        calls = int(stub.stats().get("calls") or 0)
        c.add("桩 calls == 2（空 content 重试）", calls == 2, calls)
        c.add("source == llm", intent.get("source") == "llm", intent.get("source"))
    return c


def scenario_6(stub: Stub) -> Checks:
    c = Checks("6) 500_always 熔断与冷却恢复")
    stub.mode("500_always")
    stub.reset()
    # 显式 RETRY=0：熔断计数按「轮」而不是「HTTP 尝试」累计，3 轮后 calls 恰好 3；
    # 默认 RETRY=1 时每轮最多 2 次 HTTP，calls 会是 6，容易把断言语义带偏。
    with app_server(extra={"DEEPSEEK_BREAKER_COOLDOWN": "2", "DEEPSEEK_BREAKER_THRESHOLD": "3",
                           "DEEPSEEK_BUDGET": "4", "DEEPSEEK_RETRY": "0"}) as base:
        sid = session(base)
        for _ in range(3):
            chat(base, sid, "帮我看看这个月账单花了多少")
        status = llm_status(base)
        stats = status.get("stats") or {}
        breaker = breaker_of(status)
        calls_before = int(stub.stats().get("calls") or 0)
        c.add("3 轮后桩 calls == 3（每轮 1 次 HTTP）", calls_before == 3, calls_before)
        c.add("3 轮后 breaker.open == true", breaker.get("open") is True, json.dumps(breaker, ensure_ascii=False))
        c.add("3 轮后 degraded == true", status.get("degraded") is True, status.get("degraded"))
        c.add("stats.fail >= 3", int(stats.get("fail") or 0) >= 3, stats.get("fail"))
        started4 = time.time()
        result4 = chat(base, sid, "帮我看看这个月账单花了多少")
        elapsed4 = time.time() - started4
        intent4 = result4.get("intent") or {}
        reason4 = str(intent4.get("fallback_reason") or "")
        c.add("第 4 轮 source == rule", intent4.get("source") == "rule", intent4.get("source"))
        c.add("第 4 轮 fallback_reason 含熔断", "熔断" in reason4, reason4[:120])
        c.add("第 4 轮降级耗时 < 500ms（熔断后不发网络请求）", elapsed4 < 0.5, "%.0fms" % (elapsed4 * 1000))
        calls_after = int(stub.stats().get("calls") or 0)
        c.add("第 4 轮不再打桩（calls 不增）", calls_after == calls_before, "%d -> %d" % (calls_before, calls_after))
        status2 = llm_status(base)
        stats2 = status2.get("stats") or {}
        c.add("stats.circuit_skipped >= 1", int(stats2.get("circuit_skipped") or 0) >= 1,
              stats2.get("circuit_skipped"))
        c.add("第 4 轮后 degraded == true", status2.get("degraded") is True, status2.get("degraded"))
        # 冷却 2s：切回 ok 并等 2.3s，应恢复真实调用
        stub.mode("ok")
        time.sleep(2.3)
        result5 = chat(base, sid, "帮我看看这个月账单花了多少")
        intent5 = result5.get("intent") or {}
        status3 = llm_status(base)
        c.add("冷却后恢复 source == llm", intent5.get("source") == "llm", intent5.get("source"))
        c.add("冷却后 breaker 关闭且不降级",
              breaker_of(status3).get("open") is False and status3.get("degraded") is False,
              json.dumps({"breaker": breaker_of(status3), "degraded": status3.get("degraded")},
                         ensure_ascii=False))
    return c


def scenario_7(stub: Stub) -> Checks:
    c = Checks("7) slow 超时回退")
    stub.mode("slow")                     # 桩每次延迟 3s（启动时 LLM_STUB_DELAY=3）
    stub.reset()
    with app_server(extra={"DEEPSEEK_TIMEOUT": "1", "DEEPSEEK_BUDGET": "4"}) as base:
        sid = session(base)
        started = time.time()
        result = chat(base, sid, "帮我看看这个月账单花了多少", timeout=25)
        elapsed = time.time() - started
        intent = result.get("intent") or {}
        stats = llm_status(base).get("stats") or {}
        c.add("单轮总耗时 < 6s", elapsed < 6.0, "%.2fs" % elapsed)
        c.add("超时后回退（fallback/rule）", intent.get("source") in ("fallback", "rule"), intent.get("source"))
        c.add("stats.timeouts >= 1", int(stats.get("timeouts") or 0) >= 1, stats.get("timeouts"))
    return c


def scenario_8(stub: Stub) -> Checks:
    c = Checks("8) POST /api/llm/test")
    stub.mode("ok")
    stub.reset()
    with app_server(key="") as base:              # 显式空 Key：模拟未配置
        status = llm_status(base)
        _code, body = _http("POST", base + "/api/llm/test", {}, timeout=15)
        hint = str(body.get("hint") or "")
        c.add("无 Key：status.enabled == false", status.get("enabled") is False, status.get("enabled"))
        c.add("无 Key：ok == false", body.get("ok") is False, body.get("ok"))
        c.add("无 Key：hint 含 .env", ".env" in hint, hint)
        # Lead 口径：无 Key 是本地短路，不发请求、不计入 stats（calls 仍 0）
        stub_calls = int(stub.stats().get("calls") or 0)
        stats_nokey = llm_status(base).get("stats") or {}
        c.add("无 Key：本地短路不请求桩、不计入 stats（calls 仍 0）",
              stub_calls == 0 and int(stats_nokey.get("calls") or 0) == 0,
              "桩 calls=%d stats.calls=%s" % (stub_calls, stats_nokey.get("calls")))
    stub.reset()
    with app_server() as base:                    # 假 Key + 桩
        _code, audit_before = _http("GET", base + "/api/audit", timeout=10)
        entries_before = len(audit_before.get("entries") or [])
        stats_before = llm_status(base).get("stats") or {}
        _code, body = _http("POST", base + "/api/llm/test", {}, timeout=20)
        usage = body.get("usage") or {}
        calls = int(stub.stats().get("calls") or 0)
        status_after = llm_status(base)
        stats_after = status_after.get("stats") or {}
        _code, audit_after = _http("GET", base + "/api/audit", timeout=10)
        entries_after = len(audit_after.get("entries") or [])
        c.add("有 Key：ok == true", body.get("ok") is True, json.dumps(body, ensure_ascii=False)[:160])
        c.add("有 Key：latency_ms > 0", int(body.get("latency_ms") or 0) > 0, body.get("latency_ms"))
        c.add("有 Key：usage 非空",
              isinstance(usage, dict) and int(usage.get("total_tokens") or 0) > 0,
              json.dumps(usage, ensure_ascii=False))
        c.add("有 Key：cost_yuan > 0", isinstance(body.get("cost_yuan"), (int, float)) and body.get("cost_yuan") > 0,
              body.get("cost_yuan"))
        # 成本公式交叉验证：hit*0.02 + miss*1.0 + completion*4.0（元 / 1e6 tokens）
        cost = body.get("cost_yuan")
        expect_cost = 1024 * 0.02 / 1e6 + 156 * 1.0 / 1e6 + 96 * 4.0 / 1e6
        c.add("有 Key：cost_yuan 与公式一致（0.00056048）",
              isinstance(cost, (int, float)) and abs(float(cost) - expect_cost) < 1e-9, cost)
        c.add("测试连接真实打桩 1 次", calls >= 1, calls)
        # Lead 口径：/api/llm/test 内部走 call_model，会计入 stats 与熔断统计；唯一不写审计链
        c.add("有 Key：/api/llm/test 计入 stats（calls 0→≥1、last_cost_yuan>0、degraded=false）",
              int(stats_after.get("calls") or 0) == int(stats_before.get("calls") or 0) + 1
              and float(stats_after.get("last_cost_yuan") or 0) > 0
              and status_after.get("degraded") is False,
              json.dumps({"before": stats_before.get("calls"), "after": stats_after.get("calls"),
                          "last_cost_yuan": stats_after.get("last_cost_yuan"),
                          "degraded": status_after.get("degraded")}, ensure_ascii=False))
        c.add("POST /api/llm/test 不写审计链", entries_after == entries_before,
              "%d -> %d" % (entries_before, entries_after))
    return c


def _result_tools(result: dict) -> list:
    """从 /api/chat 结果里取出计划工具 + 已执行工具（去重保序）。"""
    tools = []
    plan = result.get("plan") or {}
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and step.get("tool"):
            tools.append(str(step["tool"]))
    for item in result.get("executed") or []:
        if isinstance(item, dict) and item.get("tool"):
            tool = str(item["tool"])
            if tool not in tools:
                tools.append(tool)
    return tools


def scenario_9(stub: Stub) -> Checks:
    c = Checks("9) v0.4.0 新能力（模型模式）+ 安全护栏")
    # ---- 9.1 安全护栏差分验证：规则判预约转账 / 异常扫描时，模型的改判必须被强制回原意图
    product_llm, product_nlu = _product_llm_nlu()
    if product_llm is None or product_nlu is None:
        c.add("产品 llm/nlu 可用于安全护栏差分验证", False, "延迟导入失败")
    else:
        text_sched = "明天上午 9 点给妈妈转 100 元"
        fallback_sched = product_nlu.parse(text_sched, {})
        forced_sched = product_llm._normalize(
            {"intent": "transfer", "amount_yuan": 100, "payee_name": "妈妈"},
            text_sched, {}, fallback_sched)
        c.add("护栏：预约转账句被模型改判立即转账 → 强制回 scheduled_transfer",
              forced_sched.get("name") == "scheduled_transfer",
              "rule=%s model=transfer -> %s" % (fallback_sched.get("name"), forced_sched.get("name")))
        text_scan = "帮我查一下最近有没有异常交易"
        fallback_scan = product_nlu.parse(text_scan, {})
        forced_scan = product_llm._normalize({"intent": "query_transactions"},
                                             text_scan, {}, fallback_scan)
        c.add("护栏：异常扫描句被模型改判查流水 → 强制回 anomaly_scan",
              forced_scan.get("name") == "anomaly_scan",
              "rule=%s model=query_transactions -> %s" % (fallback_scan.get("name"),
                                                          forced_scan.get("name")))
    # ---- 9.2 模型模式端到端：v0.4.0 六类新能力 intent/source/工具正确
    stub.mode("ok")
    stub.reset()
    cases = (
        ("把稳利宝赎回来 1000 元", "wealth_redeem", ("redeem_wealth",)),
        ("把信用卡日累计限额调到 5000 元", "card_limit", ("set_card_limit",)),
        ("用储蓄卡在京东消费 25000 元", "card_spend", ("card_spend",)),
        ("帮我查一下最近有没有异常交易", "anomaly_scan", ("scan_anomalies",)),
        ("看看今年年度账单", "annual_report", ("annual_report",)),
        ("明天上午 9 点给妈妈转 100 元", "scheduled_transfer", ("create_scheduled_transfer",)),
        ("帮我查一下预约转账", "scheduled_transfer", ("list_scheduled_transfers",)),
    )
    with app_server() as base:
        for text, expect_intent, expect_tools in cases:
            sid = session(base)
            result = chat(base, sid, text)
            intent = result.get("intent") or {}
            tools = _result_tools(result)
            c.add("%s：intent=%s" % (text, expect_intent),
                  intent.get("name") == expect_intent, "intent=%s" % intent.get("name"))
            c.add("%s：source=llm" % text, intent.get("source") == "llm", intent.get("source"))
            missing = [tool for tool in expect_tools if tool not in tools]
            c.add("%s：计划/执行含 %s" % (text, "/".join(expect_tools)),
                  not missing, "missing=%s tools=%s" % (missing, tools))
        sid = session(base)
        result = chat(base, sid, "明天上午 9 点给妈妈转 100 元")
        tools = _result_tools(result)
        c.add("预约转账句不会出现立即转账工具（get_payees/create_transfer）",
              "create_transfer" not in tools and "get_payees" not in tools, tools)
        status = llm_status(base)
        stats = status.get("stats") or {}
        c.add("模型模式统计：stats.calls >= 7", int(stats.get("calls") or 0) >= 7, stats.get("calls"))
        c.add("degraded == false", status.get("degraded") is False, status.get("degraded"))
    return c


SCENARIOS = {
    "1": scenario_1, "2": scenario_2, "3": scenario_3, "4": scenario_4,
    "5": scenario_5, "6": scenario_6, "7": scenario_7, "8": scenario_8,
    "9": scenario_9,
}


def main() -> int:
    args = [a for a in sys.argv[1:]]
    only = None
    as_json = "--json" in args
    if "--only" in args:
        idx = args.index("--only")
        if idx + 1 < len(args):
            only = [x.strip() for x in args[idx + 1].split(",") if x.strip()]
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    print("ZeroStep v%s LLM e2e（桩 %s | 主服务 %s:%d）" % (_repo_version(), STUB_BASE, HOST,
                                                          APP_PORT))
    stub = Stub()
    results = []
    try:
        stub.start()
        for key in sorted(SCENARIOS):
            if only and key not in only:
                continue
            checks = SCENARIOS[key](stub)
            results.append(checks)
            for item in checks.items:
                print("  %s  %-46s %s" % ("PASS" if item["ok"] else "FAIL", item["name"], item["detail"]))
            passed = sum(1 for item in checks.items if item["ok"])
            print("%s  %s：%s（%d/%d 项）\n" % ("PASS" if checks.ok else "FAIL", checks.scenario,
                                                "PASS" if checks.ok else "FAIL", passed, len(checks.items)))
    except Exception as exc:  # noqa: BLE001 - 环境/启动错误统一报错并返回 2
        print("[环境错误] %s" % exc)
        return 2
    finally:
        stub.stop()

    all_items = [item for checks in results for item in checks.items]
    total = len(all_items)
    passed = sum(1 for item in all_items if item["ok"])
    scenario_ok = sum(1 for checks in results if checks.ok)
    print("=" * 72)
    print("场景汇总：%d/%d 场景通过；检查项：%d/%d 通过" % (scenario_ok, len(results), passed, total))
    for checks in results:
        print("  %s  %s（%d/%d）" % ("PASS" if checks.ok else "FAIL", checks.scenario,
                                    sum(1 for i in checks.items if i["ok"]), len(checks.items)))
    if as_json:
        print(json.dumps({"scenarios": [{"scenario": c.scenario, "ok": c.ok, "items": c.items}
                                        for c in results]}, ensure_ascii=False))
    failed = [c for c in results if not c.ok]
    return 0 if not failed and results else 1


if __name__ == "__main__":
    sys.exit(main())
