"""回退链路冒烟：模型不可用时必须退回规则解析，且审计里能看出 source=fallback。

用法：
    python scripts/smoke_llm_fallback.py
不需要真实 API Key：把 Base URL 指向本机不存在的端口即可模拟网络故障。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy-for-fallback-test")

from app import config, db, engine, llm, mock_core  # noqa: E402


def main():
    checks = []
    llm.set_mode("llm")
    old_base, old_timeout = config.LLM_BASE_URL, config.LLM_TIMEOUT
    config.LLM_BASE_URL = "http://127.0.0.1:9"   # discard 端口，必然连不上
    config.LLM_TIMEOUT = 2
    try:
        tmp = ROOT / ".eval_tmp"
        tmp.mkdir(exist_ok=True)
        config.DB_PATH = str(tmp / "fallback.db")
        conn = db.connect()
        db.init_schema(conn)
        db.seed(conn)
        ctx = {"accounts": [dict(r) for r in conn.execute("SELECT * FROM accounts WHERE user_id='u001'")],
               "cards": [dict(r) for r in conn.execute("SELECT * FROM cards WHERE user_id='u001'")],
               "subscriptions": [dict(r) for r in conn.execute("SELECT * FROM subscriptions WHERE user_id='u001'")],
               "payees": mock_core.list_payees(conn, "u001")}
        out = llm.parse("帮我查一下这个月账单", ctx)
        checks.append(("网络故障回退规则", out.get("source") == "fallback", out.get("source")))
        checks.append(("回退后仍能识别意图", out.get("name") == "query_bills", out.get("name")))
        checks.append(("回退原因可追溯", bool(out.get("fallback_reason")), str(out.get("fallback_reason"))[:60]))

        session = engine.create_session(conn, "u001")
        result = engine.run_turn(conn, session["session_id"], "帮我查一下这个月账单")
        intent = result.get("intent") or {}
        checks.append(("端到端仍能返回结果", result.get("status") == "ok" and bool(result.get("reply")), result.get("status")))
        checks.append(("会话可见回退来源", intent.get("source") == "fallback", intent.get("source")))
        chain = __import__("app.audit", fromlist=["audit"]).verify(conn)
        checks.append(("审计链在回退后仍完整", chain.get("valid") is True, "checked=%s" % chain.get("checked")))
        conn.close()
        try:
            os.unlink(config.DB_PATH)
        except OSError:
            pass
    finally:
        config.LLM_BASE_URL, config.LLM_TIMEOUT = old_base, old_timeout
        llm.set_mode("auto")

    failed = 0
    for name, ok, detail in checks:
        print("%s  %-24s %s" % ("PASS" if ok else "FAIL", name, detail))
        failed += 0 if ok else 1
    print("结果：%d/%d 通过" % (len(checks) - failed, len(checks)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
