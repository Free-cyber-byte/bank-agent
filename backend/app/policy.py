"""三级风险策略引擎（确定性，不依赖模型）。

low  绿：纯查询/提醒，自动执行
medium 黄：小额转账、订阅取消、虚拟卡申请/冻结，用户一次确认
high 红：大额转账、新收款人、拆分规避、理财申购，动态验证码 + 确认
blocked：余额不足、超硬上限、缺少收款人、注入/越权攻击
"""
from typing import Any, Dict, List, Optional, Tuple

from . import config, mock_core
from .mock_core import yuan

LOW_TOOLS = {"get_accounts", "get_transactions", "get_bill_summary", "budget_analysis", "list_wealth",
             "list_payees", "get_payees", "list_subscriptions", "list_cards", "create_reminder", "get_products",
             "get_recent_transactions"}
MEDIUM_TOOLS = {"cancel_subscription", "apply_virtual_card", "set_card_status", "create_transfer"}
HIGH_TOOLS = {"purchase_wealth"}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def max_risk(a: str, b: str) -> str:
    return a if RISK_ORDER.get(a, 0) >= RISK_ORDER.get(b, 0) else b


def evaluate(conn, user_id: str, steps: List[Dict[str, Any]], intent: Dict[str, Any]) -> Dict[str, Any]:
    risk = "low"
    reasons: List[str] = []
    challenge = None
    blocked: Optional[Dict[str, str]] = None
    amount_total = 0
    counterparty = ""
    action_summary = ""
    planned_transfer_total = 0

    for step in steps:
        tool = step["tool"]
        args = step.get("args", {}) or {}
        step_risk = "low"
        if tool == "create_transfer":
            amount = int(args.get("amount_cents") or 0)
            amount_total += amount
            planned_transfer_total += amount
            if amount <= 0:
                blocked = blocked or {"code": "AMOUNT_INVALID", "message": "转账金额不合法"}
                continue
            if amount > config.HARD_SINGLE_LIMIT_CENTS:
                blocked = blocked or {"code": "LIMIT_EXCEEDED", "message": "单笔金额超过演示系统上限 5 万元，已拦截"}
                continue
            if not args.get("payee_name"):
                blocked = blocked or {"code": "PAYEE_REQUIRED", "message": "没有识别到收款人，请补充收款人姓名"}
                continue
            if args.get("payee_is_new") and not args.get("payee_account"):
                blocked = blocked or {"code": "NEW_PAYEE_UNVERIFIED",
                                      "message": "首次向新收款人「%s」转账，需先补充并核实对方账户信息" % args.get("payee_name")}
                continue
            acc = conn.execute("SELECT * FROM accounts WHERE account_id=? AND user_id=?", (args.get("from_account_id"), user_id)).fetchone()
            if acc and int(acc["balance_cents"]) < amount:
                blocked = blocked or {"code": "INSUFFICIENT_FUNDS", "message": "账户余额不足，当前可用 %s" % yuan(int(acc["balance_cents"]))}
                continue
            cumulative = mock_core.today_transfer_total(conn, user_id) + planned_transfer_total
            step_risk = "medium"
            if args.get("internal"):
                reasons.append("资金在自己名下账户之间划转，按中风险处理")
            else:
                if amount > config.SINGLE_LIMIT_CENTS or cumulative > config.DAILY_LIMIT_CENTS:
                    step_risk = "high"
                    reasons.append("24 小时累计转账将达到 %s，超过 1000 元小额阈值" % yuan(cumulative))
                if args.get("payee_is_new"):
                    step_risk = "high"
                    reasons.append("收款人「%s」是首次转账，强制强验证" % args.get("payee_name"))
            counterparty = args.get("payee_name") or ""
            action_summary = "向「%s」划转 %s" % (counterparty, yuan(amount))
        elif tool == "purchase_wealth":
            step_risk = "high"
            amount = int(args.get("amount_cents") or 0)
            amount_total += amount
            reasons.append("理财申购会划出资金，属于高风险操作")
            counterparty = args.get("product_name") or "理财产品"
            action_summary = "申购「%s」%s" % (counterparty, yuan(amount))
            info = mock_core.list_wealth(conn, user_id)
            pid = args.get("product_id")
            prod = next((p for p in info["products"] if p["product_id"] == pid), None)
            if not prod:
                blocked = blocked or {"code": "PRODUCT_NOT_FOUND", "message": "没有找到该理财产品"}
                continue
            if amount < int(prod["min_amount_cents"]):
                blocked = blocked or {"code": "BELOW_MIN_AMOUNT", "message": "起购金额为 %s" % yuan(int(prod["min_amount_cents"]))}
                continue
            if amount > 0:
                acc = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
                if acc and int(acc["balance_cents"]) < amount:
                    blocked = blocked or {"code": "INSUFFICIENT_FUNDS", "message": "工资卡余额不足，当前 %s" % yuan(int(acc["balance_cents"]))}
                    continue
        elif tool == "repay_credit":
            step_risk = "medium"
            amount_total += int(args.get("amount_cents") or 0)
            counterparty = "信用卡还款"
            action_summary = "归还信用卡欠款 %s" % yuan(int(args.get("amount_cents") or 0))
        elif tool in ("cancel_subscription", "apply_virtual_card", "set_card_status"):
            step_risk = "medium"
            if tool == "cancel_subscription":
                action_summary = "取消订阅「%s」" % (args.get("merchant") or args.get("sub_id") or "")
            elif tool == "apply_virtual_card":
                action_summary = "申请一张虚拟卡，单日限额 %s" % yuan(int(args.get("daily_limit_cents") or 200000))
            else:
                status_text = "冻结" if args.get("status") == "frozen" else "解冻"
                action_summary = "%s银行卡 %s" % (status_text, args.get("card_last4", ""))
        elif tool in LOW_TOOLS:
            step_risk = "low"
        else:
            step_risk = "medium"
        risk = max_risk(risk, step_risk)

    if blocked:
        return {"risk_level": "blocked", "challenge": None, "reasons": reasons, "blocked": blocked,
                "amount_cents": amount_total, "counterparty": counterparty,
                "summary": action_summary, "requires_approval": False}
    if risk == "high":
        challenge = "totp"
        if not reasons:
            reasons.append("高风险资金操作，需要动态验证码二次确认")
    elif risk == "medium":
        challenge = "confirm"
        if not reasons:
            reasons.append("按银行权限分级，该操作需要用户确认后执行")
    return {"risk_level": risk, "challenge": challenge, "reasons": reasons, "blocked": None,
            "amount_cents": amount_total, "counterparty": counterparty,
            "summary": action_summary, "requires_approval": risk in ("medium", "high")}
