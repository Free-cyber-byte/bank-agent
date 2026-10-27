"""ZeroStep 评测运行器：读取 tasks/t*.json，逐条在独立数据库中回放并断言。

用法：
    python scripts/run_tasks.py
    python scripts/run_tasks.py -v --filter t06,t07
    python scripts/run_tasks.py --category security --json report.json
"""
import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import audit, config, db, engine, totp  # noqa: E402

TASKS_DIR = ROOT / "tasks"


def load_tasks(filters, category):
    tasks = []
    for path in sorted(TASKS_DIR.glob("t*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print("[WARN] 跳过无法解析的任务文件 %s：%s" % (path.name, exc))
            continue
        tid = data.get("id") or path.stem
        if filters and tid not in filters:
            continue
        if category and data.get("category") != category:
            continue
        data.setdefault("id", tid)
        tasks.append(data)
    return tasks


def executed_tools(result):
    out = []
    for item in result.get("executed") or []:
        if isinstance(item, dict) and item.get("tool"):
            out.append(item["tool"])
    return out


def plan_tools(result):
    return [s.get("tool") for s in (result.get("plan") or {}).get("steps", [])]


def blocked_code(result):
    blocked = result.get("blocked")
    if isinstance(blocked, dict):
        return blocked.get("code")
    if isinstance(blocked, str):
        return blocked
    return None


def same_set(left, right):
    return sorted(left or []) == sorted(right or [])


def run_one(task, verbose=False):
    tmpdir = str(ROOT / ".eval_tmp" / task["id"])
    os.makedirs(tmpdir, exist_ok=True)
    old_db = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "bank.db")
    failures = []
    info = {}
    conn = None
    try:
        conn = db.connect()
        db.init_schema(conn)
        db.seed(conn)
        uid = task.get("user_id") or "u001"
        session = engine.create_session(conn, uid)
        sid = session["session_id"]
        exp = task.get("expect") or {}
        first = None
        final = None
        for text in task.get("turns") or []:
            result = engine.run_turn(conn, sid, text)
            if first is None:
                first = result
            final = result
            approval = result.get("approval")
            if approval and (exp.get("approval") or exp.get("after")):
                info["approval"] = {
                    "risk": approval.get("risk_level"),
                    "challenge": (approval.get("challenge") or {}).get("type"),
                }
                result = engine.resume(conn, sid, approval.get("approval_id"), "approve", totp.current_code(uid))
                final = result
                info["after_status"] = result.get("status")
                info["executed"] = executed_tools(result)
                info["undo"] = bool(result.get("undo"))
        if first is None:
            failures.append("任务没有 turns 或未产生结果")
            return False, failures, info

        info["intent"] = (first.get("intent") or {}).get("name")
        info["intent_source"] = (first.get("intent") or {}).get("source")
        info["status"] = first.get("status")
        info["plan_tools"] = plan_tools(first)
        info["blocked"] = blocked_code(first)
        info["reply"] = final.get("reply") if final else ""

        if exp.get("status"):
            actual = info.get("status")
            if exp["status"] != "awaiting_approval" and exp.get("after"):
                actual = info.get("after_status") or actual
            if actual != exp["status"]:
                failures.append("状态不符：期望 %s，实际 %s" % (exp["status"], actual))
        if exp.get("intent") and info.get("intent") != exp["intent"]:
            failures.append("意图不符：期望 %s，实际 %s" % (exp["intent"], info.get("intent")))
        if exp.get("plan_tools") is not None and not same_set(info.get("plan_tools"), exp["plan_tools"]):
            failures.append("计划工具不符：期望 %s，实际 %s" % (exp["plan_tools"], info.get("plan_tools")))
        if exp.get("blocked") and info.get("blocked") != exp["blocked"]:
            failures.append("拦截码不符：期望 %s，实际 %s" % (exp["blocked"], info.get("blocked")))
        if exp.get("approval"):
            actual_approval = info.get("approval") or {}
            if actual_approval.get("risk") != exp["approval"].get("risk"):
                failures.append("风险级不符：期望 %s，实际 %s" % (exp["approval"].get("risk"), actual_approval.get("risk")))
            if actual_approval.get("challenge") != exp["approval"].get("challenge"):
                failures.append("审批方式不符：期望 %s，实际 %s" % (exp["approval"].get("challenge"), actual_approval.get("challenge")))
        after = exp.get("after")
        if after:
            if after.get("status") and info.get("after_status") != after["status"]:
                failures.append("审批后状态不符：期望 %s，实际 %s" % (after["status"], info.get("after_status")))
            if after.get("executed") is not None and not same_set(info.get("executed"), after["executed"]):
                failures.append("执行工具不符：期望 %s，实际 %s" % (after["executed"], info.get("executed")))
            if after.get("undo") and not info.get("undo"):
                failures.append("期望返回可撤销的 action_id 与 60 秒窗口")
        text = info.get("reply") or ""
        for needle in exp.get("reply_contains") or []:
            if needle not in text:
                failures.append("回复缺少关键词：%s" % needle)
        executed = executed_tools(first) + executed_tools(final or {})
        for bad in exp.get("forbidden_executed") or []:
            if bad in executed:
                failures.append("禁止执行的工具被执行：%s" % bad)
        for bad in exp.get("forbidden_tools") or []:
            if bad in plan_tools(first):
                failures.append("计划中出现了禁止工具：%s" % bad)
        if exp.get("audit_valid"):
            chain = audit.verify(conn)
            info["audit"] = chain
            if not chain.get("valid"):
                failures.append("审计链校验失败：%s" % chain.get("reason"))
            if chain.get("checked", 0) <= 0:
                failures.append("审计链为空")
    except Exception as exc:  # noqa: BLE001
        failures.append("运行异常：%s: %s" % (type(exc).__name__, exc))
        if verbose:
            traceback.print_exc()
    finally:
        if conn is not None:
            conn.close()
        config.DB_PATH = old_db
        try:
            for name in os.listdir(tmpdir):
                try:
                    os.unlink(os.path.join(tmpdir, name))
                except OSError:
                    pass
            os.rmdir(tmpdir)
        except OSError:
            pass
    return (not failures), failures, info


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 银行 Agent 评测运行器")
    parser.add_argument("--filter", default="", help="逗号分隔的任务 id，如 t01,t06")
    parser.add_argument("--category", default="", help="只跑某个类别：bills/transfer/wealth/card/subscription/cross/security")
    parser.add_argument("--json", default="", help="把完整报告写入指定 JSON 文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印异常堆栈与回复全文")
    args = parser.parse_args(argv)

    filters = set(x.strip() for x in args.filter.split(",") if x.strip())
    tasks = load_tasks(filters, args.category)
    if not tasks:
        print("没有匹配的任务。")
        return 2

    print("ZeroStep 评测：共 %d 条任务" % len(tasks))
    print("-" * 72)
    rows = []
    passed = 0
    for task in tasks:
        ok, failures, info = run_one(task, verbose=args.verbose)
        if ok:
            passed += 1
        rows.append({"id": task["id"], "title": task.get("title", ""), "category": task.get("category", ""),
                     "ok": ok, "failures": failures, "info": info})
        mark = "PASS" if ok else "FAIL"
        print("%s  %s  %-22s  %s" % (mark, task["id"], task.get("title", ""), (info.get("intent") or "-")))
        if not ok:
            for item in failures:
                print("       - " + item)
            if args.verbose:
                print("       回复：" + str(info.get("reply"))[:400])
    total = len(tasks)
    print("-" * 72)
    print("结果：%d/%d 通过，%d 失败" % (passed, total, total - passed))
    if args.json:
        report = {"total": total, "passed": passed, "failed": total - passed, "tasks": rows}
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("报告已写入 " + args.json)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
