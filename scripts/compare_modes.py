"""规则模式 vs LLM 模式对比评测。

做什么：
1. 30 条标准用例分别在 rule / llm 两种模式下跑完整引擎，比较通过率；
2. 10 条口语化变体（同一意图的不同说法）只比较「意图是否听懂」，体现 LLM 的增益；
3. 记录两种模式下的解析耗时（p50/p95/均值）；
4. 安全检查用例（t26-t30）单独统计，证明接入模型没有削弱拦截能力。

用法：
    python scripts/compare_modes.py            # 需要 DEEPSEEK_API_KEY，否则 llm 模式自动回退规则
    python scripts/compare_modes.py --no-llm   # 只跑规则模式（离线可跑）

输出：
    scripts/eval_modes.json  机器可读
    scripts/eval_modes.md    可直接贴进技术文档的表格
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db, engine, llm, mock_core  # noqa: E402
import run_tasks as rt  # noqa: E402

VARIANTS = [
    {"id": "v01", "text": "这个月钱都花哪儿了", "intent": "query_bills"},
    {"id": "v02", "text": "我卡里还剩多少", "intent": "query_balance"},
    {"id": "v03", "text": "帮我把八百块转给我妈", "intent": "transfer"},
    {"id": "v04", "text": "腾讯视频那个会员帮我退了", "intent": "subscription_cancel"},
    {"id": "v05", "text": "我那几张卡都还好吗", "intent": "card_query"},
    {"id": "v06", "text": "钱到账了，8000 存到储蓄卡，3000 买稳健的，2000 还信用卡", "intent": "cross_scenario"},
    {"id": "v07", "text": "最近有没有什么不对劲的消费", "intent": "query_transactions"},
    {"id": "v08", "text": "下个月怎么攒点钱", "intent": "budget_plan"},
    {"id": "v09", "text": "我想买点稳利宝，一千块", "intent": "wealth_purchase"},
    {"id": "v10", "text": "把信用卡先冻了吧", "intent": "card_freeze"},
]

SECURITY_IDS = {"t26", "t27", "t28", "t29", "t30"}


def build_ctx():
    tmpdir = ROOT / ".eval_tmp" / "ctx"
    tmpdir.mkdir(parents=True, exist_ok=True)
    old = config.DB_PATH
    db_file = tmpdir / "bank.db"
    try:
        db_file.unlink()
    except OSError:
        pass
    config.DB_PATH = str(db_file)
    try:
        conn = db.connect()
        db.init_schema(conn)
        db.seed(conn)
        uid = config.DEFAULT_USER_ID
        ctx = {
            "payees": mock_core.list_payees(conn, uid),
            "products": mock_core.list_wealth(conn, uid)["products"],
            "subscriptions": mock_core.list_subscriptions(conn, uid)["subscriptions"],
            "cards": mock_core.list_cards(conn, uid),
            "accounts": mock_core.list_accounts(conn, uid),
            "risk_level": "稳健型",
        }
        conn.close()
        return ctx
    finally:
        config.DB_PATH = old
        try:
            db_file.unlink()
        except OSError:
            pass


def percentile(values, pct):
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def measure_parse(ctx):
    texts = []
    for task in rt.load_tasks(set(), ""):
        turns = task.get("turns") or [""]
        texts.append(turns[0])
    texts += [v["text"] for v in VARIANTS]
    latencies = []
    intents = []
    for text in texts:
        started = time.perf_counter()
        result = llm.parse(text, ctx)
        latencies.append((time.perf_counter() - started) * 1000)
        intents.append({"text": text, "intent": result.get("name"), "source": result.get("source"),
                        "fallback_reason": result.get("fallback_reason")})
    return {
        "calls": len(latencies),
        "avg_ms": round(sum(latencies) / len(latencies), 2),
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "max_ms": round(max(latencies), 2),
        "intents": intents,
    }


def run_suite(mode, tasks):
    llm._mode = mode
    llm._last_error = ""
    results = []
    passed = 0
    started = time.perf_counter()
    for task in tasks:
        ok, failures, info = rt.run_one(task, verbose=False)
        if ok:
            passed += 1
        results.append({"id": task["id"], "title": task.get("title", ""), "category": task.get("category", ""),
                        "ok": ok, "failures": failures, "intent": info.get("intent"),
                        "status": info.get("status"), "blocked": info.get("blocked"),
                        "source": (info.get("intent_source") or "")})
    elapsed = time.perf_counter() - started
    return {"passed": passed, "total": len(tasks), "elapsed_s": round(elapsed, 1), "results": results}


def check_variants(mode, ctx):
    llm._mode = mode
    hits = []
    for v in VARIANTS:
        result = llm.parse(v["text"], ctx)
        hits.append({"id": v["id"], "text": v["text"], "expect": v["intent"],
                     "actual": result.get("name"), "correct": result.get("name") == v["intent"],
                     "source": result.get("source")})
    return {"correct": sum(1 for h in hits if h["correct"]), "total": len(hits), "items": hits}


def security_summary(suite):
    """安全用例以“是否符合预期”为准：注入/钓鱼/越权应拦截，数据里的注入应识别但照常查询。"""
    rows = [r for r in suite["results"] if r["id"] in SECURITY_IDS]
    passed = sum(1 for r in rows if r["ok"])
    return {"passed": passed, "total": len(rows), "items": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 规则 vs LLM 对比评测")
    parser.add_argument("--no-llm", action="store_true", help="只跑规则模式（离线）")
    args = parser.parse_args(argv)

    tasks = rt.load_tasks(set(), "")
    ctx = build_ctx()
    modes = ["rule"] if args.no_llm else ["rule", "llm"]
    report = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode_available": llm.configured(),
              "model": config.LLM_MODEL, "modes": {}}

    for mode in modes:
        print("[%s] 跑 %d 条标准用例..." % (mode, len(tasks)))
        suite = run_suite(mode, tasks)
        print("[%s] 跑 %d 条口语化变体..." % (mode, len(VARIANTS)))
        variants = check_variants(mode, ctx)
        print("[%s] 测量解析耗时..." % mode)
        latency = measure_parse(ctx)
        report["modes"][mode] = {"suite": suite, "variants": variants, "latency": latency,
                                 "security": security_summary(suite)}

    out_json = ROOT / "scripts" / "eval_modes.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# ZeroStep 规则模式 vs LLM 模式对比")
    lines.append("")
    lines.append("生成时间：%s；模型：%s；Key 可用：%s" % (report["generated_at"], report["model"], "是" if report["mode_available"] else "否"))
    lines.append("")
    lines.append("| 指标 | 规则模式 | LLM 模式 |")
    lines.append("| --- | --- | --- |")
    rule = report["modes"].get("rule", {})
    llm_res = report["modes"].get("llm", {})

    def cell(mode_data, fn):
        return fn(mode_data) if mode_data else "-"

    lines.append("| 30 条标准用例通过率 | %s | %s |" % (
        cell(rule, lambda d: "%d/%d" % (d["suite"]["passed"], d["suite"]["total"])),
        cell(llm_res, lambda d: "%d/%d" % (d["suite"]["passed"], d["suite"]["total"]))))
    lines.append("| 10 条口语化变体听懂率 | %s | %s |" % (
        cell(rule, lambda d: "%d/%d" % (d["variants"]["correct"], d["variants"]["total"])),
        cell(llm_res, lambda d: "%d/%d" % (d["variants"]["correct"], d["variants"]["total"]))))
    lines.append("| 安全用例通过（5 条） | %s | %s |" % (
        cell(rule, lambda d: "%d/%d" % (d["security"]["passed"], d["security"]["total"])),
        cell(llm_res, lambda d: "%d/%d" % (d["security"]["passed"], d["security"]["total"]))))
    lines.append("| 解析耗时 均值 / p95 | %s | %s |" % (
        cell(rule, lambda d: "%.2f ms / %.2f ms" % (d["latency"]["avg_ms"], d["latency"]["p95_ms"])),
        cell(llm_res, lambda d: "%.2f ms / %.2f ms" % (d["latency"]["avg_ms"], d["latency"]["p95_ms"]))))
    lines.append("")
    lines.append("## 口语化变体明细（LLM 模式）")
    lines.append("")
    lines.append("| 用例 | 用户说法 | 期望意图 | 实际意图 | 结果 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for item in (llm_res.get("variants", {}) or {}).get("items", []):
        lines.append("| %s | %s | %s | %s | %s |" % (
            item["id"], item["text"], item["expect"], item["actual"], "✓" if item["correct"] else "✗"))
    lines.append("")
    if rule.get("variants"):
        lines.append("## 同一批变体在规则模式下的表现（说明词表覆盖的边界）")
        lines.append("")
        for item in rule["variants"]["items"]:
            if not item["correct"]:
                lines.append("- %s「%s」：期望 %s，实际 %s" % (item["id"], item["text"], item["expect"], item["actual"]))
    out_md = ROOT / "scripts" / "eval_modes.md"
    out_md.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    print("报告已写入 scripts/eval_modes.json 与 scripts/eval_modes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
