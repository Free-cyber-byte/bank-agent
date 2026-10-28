"""ZeroStep 评测运行器：读取 tasks/t*.json，逐条在独立数据库中回放并断言。

用法：
    python scripts/run_tasks.py
    python scripts/run_tasks.py -v --filter t06,t07
    python scripts/run_tasks.py --category security --json report.json

v0.2.0 可选断言扩展（旧用例不写这些字段时行为与旧版完全一致）：
    expect.env                  该用例运行期间临时覆盖的环境变量，如 {"MCP_ENABLED": "0"}
    expect.expected_skill       技能 id 或 id 列表，断言 intent.skills 包含
    expect.expected_tools       工具名列表，断言本轮（含审批后）实际执行过
    expect.expected_rules       断言审批单 approval.hits 的规则名包含；元素可为字符串
                                或 {"rule": 名称, "contains": detail 子串, "digit": true}
    expect.reply_contains_any   回复至少包含列表中的一个关键词（用于「外部成功或降级」两可场景）
    expect.expected_attack      断言 status == blocked 且 blocked.code 等于该值
    expect.expected_attack_type 断言 intent.attack_type 等于该值
"""
import argparse
import importlib
import json
import os
import re
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


def _apply_env(overrides):
    """临时覆盖环境变量并原地重载 app.config。

    engine/mcp_client/skills 等模块必须在函数内动态读 config.X，重载才立即生效。
    返回 {key: 原值或 None}，供 _restore_env 还原。
    """
    saved = {}
    for key, value in (overrides or {}).items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    if overrides:
        importlib.reload(config)
    return saved


def _restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if saved:
        importlib.reload(config)


def approval_hits(result):
    """取审批单顶层暴露的 policy hits（v0.2.0 起 serialize_approval 顶层带 hits）。"""
    approval = result.get("approval") or {}
    hits = approval.get("hits")
    return hits if isinstance(hits, list) else []


def run_one(task, verbose=False):
    tmpdir = str(ROOT / ".eval_tmp" / task["id"])
    os.makedirs(tmpdir, exist_ok=True)
    old_db = config.DB_PATH
    env_saved = {}
    failures = []
    info = {}
    conn = None
    try:
        env_saved = _apply_env(task.get("env"))
        config.DB_PATH = os.path.join(tmpdir, "bank.db")
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
            if approval:
                if "approval_hits" not in info:
                    info["approval_hits"] = approval_hits(result)
                if "approval" not in info:
                    info["approval"] = {
                        "risk": approval.get("risk_level"),
                        "challenge": (approval.get("challenge") or {}).get("type"),
                    }
                if exp.get("approval") or exp.get("after"):
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
        info["skills"] = list((first.get("intent") or {}).get("skills") or [])
        info["attack_type"] = (first.get("intent") or {}).get("attack_type")
        info["executed_all"] = executed_tools(first) + executed_tools(final or {})

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
        expected_skill = exp.get("expected_skill")
        if expected_skill:
            wanted_skills = expected_skill if isinstance(expected_skill, list) else [expected_skill]
            actual_skills = info.get("skills") or []
            missing_skills = [s for s in wanted_skills if s not in actual_skills]
            if missing_skills:
                failures.append("技能未命中：期望包含 %s，实际 intent.skills=%s" % (missing_skills, actual_skills))
        if exp.get("expected_tools") is not None:
            actual_tools = info.get("executed_all") or []
            missing_tools = [t for t in exp["expected_tools"] if t not in actual_tools]
            if missing_tools:
                failures.append("期望执行未发生：%s，实际执行 %s" % (missing_tools, actual_tools))
        expected_rules = exp.get("expected_rules") or []
        if expected_rules:
            hits = info.get("approval_hits") or []
            rule_names = [h.get("rule") for h in hits if isinstance(h, dict)]
            hits_by_name = {}
            for hit in hits:
                if isinstance(hit, dict) and hit.get("rule"):
                    hits_by_name.setdefault(hit["rule"], hit)
            for item in expected_rules:
                if isinstance(item, str):
                    if item not in rule_names:
                        failures.append("审批规则未命中：期望 %s，实际 hits=%s" % (item, rule_names))
                    continue
                rule = (item or {}).get("rule")
                if rule not in hits_by_name:
                    failures.append("审批规则未命中：期望 %s，实际 hits=%s" % (rule, rule_names))
                    continue
                detail = str(hits_by_name[rule].get("detail") or "")
                if item.get("contains") and item["contains"] not in detail:
                    failures.append("规则 %s 详情缺少关键词：%s，实际「%s」" % (rule, item["contains"], detail))
                if item.get("digit") and not re.search(r"\d", detail):
                    failures.append("规则 %s 详情缺少具体数字：「%s」" % (rule, detail))
        if exp.get("expected_attack_type"):
            actual_attack_type = info.get("attack_type")
            if actual_attack_type != exp["expected_attack_type"]:
                failures.append("攻击类型不符：期望 %s，实际 %s" % (exp["expected_attack_type"], actual_attack_type))
        if exp.get("expected_attack"):
            expected_code = exp["expected_attack"]
            if info.get("status") != "blocked":
                failures.append("期望被拦截（blocked），实际状态 %s" % info.get("status"))
            if info.get("blocked") != expected_code:
                failures.append("拦截码不符：期望 %s，实际 %s" % (expected_code, info.get("blocked")))
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
        any_needles = exp.get("reply_contains_any") or []
        if any_needles and not any(str(n) in text for n in any_needles):
            failures.append("回复未包含任一关键词：%s（实际回复：%s）"
                            % ("/".join(str(n) for n in any_needles), text[:120]))
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
        try:
            _restore_env(env_saved)
        finally:
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
    # Windows 控制台默认 GBK，回复里含 ¥、≈ 等字符时打印会抛 UnicodeEncodeError；
    # 统一按 UTF-8 输出（JSON 报告本身始终以 UTF-8 写文件，不受影响）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
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
