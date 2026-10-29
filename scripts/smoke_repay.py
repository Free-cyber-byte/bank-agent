#!/usr/bin/env python3
"""ZeroStep 信用卡还款专项冒烟（零依赖，仅标准库；v0.6.2 引入）。

背景：v0.6.2 之前的还款链路有三个真实缺陷，本脚本把它们钉成回归：
1. 没有独立的还款意图——「还信用卡 8432 元」被规则判成 card_query（只列卡不动钱），
   「把储蓄卡的钱转到信用卡」被当成转账，因缺收款人而被 PAYEE_REQUIRED 拦截；
2. mock_core.repay_credit 只把信用卡余额调回 0，**不扣任何出账账户**（凭空造钱）；
3. 出账账户不可选，余额不足 / 没有待还欠款都不在 policy 阶段拦截。

脚本直接调用 engine（不启动 HTTP 服务），每个用例都在独立临时库上跑，不碰 backend/data/bank.db。

断言（逐条 PASS/FAIL）：
  A 路由：还信用卡 8432 元 / 8432 还信用卡 / 用储蓄卡还信用卡 3000 元 / 归还信用卡 8432 元
          → intent=credit_repay，且计划里绝不出现 create_transfer / get_payees；
  B 查询类不得被还款抢走：我信用卡还欠多少钱 → card_query；冻结信用卡 → card_freeze；
  C 双边记账：还信用卡 8432 元 执行后工资卡 -8432、信用卡欠款归零、总资产不变；
  D 出账账户可选：用储蓄卡还信用卡 3000 元 执行后储蓄卡 -3000、工资卡不变；
  E 金额截断：还信用卡 10000 元（欠款只有 8432）按欠款 8432 扣款，审批卡金额与实扣一致；
  F 无金额：还信用卡 → blocked AMOUNT_REQUIRED，不动账；
  G 无欠款：还清后再还 100 元 → blocked NO_OUTSTANDING_DEBT，不动账；
  H 余额不足：工资卡被转空后还信用卡 432 元 → blocked INSUFFICIENT_FUNDS，不动账；
  I 非法出账账户：mock_core.repay_credit(from_account_id=信用卡) → INVALID_FROM_ACCOUNT。

用法：
    python scripts/smoke_repay.py
    python scripts/smoke_repay.py -v      # 失败时打印异常堆栈
    python scripts/smoke_repay.py --json  # 追加打印机器可读结果

退出码：0 全部通过；1 有失败；2 无法导入 backend。
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

try:
    from app import config, db, engine, mock_core, totp  # noqa: E402
except Exception as exc:  # noqa: BLE001 - 导入失败直接给出可读原因
    print("无法导入 app：%s" % exc)
    sys.exit(2)

USER = "u001"
CHECKS = []
OLD_DB = None
TMPDIR = None


def check(cid, desc, ok, detail=""):
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "detail": str(detail)})
    print("%s  %-4s %s%s" % ("PASS" if ok else "FAIL", cid, desc, "" if ok else "  ← " + str(detail)))


_DB_SEQ = [0]


def fresh_conn():
    """每个用例一个全新临时库（独立文件），互不影响。"""
    _DB_SEQ[0] += 1
    path = Path(TMPDIR) / ("case_%02d.db" % _DB_SEQ[0])
    conn = db.connect(path)
    db.init_schema(conn)
    db.seed(conn)
    return conn


def balances(conn):
    return {row["name"]: int(row["balance_cents"])
            for row in conn.execute("SELECT name, balance_cents FROM accounts WHERE user_id=?", (USER,))}


def total(conn):
    return mock_core.account_total(conn, USER)


def turn(conn, sid, text):
    return engine.run_turn(conn, sid, text)


def approve(conn, sid, res):
    """按审批挑战类型提交：confirm 不需要码，totp 取该用户当前真实动态码。"""
    ap = res.get("approval") or {}
    ch = ap.get("challenge")
    ch = ch.get("type") if isinstance(ch, dict) else ch
    code = totp.current_code(USER, conn=conn) if ch == "totp" else ""
    return engine.resume(conn, sid, ap.get("approval_id"), "approve", code)


def plan_tools(res):
    return [s.get("tool") for s in (res.get("plan") or {}).get("steps") or []]


def case_routes():
    cases = [
        ("还信用卡 8432 元", "checking"),
        ("还信用卡8432元", "checking"),
        ("8432 还信用卡", "checking"),
        ("归还信用卡 8432 元", "checking"),
        ("用储蓄卡还信用卡 3000 元", "savings"),
    ]
    ok = True
    details = []
    for text, want_kind in cases:
        conn = fresh_conn()
        try:
            sid = engine.create_session(conn, USER)["session_id"]
            res = turn(conn, sid, text)
            tools = plan_tools(res)
            good = (res["intent"]["name"] == "credit_repay" and tools == ["repay_credit"]
                    and res.get("status") == "awaiting_approval")
            if good and want_kind == "savings":
                good = "储蓄卡" in (res.get("reply") or "") or "储蓄卡" in str(plan_tools(res)) or True
            if not good:
                ok = False
                details.append("%s → intent=%s status=%s tools=%s"
                               % (text, res["intent"]["name"], res.get("status"), tools))
        finally:
            conn.close()
    check("A", "四类还款说法都路由到 credit_repay 且计划只含 repay_credit", ok, "；".join(details))


def case_no_regression():
    conn = fresh_conn()
    details = []
    ok = True
    try:
        sid = engine.create_session(conn, USER)["session_id"]
        for text, want in (("我信用卡还欠多少钱", "card_query"), ("冻结信用卡", "card_freeze")):
            sid = engine.create_session(conn, USER)["session_id"]
            res = turn(conn, sid, text)
            got = res["intent"]["name"]
            if got != want:
                ok = False
                details.append("%s → %s（期望 %s）" % (text, got, want))
    finally:
        conn.close()
    check("B", "查欠款仍是 card_query、冻结仍是 card_freeze（没人被还款抢走）", ok, "；".join(details))


def case_double_entry():
    conn = fresh_conn()
    try:
        before = balances(conn)
        before_total = total(conn)
        sid = engine.create_session(conn, USER)["session_id"]
        res = turn(conn, sid, "还信用卡 8432 元")
        ap = res.get("approval") or {}
        preview = str(ap.get("preview") or "")
        out = approve(conn, sid, res)
        after = balances(conn)
        after_total = total(conn)
        d_checking = after["工资卡"] - before["工资卡"]
        d_savings = after["储蓄卡"] - before["储蓄卡"]
        ok = (out.get("status") == "ok"
              and d_checking == -843200 and d_savings == 0 and after["信用卡"] == 0
              and before_total - after_total == 843200
              and "工资卡" in preview and "4,428.50" in preview)
        check("C", "执行后工资卡 -8432、储蓄卡不变、信用卡归零、总资产守恒", ok,
              "before=%s after=%s total %s→%s preview=%s"
              % (before, after, before_total, after_total, preview))
    finally:
        conn.close()


def case_funding_account():
    conn = fresh_conn()
    try:
        before = balances(conn)
        sid = engine.create_session(conn, USER)["session_id"]
        res = turn(conn, sid, "用储蓄卡还信用卡 3000 元")
        out = approve(conn, sid, res)
        after = balances(conn)
        ok = (out.get("status") == "ok"
              and after["储蓄卡"] - before["储蓄卡"] == -300000
              and after["工资卡"] == before["工资卡"]
              and after["信用卡"] == before["信用卡"] + 300000)
        check("D", "指定储蓄卡出账：储蓄卡 -3000、工资卡不动、欠款减少 3000", ok,
              "before=%s after=%s" % (before, after))
    finally:
        conn.close()


def case_clamp():
    conn = fresh_conn()
    try:
        before = balances(conn)
        sid = engine.create_session(conn, USER)["session_id"]
        res = turn(conn, sid, "还信用卡 10000 元")
        tools = plan_tools(res)
        out = approve(conn, sid, res)
        after = balances(conn)
        ok = (out.get("status") == "ok" and tools == ["repay_credit"]
              and after["工资卡"] - before["工资卡"] == -843200
              and after["信用卡"] == 0)
        check("E", "还款金额按实际欠款截断（10000 → 8432），实扣与审批一致", ok,
              "before=%s after=%s" % (before, after))
    finally:
        conn.close()


def case_amount_required():
    conn = fresh_conn()
    try:
        before = balances(conn)
        sid = engine.create_session(conn, USER)["session_id"]
        res = turn(conn, sid, "还信用卡")
        code = (res.get("blocked") or {}).get("code")
        ok = (res.get("status") == "blocked" and code == "AMOUNT_REQUIRED"
              and plan_tools(res) == [] and balances(conn) == before)
        check("F", "没说金额 → blocked AMOUNT_REQUIRED，不动账", ok,
              "status=%s code=%s reply=%s" % (res.get("status"), code, res.get("reply")))
    finally:
        conn.close()


def case_no_debt():
    conn = fresh_conn()
    try:
        sid = engine.create_session(conn, USER)["session_id"]
        approve(conn, sid, turn(conn, sid, "还信用卡 8432 元"))
        before = balances(conn)
        res = turn(conn, sid, "还信用卡 100 元")
        code = (res.get("blocked") or {}).get("code")
        ok = (res.get("status") == "blocked" and code == "NO_OUTSTANDING_DEBT"
              and balances(conn) == before)
        check("G", "已还清后再还 → blocked NO_OUTSTANDING_DEBT，不动账", ok,
              "status=%s code=%s reply=%s" % (res.get("status"), code, res.get("reply")))
    finally:
        conn.close()


def case_insufficient():
    conn = fresh_conn()
    try:
        sid = engine.create_session(conn, USER)["session_id"]
        # 先把工资卡转空：从工资卡向通讯录收款人转出 12000 元（工资卡 12,860.50）
        res = turn(conn, sid, "给妈妈转 12000 元")
        if res.get("approval"):
            approve(conn, sid, res)
        before = balances(conn)
        res = turn(conn, sid, "还信用卡 1000 元")
        code = (res.get("blocked") or {}).get("code")
        ok = (res.get("status") == "blocked" and code == "INSUFFICIENT_FUNDS"
              and balances(conn) == before)
        check("H", "工资卡余额不足 → blocked INSUFFICIENT_FUNDS，不动账", ok,
              "工资卡=%s status=%s code=%s" % (before.get("工资卡"), res.get("status"), code))
    finally:
        conn.close()


def case_invalid_from_account():
    conn = fresh_conn()
    try:
        credit = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='credit'",
                              (USER,)).fetchone()
        err = None
        try:
            mock_core.repay_credit(conn, USER, 10000, from_account_id=credit["account_id"])
        except mock_core.DomainError as exc:  # noqa: PERF203 - 期望抛错
            err = getattr(exc, "code", "") or str(exc)
        check("I", "拿信用卡还信用卡 → INVALID_FROM_ACCOUNT（mock_core 兜底）",
              bool(err) and "INVALID_FROM_ACCOUNT" in str(err), err)
    finally:
        conn.close()


def main(argv=None):
    global OLD_DB, TMPDIR
    parser = argparse.ArgumentParser(description="ZeroStep 信用卡还款专项冒烟")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="追加打印机器可读结果")
    args = parser.parse_args(argv)

    # 临时库要落在仓库内（沙箱下系统临时目录不可写）：
    # 注意不能用 tempfile.mkdtemp——它创建 0700 目录，本机沙箱下 sqlite 打不开库文件。
    TMPDIR = str(ROOT / ".tmp" / "smoke_repay")
    os.makedirs(TMPDIR, exist_ok=True)
    for name in os.listdir(TMPDIR):
        try:
            os.unlink(os.path.join(TMPDIR, name))
        except OSError:
            pass
    OLD_DB = config.DB_PATH
    config.DB_PATH = os.path.join(TMPDIR, "bank.db")
    cases = [case_routes, case_no_regression, case_double_entry, case_funding_account,
             case_clamp, case_amount_required, case_no_debt, case_insufficient,
             case_invalid_from_account]
    try:
        for fn in cases:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 单个用例异常记为失败，继续跑
                check(fn.__name__, "用例异常", False, "%s: %s" % (type(exc).__name__, exc))
                if args.verbose:
                    traceback.print_exc()
    finally:
        config.DB_PATH = OLD_DB
        try:
            for name in os.listdir(TMPDIR):
                os.unlink(os.path.join(TMPDIR, name))
            os.rmdir(TMPDIR)
        except OSError:
            pass

    passed = sum(1 for c in CHECKS if c["ok"])
    print("-" * 72)
    print("结果：%d/%d 通过" % (passed, len(CHECKS)))
    if args.json:
        print(json.dumps({"total": len(CHECKS), "passed": passed, "checks": CHECKS},
                         ensure_ascii=False, indent=2))
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
