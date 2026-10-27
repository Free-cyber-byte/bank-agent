"""HTTP 冒烟测试：验证 LLM 模式接口与对话链路（需要服务已启动）。

用法：
    PORT=8031 python backend/server.py          # 终端 1
    python scripts/smoke_llm_http.py 8031       # 终端 2
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:%s" % (sys.argv[1] if len(sys.argv) > 1 else "8000")


def call(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    checks = []

    status, health = call("GET", "/api/health")
    checks.append(("GET /api/health", status == 200 and "llm_mode" in health, health.get("llm_mode")))

    status, st = call("GET", "/api/llm/status")
    checks.append(("GET /api/llm/status", status == 200 and st.get("mode") in ("rule", "llm", "auto"),
                   "%s -> %s, key=%s" % (st.get("mode"), st.get("effective"), st.get("key_masked"))))

    status, switched = call("POST", "/api/llm/mode", {"mode": "llm"})
    checks.append(("POST /api/llm/mode llm", status == 200 and switched.get("effective") == "llm",
                   switched.get("effective")))
    if not st.get("enabled"):
        print("未配置 DEEPSEEK_API_KEY，跳过对话断言")
    else:
        status, session = call("POST", "/api/session")
        sid = session["session_id"]
        status, result = call("POST", "/api/chat", {"session_id": sid, "text": "帮我看看这个月花了多少钱"})
        intent = result.get("intent") or {}
        checks.append(("对话意图走模型", intent.get("source") == "llm",
                       "%s / %s / %sms" % (intent.get("name"), intent.get("source"), intent.get("latency_ms"))))

    status, back = call("POST", "/api/llm/mode", {"mode": "rule"})
    checks.append(("切回规则模式", status == 200 and back.get("effective") == "rule", back.get("effective")))

    ok = False
    try:
        call("POST", "/api/llm/mode", {"mode": "banana"})
    except urllib.error.HTTPError as exc:
        ok = exc.code == 400
    checks.append(("非法 mode 返回 400", ok, ""))
    call("POST", "/api/llm/mode", {"mode": "auto"})

    failed = 0
    for name, ok, detail in checks:
        print("%s  %-26s %s" % ("PASS" if ok else "FAIL", name, detail))
        if not ok:
            failed += 1
    print("结果：%d/%d 通过" % (len(checks) - failed, len(checks)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
