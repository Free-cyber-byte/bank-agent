"""三级风险策略引擎（确定性，不依赖模型）。

low  绿：纯查询/提醒，自动执行
medium 黄：小额转账、订阅取消、虚拟卡申请/冻结，用户一次确认
high 红：大额转账、新收款人、拆分规避、理财申购，动态验证码 + 确认
blocked：余额不足、超硬上限、缺少收款人、注入/越权攻击

v0.2.0 起，每次判定额外产出 hits（可解释判定项）：
    {rule, dimension, level, detail, value, threshold}
rule 是固定规则名（前端/测试/审计都按它断言），
detail 必须带具体数字（金额、笔数、阈值），不允许只写「风险较高」。
判定等级仍由原有逻辑决定，hits 只做解释与留痕，不改变 t01–t30 的判定结果。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import config, mock_core
from .mock_core import yuan

LOW_TOOLS = {"get_accounts", "get_transactions", "get_bill_summary", "budget_analysis", "list_wealth",
             "list_payees", "get_payees", "list_subscriptions", "list_cards", "create_reminder", "get_products",
             "get_recent_transactions", "fx_convert", "risk_screen",
             "scan_anomalies", "annual_report", "list_scheduled_transfers"}
MEDIUM_TOOLS = {"cancel_subscription", "apply_virtual_card", "create_transfer",
                "redeem_wealth", "set_card_limit", "cancel_scheduled_transfer"}
# v0.4.1：挂失/解挂按官方红线归入红色强验证（多因子 + TOTP）
HIGH_TOOLS = {"purchase_wealth", "card_spend", "create_scheduled_transfer", "set_card_status"}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# 频次维度：只解释、不升级判定（保证回归确定性）
FREQUENCY_WINDOW_MINUTES = 10
FREQUENCY_WARN_COUNT = 5
FREQUENCY_HIGH_COUNT = 12
MAX_HITS = 8


def max_risk(a: str, b: str) -> str:
    return a if RISK_ORDER.get(a, 0) >= RISK_ORDER.get(b, 0) else b


def _hit(rule: str, dimension: str, level: str, detail: str,
         value: Optional[Any] = None, threshold: Optional[Any] = None) -> Dict[str, Any]:
    """一条可解释判定项：固定规则名 + 维度 + 等级 + 带具体数字的说明。"""
    return {"rule": rule, "dimension": dimension, "level": level, "detail": detail,
            "value": value, "threshold": threshold}


def _add_hit(hits: List[Dict[str, Any]], hit: Dict[str, Any]) -> None:
    if len(hits) < MAX_HITS:
        hits.append(hit)


def _count_recent_transfers(conn, user_id: str, minutes: int = FREQUENCY_WINDOW_MINUTES) -> int:
    since = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM transfers WHERE user_id=? AND status='executed' AND created_at>=?",
        (user_id, since),
    ).fetchone()
    return int(row["c"] or 0)


def _payee_executed_count(conn, user_id: str, payee_name: str) -> int:
    if not payee_name:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM transfers WHERE user_id=? AND status='executed' AND payee_name=?",
        (user_id, payee_name),
    ).fetchone()
    return int(row["c"] or 0)


def evaluate(conn, user_id: str, steps: List[Dict[str, Any]], intent: Dict[str, Any]) -> Dict[str, Any]:
    risk = "low"
    reasons: List[str] = []
    hits: List[Dict[str, Any]] = []
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
                _add_hit(hits, _hit("amount_invalid", "金额", "high",
                                    "转账金额必须大于 0 元，当前为 %.2f 元" % (amount / 100.0),
                                    value=amount, threshold=0))
                continue
            if amount > config.HARD_SINGLE_LIMIT_CENTS:
                blocked = blocked or {"code": "LIMIT_EXCEEDED", "message": "单笔金额超过演示系统上限 5 万元，已拦截"}
                _add_hit(hits, _hit("amount_hard_limit", "金额（单笔）", "high",
                                    "单笔 %s 元 超过演示系统硬上限 %s 元，直接拦截" % (yuan(amount), yuan(config.HARD_SINGLE_LIMIT_CENTS)),
                                    value=amount, threshold=config.HARD_SINGLE_LIMIT_CENTS))
                continue
            if not args.get("payee_name"):
                blocked = blocked or {"code": "PAYEE_REQUIRED", "message": "没有识别到收款人，请补充收款人姓名"}
                _add_hit(hits, _hit("payee_unverified", "收款人", "high",
                                    "未识别到收款人姓名，转账金额 %s 元无法执行" % yuan(amount),
                                    value=0, threshold=1))
                continue
            if args.get("payee_is_new") and not args.get("payee_account"):
                blocked = blocked or {"code": "NEW_PAYEE_UNVERIFIED",
                                      "message": "首次向新收款人「%s」转账，需先补充并核实对方账户信息" % args.get("payee_name")}
                _add_hit(hits, _hit("payee_unverified", "收款人", "high",
                                    "收款人「%s」为首次转账且未提供账户，历史成功转账 0 笔" % args.get("payee_name"),
                                    value=0, threshold=1))
                continue
            acc = conn.execute("SELECT * FROM accounts WHERE account_id=? AND user_id=?", (args.get("from_account_id"), user_id)).fetchone()
            if acc and int(acc["balance_cents"]) < amount:
                blocked = blocked or {"code": "INSUFFICIENT_FUNDS", "message": "账户余额不足，当前可用 %s" % yuan(int(acc["balance_cents"]))}
                _add_hit(hits, _hit("insufficient_funds", "金额（余额）", "high",
                                    "账户可用 %s 元 < 转账金额 %s 元，缺口 %s 元" % (
                                        yuan(int(acc["balance_cents"])), yuan(amount), yuan(amount - int(acc["balance_cents"]))),
                                    value=amount, threshold=int(acc["balance_cents"])))
                continue
            cumulative = mock_core.today_transfer_total(conn, user_id) + planned_transfer_total
            step_risk = "medium"
            single_limit = config.SINGLE_LIMIT_CENTS
            daily_limit = config.DAILY_LIMIT_CENTS
            if args.get("internal"):
                reasons.append("资金在自己名下账户之间划转，按中风险处理")
                _add_hit(hits, _hit("reversed_window", "可逆性", "low",
                                    "同一用户账户间划转，执行后 60 秒内可撤销（窗口 %d 秒）" % config.UNDO_WINDOW_SECONDS,
                                    value=config.UNDO_WINDOW_SECONDS, threshold=60))
            else:
                if amount > single_limit or cumulative > daily_limit:
                    step_risk = "high"
                    reasons.append("24 小时累计转账将达到 %s，超过 1000 元小额阈值" % yuan(cumulative))
                if amount > single_limit:
                    _add_hit(hits, _hit("amount_single_limit", "金额（单笔）", "high",
                                        "单笔 %s 元 > 小额免密阈值 %s 元，升级为高风险" % (yuan(amount), yuan(single_limit)),
                                        value=amount, threshold=single_limit))
                else:
                    _add_hit(hits, _hit("amount_single_limit", "金额（单笔）", "medium",
                                        "单笔 %s 元 ≤ 小额阈值 %s 元，按中风险确认" % (yuan(amount), yuan(single_limit)),
                                        value=amount, threshold=single_limit))
                if cumulative > daily_limit:
                    _add_hit(hits, _hit("amount_daily_limit", "金额（日累计）", "high",
                                        "24 小时累计（含本次）%s 元 > 日累计阈值 %s 元" % (yuan(cumulative), yuan(daily_limit)),
                                        value=cumulative, threshold=daily_limit))
                else:
                    _add_hit(hits, _hit("amount_daily_limit", "金额（日累计）", "medium",
                                        "24 小时累计（含本次）%s 元 ≤ 日累计阈值 %s 元" % (yuan(cumulative), yuan(daily_limit)),
                                        value=cumulative, threshold=daily_limit))
                if args.get("payee_is_new"):
                    step_risk = "high"
                    reasons.append("收款人「%s」是首次转账，强制强验证" % args.get("payee_name"))
                    _add_hit(hits, _hit("payee_new", "收款人", "high",
                                        "收款人「%s」历史成功转账 0 笔（阈值 1 笔），首次转账强制动态验证码" % args.get("payee_name"),
                                        value=0, threshold=1))
                else:
                    known = _payee_executed_count(conn, user_id, args.get("payee_name") or "")
                    _add_hit(hits, _hit("payee_known", "收款人", "low",
                                        "收款人「%s」已存在于通讯录，历史成功转账 %d 笔（阈值 1 笔）" % (args.get("payee_name") or "", known),
                                        value=known, threshold=1))
                _add_hit(hits, _hit("irreversible", "可逆性", "medium",
                                    "对外转账执行后不可撤回，仅保留 %d 秒撤销窗口" % config.UNDO_WINDOW_SECONDS,
                                    value=config.UNDO_WINDOW_SECONDS, threshold=0))
            recent = _count_recent_transfers(conn, user_id)
            if recent >= FREQUENCY_HIGH_COUNT:
                _add_hit(hits, _hit("frequency_5_in_10min", "频次", "medium",
                                    "近 %d 分钟已转账 %d 笔（预警阈值 %d 笔），已超过高频上限 %d 笔，建议人工复核" % (
                                        FREQUENCY_WINDOW_MINUTES, recent, FREQUENCY_WARN_COUNT, FREQUENCY_HIGH_COUNT),
                                    value=recent, threshold=FREQUENCY_WARN_COUNT))
            elif recent >= FREQUENCY_WARN_COUNT:
                _add_hit(hits, _hit("frequency_5_in_10min", "频次", "medium",
                                    "近 %d 分钟已转账 %d 笔（阈值 %d 笔），触发高频预警" % (
                                        FREQUENCY_WINDOW_MINUTES, recent, FREQUENCY_WARN_COUNT),
                                    value=recent, threshold=FREQUENCY_WARN_COUNT))
            else:
                _add_hit(hits, _hit("frequency_5_in_10min", "频次", "low",
                                    "近 %d 分钟已转账 %d 笔，未达 %d 笔预警阈值" % (
                                        FREQUENCY_WINDOW_MINUTES, recent, FREQUENCY_WARN_COUNT),
                                    value=recent, threshold=FREQUENCY_WARN_COUNT))
            counterparty = args.get("payee_name") or ""
            action_summary = "向「%s」划转 %s" % (counterparty, yuan(amount))
        elif tool == "purchase_wealth":
            step_risk = "high"
            amount = int(args.get("amount_cents") or 0)
            amount_total += amount
            reasons.append("理财申购会划出资金，属于高风险操作")
            _add_hit(hits, _hit("irreversible", "可逆性", "high",
                                "理财申购 %s 元确认后不可撤回，赎回需 T+1 到账" % yuan(amount),
                                value=amount, threshold=0))
            counterparty = args.get("product_name") or "理财产品"
            action_summary = "申购「%s」%s" % (counterparty, yuan(amount))
            info = mock_core.list_wealth(conn, user_id)
            pid = args.get("product_id")
            prod = next((p for p in info["products"] if p["product_id"] == pid), None)
            if not prod:
                blocked = blocked or {"code": "PRODUCT_NOT_FOUND", "message": "没有找到该理财产品"}
                _add_hit(hits, _hit("product_not_found", "产品", "high",
                                    "理财产品编号 %s 不在在售列表中（在售 %d 只）" % (pid or "-", len(info["products"])),
                                    value=pid, threshold=None))
                continue
            if amount < int(prod["min_amount_cents"]):
                blocked = blocked or {"code": "BELOW_MIN_AMOUNT", "message": "起购金额为 %s" % yuan(int(prod["min_amount_cents"]))}
                _add_hit(hits, _hit("below_min_amount", "金额（起购）", "high",
                                    "申购金额 %s 元 < 产品起购金额 %s 元" % (yuan(amount), yuan(int(prod["min_amount_cents"]))),
                                    value=amount, threshold=int(prod["min_amount_cents"])))
                continue
            if amount > 0:
                acc = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
                if acc and int(acc["balance_cents"]) < amount:
                    blocked = blocked or {"code": "INSUFFICIENT_FUNDS", "message": "工资卡余额不足，当前 %s" % yuan(int(acc["balance_cents"]))}
                    _add_hit(hits, _hit("insufficient_funds", "金额（余额）", "high",
                                        "工资卡可用 %s 元 < 申购金额 %s 元" % (yuan(int(acc["balance_cents"])), yuan(amount)),
                                        value=amount, threshold=int(acc["balance_cents"])))
                    continue
        elif tool == "repay_credit":
            step_risk = "medium"
            amount_total += int(args.get("amount_cents") or 0)
            counterparty = "信用卡还款"
            action_summary = "归还信用卡欠款 %s" % yuan(int(args.get("amount_cents") or 0))
            _add_hit(hits, _hit("irreversible", "可逆性", "medium",
                                "信用卡还款 %s 元到账后不可撤销" % yuan(int(args.get("amount_cents") or 0)),
                                value=int(args.get("amount_cents") or 0), threshold=0))
        elif tool in ("cancel_subscription", "apply_virtual_card"):
            step_risk = "medium"
            if tool == "cancel_subscription":
                action_summary = "取消订阅「%s」" % (args.get("merchant") or args.get("sub_id") or "")
                _add_hit(hits, _hit("reversed_window", "可逆性", "low",
                                    "取消订阅「%s」后可随时重新订阅，属可逆操作" % (args.get("merchant") or args.get("sub_id") or ""),
                                    value=config.UNDO_WINDOW_SECONDS, threshold=60))
            elif tool == "apply_virtual_card":
                action_summary = "申请一张虚拟卡，单日限额 %s" % yuan(int(args.get("daily_limit_cents") or 200000))
                _add_hit(hits, _hit("reversed_window", "可逆性", "low",
                                    "虚拟卡单日限额 %s 元，可随时冻结或删除" % yuan(int(args.get("daily_limit_cents") or 200000)),
                                    value=int(args.get("daily_limit_cents") or 200000), threshold=0))
        elif tool == "set_card_status":
            # v0.4.1 官方红线：卡片挂失/解挂属红色强验证，必须多因子确认
            step_risk = "high"
            status_text = "挂失（冻结）" if args.get("status") == "frozen" else "解挂（解冻）"
            card_last4 = args.get("card_last4", "")
            action_summary = "%s银行卡 %s" % (status_text, card_last4)
            _add_hit(hits, _hit("card_status_change", "动作类型", "high",
                                "银行卡 %s 的%s属于红色强验证操作（挂失/解挂不可由 Agent 自动完成），需动态码/短信/人脸多因子确认" % (
                                    card_last4 or "****", status_text),
                                value="frozen" if args.get("status") == "frozen" else "active", threshold=None))
        elif tool == "redeem_wealth":
            step_risk = "medium"
            amount = int(args.get("amount_cents") or 0)
            amount_total += amount
            counterparty = args.get("product_name") or args.get("product_id") or "理财产品"
            action_summary = "赎回「%s」%s" % (counterparty, yuan(amount))
            _add_hit(hits, _hit("reversed_window", "可逆性", "low",
                                "赎回 %s 元回到活期账户，%d 秒内可撤销，之后也可重新申购" % (yuan(amount), config.UNDO_WINDOW_SECONDS),
                                value=amount, threshold=0))
        elif tool == "set_card_limit":
            step_risk = "medium"
            d = args.get("daily_limit_cents")
            s = args.get("single_limit_cents")
            parts = []
            if d is not None:
                parts.append("日累计限额改为 %s 元" % yuan(int(d)))
            if s is not None:
                parts.append("单笔限额改为 %s 元" % yuan(int(s)))
            action_summary = "调整银行卡限额：%s" % ("、".join(parts) or "未指定新额度")
            bad_limit = None
            if d is not None and int(d) <= 0:
                bad_limit = ("日累计", int(d))
            elif s is not None and int(s) <= 0:
                bad_limit = ("单笔", int(s))
            if bad_limit is not None:
                blocked = blocked or {"code": "LIMIT_INVALID",
                                      "message": "限额必须大于 0 元，当前提交的%s限额为 %.2f 元" % (bad_limit[0], bad_limit[1] / 100.0)}
                _add_hit(hits, _hit("card_limit_invalid", "额度", "high",
                                    "限额必须大于 0 元，当前提交的%s限额为 %.2f 元，未执行任何调整" % (bad_limit[0], bad_limit[1] / 100.0),
                                    value=bad_limit[1], threshold=0))
                continue
            _add_hit(hits, _hit("card_limit_change", "额度", "medium",
                                "银行卡 %s %s，调整后立即生效，可随时再改回" % (args.get("card_last4") or "", "、".join(parts) or "限额未变化"),
                                value=(int(d) if d is not None else 0), threshold=config.SINGLE_LIMIT_CENTS))
        elif tool == "card_spend":
            amount = int(args.get("amount_cents") or 0)
            amount_total += amount
            counterparty = args.get("merchant") or "商户消费"
            action_summary = "在「%s」刷卡消费 %s" % (counterparty, yuan(amount))
            if amount <= 0:
                blocked = blocked or {"code": "AMOUNT_INVALID", "message": "消费金额不合法"}
                _add_hit(hits, _hit("amount_invalid", "金额", "high",
                                    "刷卡金额必须大于 0 元，当前为 %.2f 元" % (amount / 100.0),
                                    value=amount, threshold=0))
                continue
            step_risk = "high" if amount > config.SINGLE_LIMIT_CENTS else "medium"
            _add_hit(hits, _hit("irreversible", "可逆性", "medium" if step_risk == "medium" else "high",
                                "刷卡消费 %s 元商户已收款，事后只能申请退款不能撤销" % yuan(amount),
                                value=amount, threshold=0))
            card_id = args.get("card_id")
            card = None
            if card_id:
                card = conn.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (card_id, user_id)).fetchone()
            if card is None:
                card = conn.execute("SELECT * FROM cards WHERE user_id=? AND status='active' ORDER BY card_id LIMIT 1",
                                    (user_id,)).fetchone()
            if card is not None:
                single = int(card["single_limit_cents"] or 0)
                daily = int(card["daily_limit_cents"] or 0)
                if single and amount > single:
                    blocked = blocked or {"code": "CARD_SINGLE_LIMIT",
                                          "message": "单笔 %s 元超过该卡单笔限额 %s 元" % (yuan(amount), yuan(single))}
                    _add_hit(hits, _hit("card_limit_exceeded", "额度（单笔）", "high",
                                        "单笔 %s 元 > 该卡单笔限额 %s 元，已拦截" % (yuan(amount), yuan(single)),
                                        value=amount, threshold=single))
                    continue
                since = datetime.now().strftime("%Y-%m-%d") + "T00:00:00"
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS s FROM card_txns WHERE card_id=? AND status='success' AND ts>=?",
                    (card["card_id"], since)).fetchone()
                used = int(row["s"] or 0)
                if daily and used + amount > daily:
                    blocked = blocked or {"code": "CARD_DAILY_LIMIT",
                                          "message": "今日已用 %s 元，本次 %s 元将超过日限额 %s 元" % (yuan(used), yuan(amount), yuan(daily))}
                    _add_hit(hits, _hit("card_limit_exceeded", "额度（日累计）", "high",
                                        "今日已用 %s 元 + 本次 %s 元 > 日限额 %s 元，已拦截" % (yuan(used), yuan(amount), yuan(daily)),
                                        value=used + amount, threshold=daily))
                    continue
        elif tool == "create_scheduled_transfer":
            amount = int(args.get("amount_cents") or 0)
            amount_total += amount
            counterparty = args.get("payee_name") or ""
            action_summary = "预约在 %s 向「%s」转 %s" % (args.get("run_at") or "", counterparty, yuan(amount))
            step_risk = "high" if amount > config.SINGLE_LIMIT_CENTS else "medium"
            if amount <= 0:
                blocked = blocked or {"code": "AMOUNT_INVALID", "message": "预约转账金额不合法"}
                _add_hit(hits, _hit("amount_invalid", "金额", "high",
                                    "预约转账金额必须大于 0 元，当前为 %.2f 元" % (amount / 100.0),
                                    value=amount, threshold=0))
                continue
            if amount > config.HARD_SINGLE_LIMIT_CENTS:
                blocked = blocked or {"code": "LIMIT_EXCEEDED", "message": "单笔金额超过演示系统上限 5 万元，已拦截"}
                _add_hit(hits, _hit("amount_hard_limit", "金额（单笔）", "high",
                                    "预约单笔 %s 元 超过演示系统硬上限 %s 元" % (yuan(amount), yuan(config.HARD_SINGLE_LIMIT_CENTS)),
                                    value=amount, threshold=config.HARD_SINGLE_LIMIT_CENTS))
                continue
            _add_hit(hits, _hit("scheduled_transfer_created", "执行方式", "low",
                                "创建时确认一次，到期（%s）由调度器自动执行，届时不再打扰你" % (args.get("run_at") or "-"),
                                value=amount, threshold=config.SINGLE_LIMIT_CENTS))
            _add_hit(hits, _hit("amount_single_limit", "金额（单笔）", "high" if step_risk == "high" else "medium",
                                "预约单笔 %s 元 %s 小额阈值 %s 元，按%s风险处理"
                                % (yuan(amount), ">" if step_risk == "high" else "≤", yuan(config.SINGLE_LIMIT_CENTS),
                                   "高" if step_risk == "high" else "中"),
                                value=amount, threshold=config.SINGLE_LIMIT_CENTS))
        elif tool == "cancel_scheduled_transfer":
            step_risk = "medium"
            action_summary = "取消预约转账 %s" % (args.get("sched_id") or "")
            _add_hit(hits, _hit("reversed_window", "可逆性", "low",
                                "取消后这笔预约不会执行，资金不会变动", value=0, threshold=0))
        elif tool in LOW_TOOLS:
            step_risk = "low"
        else:
            step_risk = "medium"
        risk = max_risk(risk, step_risk)

    if blocked:
        return {"risk_level": "blocked", "challenge": None, "reasons": reasons, "hits": hits, "blocked": blocked,
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
    return {"risk_level": risk, "challenge": challenge, "reasons": reasons, "hits": hits, "blocked": None,
            "amount_cents": amount_total, "counterparty": counterparty,
            "summary": action_summary, "requires_approval": risk in ("medium", "high")}
