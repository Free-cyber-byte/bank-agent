#!/usr/bin/env python3
"""ZeroStep v0.2.0 技能卡自检（零依赖，仅标准库）。

背景：v0.2.0 新增 6 张技能卡（transfer/bills/wealth/cards/subscriptions/cross），
每张卡 3 条 examples，共 18 条。契约要求：每条 example 必须命中「自身技能卡」，
且跨卡误命中为 0；技能路由失败不得影响主流程。

用法：
    python scripts/skill_check.py
    python scripts/skill_check.py -v     # 逐条打印 18 条 example 的命中结果

断言：
  skl-1 六张技能卡齐全（id/name/triggers/tools/examples），examples 共 18 条；
  skl-2 18 条 examples 每条 match() 恰好命中自身卡、且结果长度 == 1（18/18）；
  skl-3 status() count=6，每卡 hits 计数正确（18 条匹配后各 3 次）；
  skl-4 summary(ids) 行数 <=3、行内含卡名、未知 id 被忽略；
  skl-5 get(未知 id) 返回 None，match('') 返回空列表，无关文本不误命中；
  skl-6 match() 最多返回 3 张卡（构造多卡触发文本）。

退出码：0 全部通过；1 有失败；2 无法导入 backend。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, ROOT)

try:
    from app import skills
except Exception as exc:  # noqa: BLE001
    print("无法导入 app.skills：%s" % exc)
    sys.exit(2)

EXPECTED_IDS = ["transfer", "bills", "wealth", "cards", "subscriptions", "cross"]
CHECKS = []


def _check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": detail})
    print("%s  %s  %-58s%s" % ("PASS" if ok else "FAIL", cid, desc, ("  " + detail) if detail else ""))


def run(verbose=False):
    skills.reload()
    cards = skills.all()
    by_id = {c.get("id"): c for c in cards}

    # ---------- skl-1 卡齐全、元数据完整 ----------
    problems = []
    total_examples = 0
    for cid in EXPECTED_IDS:
        card = by_id.get(cid)
        if not card:
            problems.append("缺少技能卡 %s" % cid)
            continue
        if not card.get("name"):
            problems.append("%s 缺 name" % cid)
        triggers = card.get("triggers") or []
        tools = card.get("tools") or []
        examples = card.get("examples") or []
        if not triggers:
            problems.append("%s 缺 triggers" % cid)
        if not tools:
            problems.append("%s 缺 tools" % cid)
        if len(examples) != 3:
            problems.append("%s examples=%d（期望 3）" % (cid, len(examples)))
        total_examples += len(examples)
    if set(by_id) != set(EXPECTED_IDS):
        problems.append("卡集合不符：%s" % sorted(by_id))
    if total_examples != 18:
        problems.append("examples 总数 %d（期望 18）" % total_examples)
    _check("skl-1", "六张技能卡齐全且字段完整（examples 共 18 条）", not problems,
           "；".join(problems[:5]))

    # ---------- skl-2 18 条 examples 各命中自身卡、结果长度 1 ----------
    bad = []
    checked = 0
    for cid in EXPECTED_IDS:
        card = by_id.get(cid) or {}
        for example in card.get("examples") or []:
            checked += 1
            ids = skills.match(example)
            if ids != [cid]:
                bad.append("%s -> %s" % (example, ids))
            if verbose:
                print("      %s  %r -> %s" % ("OK" if ids == [cid] else "BAD", example, ids))
    _check("skl-2", "18 条 examples 各恰好命中自身卡（结果长度==1）",
           checked == 18 and not bad,
           "%d/%d；误命中：%s" % (checked - len(bad), checked, "；".join(bad[:5])))

    # ---------- skl-3 status() 计数与 hits ----------
    st = skills.status()
    items = {s.get("id"): s for s in st.get("skills") or []}
    hit_problems = []
    for cid in EXPECTED_IDS:
        hits = int((items.get(cid) or {}).get("hits") or 0)
        if hits != 3:
            hit_problems.append("%s hits=%d" % (cid, hits))
    ok3 = (st.get("count") == 6 and len(st.get("skills") or []) == 6 and not hit_problems)
    _check("skl-3", "status() count=6，18 条匹配后每卡 hits=3", ok3,
           "count=%s %s" % (st.get("count"), "；".join(hit_problems[:5])))

    # ---------- skl-4 summary ----------
    lines = skills.summary(["transfer", "bills", "wealth"])
    line_problems = []
    if len(lines) > 3:
        line_problems.append("行数 %d > 3" % len(lines))
    for line in lines:
        if len(line) > 160 or not line.strip():
            line_problems.append("行长异常 %d" % len(line))
    if lines and not any("转账" in line for line in lines):
        line_problems.append("summary 未包含命中卡")
    unknown_lines = skills.summary(["not_exist"])
    if unknown_lines:
        line_problems.append("未知 id 未忽略")
    default_lines = skills.summary()
    if len(default_lines) > 3:
        line_problems.append("默认 summary 行数 %d > 3" % len(default_lines))
    _check("skl-4", "summary(ids) <=3 行且含卡信息，未知 id 忽略", not line_problems,
           "；".join(line_problems[:5]) or ("%d 行" % len(lines)))

    # ---------- skl-5 get / match 边界 ----------
    probe = skills.get("transfer") or {}
    ok5 = (probe.get("id") == "transfer" and skills.get("no_such_skill") is None
           and skills.match("") == [] and skills.match("今天天气怎么样呀") == [])
    _check("skl-5", "get 已知/未知技能卡、match 空串/无关文本返回正确", ok5,
           "get(transfer).id=%s unknown=%s" % (probe.get("id"), skills.get("no_such_skill")))

    # ---------- skl-6 match 最多 3 张 ----------
    many = skills.match("帮我转账、看看账单、买点理财、管管信用卡和订阅，再做个预算")
    _check("skl-6", "多卡触发文本 match() 最多返回 3 张卡", 0 < len(many) <= 3,
           "命中 %s" % (many,))

    passed = sum(1 for c in CHECKS if c["ok"])
    total = len(CHECKS)
    print("-" * 80)
    print("结果：%d/%d 项通过" % (passed, total))
    return passed == total


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 技能卡自检")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印 18 条 example 的命中结果")
    args = parser.parse_args(argv)
    return 0 if run(verbose=args.verbose) else 1


if __name__ == "__main__":
    sys.exit(main())
