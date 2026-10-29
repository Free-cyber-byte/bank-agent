"""只读业务聚合层（v0.8.0）。

「业务」页一次取回七个业务域的快照：账单 / 订阅 / 转账 / 理财 / 卡片 / 消息 / 安全。

约束与 analytics.py 一致：

- 只读。不写 transactions、events、audit，也不触发任何工具调用；
- 口径复用 mock_core 与 audit 的既有实现，不另造一套账；
- 参数或会话非法抛 BizError，由 server 统一转成 400。

消息（主动提醒）也在这里聚合：待审批、信用卡还款日、订阅扣费、预算超支、
异常消费、预约转账，统一成带优先级的条目，前端只需渲染。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

from . import audit, config, locks, mock_core

BILL_MONTHS = 6
ANOMALY_DAYS = 30
DUE_SOON_DAYS = 7
RECENT_TXN_LIMIT = 12
AUDIT_TAIL = 8
RISK_ORDER = ("low", "medium", "high")
RISK_LABEL = {"low": "绿", "medium": "黄", "high": "红"}
RISK_TEXT = {"low": "只读", "medium": "确认", "high": "强验证"}


class BizError(ValueError):
    """参数或会话非法（server 转 400 BAD_REQUEST）。"""


# ---------------------------------------------------------------- 小工具


def _yuan(cents: Any) -> str:
    return mock_core.yuan(int(cents or 0))


def _today() -> dt.date:
    return dt.date.today()


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _parse_day(value: Any) -> Optional[dt.date]:
    text = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _month_key(day: dt.date) -> str:
    return day.strftime("%Y-%m")


def _shift_month(day: dt.date, delta: int) -> dt.date:
    month = day.month - 1 + delta
    year = day.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, 1)


def _days_left(value: Any) -> Optional[int]:
    day = _parse_day(value)
    if day is None:
        return None
    return (day - _today()).days


def user_of(conn, user_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        raise BizError("没有这个用户：%s" % user_id)
    return dict(row)


def _accounts(conn, user_id: str) -> List[Dict[str, Any]]:
    return mock_core.list_accounts(conn, user_id)


def _credit_debt(conn, user_id: str) -> Dict[str, Any]:
    for acc in _accounts(conn, user_id):
        if acc.get("kind") == "credit":
            bal = int(acc.get("balance_cents") or 0)
            debt = -bal if bal < 0 else 0
            return {
                "account_id": acc.get("account_id"),
                "name": acc.get("name") or "信用卡",
                "number_masked": acc.get("number_masked") or "",
                "debt_cents": debt,
                "debt_display": _yuan(debt),
                "limit_cents": int(acc.get("credit_limit_cents") or 0),
                "limit_display": _yuan(int(acc.get("credit_limit_cents") or 0)),
            }
    return {}


def _pending_approvals(conn, user_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.approval_id, a.action, a.risk_level, a.created_at, a.params_json "
        "FROM approvals a JOIN sessions s ON a.session_id = s.session_id "
        "WHERE s.user_id=? AND a.status='pending' ORDER BY a.created_at DESC LIMIT 6",
        (user_id,),
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        amount = 0
        try:
            params = json.loads(row["params_json"] or "{}")
            args = params.get("steps") or params.get("args") or {}
            if isinstance(args, dict):
                amount = int(args.get("amount_cents") or 0)
        except (TypeError, ValueError):
            params = {}
        items.append({
            "approval_id": row["approval_id"],
            "action": row["action"],
            "risk_level": row["risk_level"],
            "created_at": row["created_at"],
            "amount_cents": amount,
            "amount_display": _yuan(amount) if amount else "",
        })
    return items


def _subscriptions_due(conn, user_id: str, within_days: int = DUE_SOON_DAYS) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in conn.execute(
        "SELECT sub_id, merchant, amount_cents, cycle, next_charge, status FROM subscriptions "
        "WHERE user_id=? AND status='active' ORDER BY next_charge",
        (user_id,),
    ).fetchall():
        days = _days_left(row["next_charge"])
        if days is None or days > within_days:
            continue
        items.append({
            "sub_id": row["sub_id"],
            "name": row["merchant"],
            "cycle": row["cycle"],
            "amount_cents": int(row["amount_cents"]),
            "amount_display": _yuan(row["amount_cents"]),
            "next_charge": str(row["next_charge"])[:10],
            "days_left": days,
        })
    return items


def _chain(conn) -> Dict[str, Any]:
    result = audit.verify(conn)
    return {"valid": bool(result.get("valid")), "checked": int(result.get("checked") or 0)}


def _today_audit(conn) -> List[Dict[str, Any]]:
    today = _today().isoformat()
    rows = conn.execute(
        "SELECT seq, ts, event_type, tool, risk_level FROM audit ORDER BY seq DESC LIMIT 200"
    ).fetchall()
    return [dict(r) for r in rows if str(r["ts"] or "").startswith(today)]


# ---------------------------------------------------------------- 账单


def _monthly_income(conn, user_id: str, months: int) -> Dict[str, int]:
    """按月份汇总所有入账流水（工资 / 转入 / 退款）。

    mock_core.bill_summary 的 monthly_trend 只有支出，这里补齐收入侧，
    否则月度净额会恒为负数。
    """
    start = _month_key(_shift_month(_today(), -(months - 1)))
    rows = conn.execute(
        "SELECT substr(ts, 1, 7) AS m, SUM(amount_cents) AS cents "
        "FROM transactions WHERE user_id = ? AND direction = 'in' AND substr(ts, 1, 7) >= ? "
        "GROUP BY m",
        (user_id, start),
    ).fetchall()
    return {r["m"]: int(r["cents"] or 0) for r in rows}


def bills(conn, user_id: str) -> Dict[str, Any]:
    summary = mock_core.bill_summary(conn, user_id, months=BILL_MONTHS)
    budget = mock_core.budget_analysis(conn, user_id)
    raw_budgets = budget.get("budgets") or {}
    limits = raw_budgets if isinstance(raw_budgets, dict) else {}
    combined = list(budget.get("over_budget") or []) + list(budget.get("within_budget") or [])
    budgets = []
    for row in combined:
        limit_cents = int(row.get("budget_cents") or 0)
        used = int(row.get("spent_cents") or 0)
        pct = int(round(used * 100.0 / limit_cents)) if limit_cents else 0
        budgets.append({
            "category": row.get("category"),
            "limit_cents": limit_cents,
            "limit_display": _yuan(limit_cents),
            "used_cents": used,
            "used_display": _yuan(used),
            "pct": pct,
            "state": "over" if pct > 100 else ("warn" if pct >= 80 else "ok"),
        })
    if not combined:
        for category, limit_cents in (limits or {}).items():
            budgets.append({
                "category": category,
                "limit_cents": int(limit_cents),
                "limit_display": _yuan(limit_cents),
                "used_cents": 0,
                "used_display": _yuan(0),
                "pct": 0,
                "state": "ok",
            })
    budgets.sort(key=lambda r: -r["pct"])
    income_by_month = _monthly_income(conn, user_id, BILL_MONTHS)
    trend = []
    for row in summary.get("monthly_trend") or []:
        out_cents = int(row.get("out_cents") or 0)
        in_cents = int(income_by_month.get(row.get("month"), 0))
        trend.append({
            "month": row.get("month"),
            "in_cents": in_cents,
            "in_display": _yuan(in_cents),
            "out_cents": out_cents,
            "out_display": _yuan(out_cents),
            "net_cents": in_cents - out_cents,
        })
    return {
        "months": BILL_MONTHS,
        "total_out_cents": int(summary.get("total_out_cents") or 0),
        "total_out_display": _yuan(summary.get("total_out_cents")),
        "total_in_cents": int(summary.get("total_in_cents") or 0),
        "total_in_display": _yuan(summary.get("total_in_cents")),
        "net_cents": int(summary.get("net_cents") or 0),
        "txn_count": int(summary.get("txn_count") or 0),
        "savings_rate": budget.get("savings_rate"),
        "by_category": [
            {
                "category": row.get("category"),
                "cents": int(row.get("amount_cents") or 0),
                "display": _yuan(row.get("amount_cents")),
                "ratio": row.get("ratio"),
            }
            for row in (summary.get("by_category") or [])
        ],
        "top_merchants": [
            {
                "merchant": row.get("merchant") or row.get("counterparty"),
                "cents": int(row.get("amount_cents") or row.get("cents") or 0),
                "display": _yuan(row.get("amount_cents") or row.get("cents")),
                "count": int(row.get("count") or 0),
            }
            for row in (summary.get("top_merchants") or [])[:8]
        ],
        "budgets": budgets,
        "over_budget": list(budget.get("over_budget") or []),
        "within_budget": list(budget.get("within_budget") or []),
        "trend": trend,
    }


# ---------------------------------------------------------------- 订阅


def subscriptions(conn, user_id: str) -> Dict[str, Any]:
    data = mock_core.list_subscriptions(conn, user_id)
    items = []
    for row in data.get("subscriptions") or []:
        days = _days_left(row.get("next_charge"))
        items.append({
            "sub_id": row.get("sub_id"),
            "name": row.get("merchant"),
            "amount_cents": int(row.get("amount_cents") or 0),
            "amount_display": _yuan(row.get("amount_cents")),
            "cycle": row.get("cycle"),
            "next_charge": str(row.get("next_charge") or "")[:10],
            "days_left": days,
            "status": row.get("status"),
        })
    items.sort(key=lambda r: (r["status"] != "active", r["days_left"] if r["days_left"] is not None else 999))
    return {
        "items": items,
        "active_count": int(data.get("active_count") or 0),
        "monthly_total_cents": int(data.get("monthly_total_cents") or 0),
        "monthly_total_display": data.get("monthly_total_display") or _yuan(data.get("monthly_total_cents")),
        "yearly_total_display": _yuan(int(data.get("yearly_total_cents") or 0)),
        "due_soon": _subscriptions_due(conn, user_id),
    }


# ---------------------------------------------------------------- 转账


def transfers(conn, user_id: str) -> Dict[str, Any]:
    payees = mock_core.list_payees(conn, user_id)
    sched = mock_core.list_scheduled_transfers(conn, user_id)
    rows = conn.execute(
        "SELECT txn_id, ts, direction, amount_cents, counterparty, category, memo, account_id "
        "FROM transactions WHERE user_id=? AND category IN ('转账','还款') ORDER BY ts DESC LIMIT ?",
        (user_id, RECENT_TXN_LIMIT),
    ).fetchall()
    recent = [{
        "txn_id": r["txn_id"],
        "ts": r["ts"],
        "direction": r["direction"],
        "amount_cents": int(r["amount_cents"]),
        "amount_display": _yuan(r["amount_cents"]),
        "counterparty": r["counterparty"],
        "category": r["category"],
        "memo": r["memo"],
        "account_id": r["account_id"],
    } for r in rows]
    today_total = int(mock_core.today_transfer_total(conn, user_id))
    return {
        "payees": [{
            "payee_id": p.get("payee_id"),
            "name": p.get("name"),
            "bank": p.get("bank"),
            "account_no": p.get("account_no"),
            "phone": p.get("phone"),
        } for p in payees],
        "recent": recent,
        "scheduled": list(sched.get("items") or []),
        "scheduled_pending": int(sched.get("pending_count") or 0),
        "today_total_cents": today_total,
        "today_total_display": _yuan(today_total),
        "daily_limit_cents": int(config.DAILY_LIMIT_CENTS),
        "daily_limit_display": _yuan(config.DAILY_LIMIT_CENTS),
        "single_limit_cents": int(config.SINGLE_LIMIT_CENTS),
        "single_limit_display": _yuan(config.SINGLE_LIMIT_CENTS),
        "hard_limit_display": _yuan(config.HARD_SINGLE_LIMIT_CENTS),
        "daily_left_cents": max(0, int(config.DAILY_LIMIT_CENTS) - today_total),
        "daily_left_display": _yuan(max(0, int(config.DAILY_LIMIT_CENTS) - today_total)),
    }


# ---------------------------------------------------------------- 理财


def wealth(conn, user_id: str) -> Dict[str, Any]:
    data = mock_core.list_wealth(conn, user_id)
    holdings = []
    for row in data.get("holdings") or []:
        amount = int(row.get("amount_cents") or 0)
        rate = float(row.get("annual_rate") or 0)
        daily = int(round(amount * rate / 100.0 / 365.0))
        holdings.append({
            "holding_id": row.get("holding_id"),
            "product_id": row.get("product_id"),
            "name": row.get("name"),
            "risk_level": row.get("risk_level"),
            "annual_rate": rate,
            "amount_cents": amount,
            "amount_display": _yuan(amount),
            "daily_income_cents": daily,
            "daily_income_display": _yuan(daily),
            "status": row.get("status"),
        })
    holdings.sort(key=lambda r: -r["amount_cents"])
    products = []
    for row in data.get("products") or []:
        products.append({
            "product_id": row.get("product_id"),
            "name": row.get("name"),
            "risk_level": row.get("risk_level"),
            "annual_rate": float(row.get("annual_rate") or 0),
            "min_amount_cents": int(row.get("min_amount_cents") or 0),
            "min_amount_display": _yuan(row.get("min_amount_cents")),
            "term_days": int(row.get("term_days") or 0),
            "description": row.get("description"),
        })
    total = int(data.get("holding_total_cents") or 0)
    return {
        "holdings": holdings,
        "products": products,
        "holding_total_cents": total,
        "holding_total_display": _yuan(total),
        "daily_income_cents": sum(h["daily_income_cents"] for h in holdings),
        "daily_income_display": _yuan(sum(h["daily_income_cents"] for h in holdings)),
    }


# ---------------------------------------------------------------- 卡片


def cards(conn, user_id: str) -> Dict[str, Any]:
    card_rows = mock_core.list_cards(conn, user_id)
    accounts = _accounts(conn, user_id)
    by_tail: Dict[str, Dict[str, Any]] = {}
    for acc in accounts:
        tail = str(acc.get("number_masked") or "").replace("*", "").strip()
        if tail:
            by_tail[tail] = acc
    since = (_today() - dt.timedelta(days=30)).isoformat()
    spend: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT account_id, SUM(amount_cents) AS total, COUNT(*) AS n FROM transactions "
        "WHERE user_id=? AND direction='out' AND substr(ts,1,10) >= ? GROUP BY account_id",
        (user_id, since),
    ).fetchall():
        spend[row["account_id"]] = {"total": int(row["total"] or 0), "count": int(row["n"] or 0)}
    items = []
    for card in card_rows:
        tail = str(card.get("number_masked") or "").replace("*", "").strip()
        acc = by_tail.get(tail) or {}
        acc_id = acc.get("account_id") or ""
        used = spend.get(acc_id) or {"total": 0, "count": 0}
        items.append({
            "card_id": card.get("card_id"),
            "kind": card.get("kind"),
            "number_masked": card.get("number_masked"),
            "status": card.get("status"),
            "daily_limit_cents": int(card.get("daily_limit_cents") or 0),
            "daily_limit_display": _yuan(card.get("daily_limit_cents")),
            "single_limit_cents": int(card.get("single_limit_cents") or 0),
            "single_limit_display": _yuan(card.get("single_limit_cents")),
            "account_id": acc_id,
            "account_name": acc.get("name") or "",
            "balance_cents": int(acc.get("balance_cents") or 0),
            "balance_display": acc.get("balance_display") or _yuan(acc.get("balance_cents")),
            "credit_limit_cents": int(acc.get("credit_limit_cents") or 0),
            "spend_30d_cents": used["total"],
            "spend_30d_display": _yuan(used["total"]),
            "spend_30d_count": used["count"],
        })
    return {
        "items": items,
        "active_count": len([c for c in items if c["status"] == "active"]),
        "frozen_count": len([c for c in items if c["status"] == "frozen"]),
    }


# ---------------------------------------------------------------- 消息（主动提醒）


def messages(conn, user_id: str) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for row in _pending_approvals(conn, user_id):
        items.append({
            "id": "approval-" + row["approval_id"],
            "priority": "P1",
            "kind": "approval",
            "title": "有一笔操作等你确认",
            "desc": "%s · 风险 %s" % (row["action"], row["risk_level"]),
            "amount_display": row["amount_display"],
            "target": "",
            "action": "去确认",
        })
    debt = _credit_debt(conn, user_id)
    if debt and debt.get("debt_cents"):
        due_day = 25
        today = _today()
        try:
            due = today.replace(day=due_day)
        except ValueError:
            due = today
        if due < today:
            due = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=due_day)
        items.append({
            "id": "credit-due",
            "priority": "P1",
            "kind": "repayment",
            "title": "信用卡待还 %s，还款日 %s（%d 天后）" % (debt["debt_display"], due.isoformat(), (due - today).days),
            "desc": "到期前还清不计息；可指定工资卡或储蓄卡出账",
            "amount_display": debt["debt_display"],
            "target": "还信用卡 %s 元" % debt["debt_display"].replace("¥", "").replace(",", ""),
            "action": "去还款",
        })
    anomalies = mock_core.scan_anomalies(conn, user_id, days=ANOMALY_DAYS)
    anomaly_rows = list(anomalies.get("anomalies") or [])
    if anomaly_rows:
        first = anomaly_rows[0]
        items.append({
            "id": "anomaly-1",
            "priority": "P1" if len(anomaly_rows) >= 2 else "P2",
            "kind": "anomaly",
            "title": "发现 %d 笔异常消费" % len(anomaly_rows),
            "desc": "%s · %s" % (first.get("counterparty") or "—", first.get("detail") or ""),
            "amount_display": first.get("amount_display") or "",
            "target": "看看最近有没有异常消费",
            "action": "看异常",
        })
    for sub in _subscriptions_due(conn, user_id):
        items.append({
            "id": "sub-" + str(sub["sub_id"]),
            "priority": "P2",
            "kind": "subscription",
            "title": "%s %d 天后扣费" % (sub["name"], sub["days_left"]),
            "desc": "自动续费 %s · 下次 %s" % (sub["amount_display"], sub["next_charge"]),
            "amount_display": sub["amount_display"],
            "target": "看看我的订阅",
            "action": "看订阅",
        })
    budget = mock_core.budget_analysis(conn, user_id)
    rows = list(budget.get("over_budget") or []) + list(budget.get("within_budget") or [])
    for row in rows:
        limit_cents = int(row.get("budget_cents") or 0)
        used = int(row.get("spent_cents") or 0)
        if not limit_cents:
            continue
        pct = int(round(used * 100.0 / limit_cents))
        if pct < 80:
            continue
        items.append({
            "id": "budget-" + str(row.get("category")),
            "priority": "P2",
            "kind": "budget",
            "title": "%s 预算已用 %d%%" % (row.get("category"), pct),
            "desc": "已用 %s / 预算 %s" % (_yuan(used), _yuan(limit_cents)),
            "amount_display": _yuan(used),
            "target": "看看预算进度",
            "action": "看预算",
        })
    sched = mock_core.list_scheduled_transfers(conn, user_id, status="pending")
    for row in (sched.get("items") or [])[:3]:
        items.append({
            "id": "sched-" + str(row.get("sched_id")),
            "priority": "P2",
            "kind": "scheduled",
            "title": "预约转账待执行：%s" % (row.get("payee_name") or ""),
            "desc": "%s · %s" % (row.get("amount_display"), str(row.get("run_at") or "")[:16].replace("T", " ")),
            "amount_display": row.get("amount_display"),
            "target": "看看我的预约转账",
            "action": "看预约",
        })
    order = {"P1": 0, "P2": 1}
    items.sort(key=lambda r: (order.get(r["priority"], 9), r["kind"]))
    p1 = len([i for i in items if i["priority"] == "P1"])
    return {
        "items": items,
        "p1_count": p1,
        "p2_count": len(items) - p1,
        "pending_approvals": len(_pending_approvals(conn, user_id)),
        "subscriptions_due": len(_subscriptions_due(conn, user_id)),
        "anomaly_count": len(anomaly_rows),
    }


# ---------------------------------------------------------------- 安全


def security(conn, user_id: str, session_id: str = "") -> Dict[str, Any]:
    today_rows = _today_audit(conn)
    counts = {level: 0 for level in RISK_ORDER}
    for row in today_rows:
        level = str(row.get("risk_level") or "low")
        counts[level] = counts.get(level, 0) + 1
    total_transfer = int(mock_core.today_transfer_total(conn, user_id))
    daily_limit = int(config.DAILY_LIMIT_CENTS)
    lock_info: Dict[str, Any] = {}
    try:
        lock_info = locks.status(conn, session_id) if session_id else {}
    except Exception:  # 锁定模块缺表时不影响展示
        lock_info = {}
    tail = []
    for row in conn.execute(
        "SELECT seq, ts, event_type, tool, risk_level FROM audit ORDER BY seq DESC LIMIT ?", (AUDIT_TAIL,)
    ).fetchall():
        tail.append({
            "seq": row["seq"],
            "ts": row["ts"],
            "event": row["event_type"],
            "tool": row["tool"],
            "risk_level": row["risk_level"],
        })
    return {
        "chain": _chain(conn),
        "risk": {
            "levels": [
                {
                    "level": level,
                    "label": RISK_LABEL[level],
                    "text": RISK_TEXT[level],
                    "count": counts.get(level, 0),
                }
                for level in RISK_ORDER
            ],
            "today_total": len(today_rows),
        },
        "transfer": {
            "today_total_cents": total_transfer,
            "today_total_display": _yuan(total_transfer),
            "daily_limit_cents": daily_limit,
            "daily_limit_display": _yuan(daily_limit),
            "left_cents": max(0, daily_limit - total_transfer),
            "left_display": _yuan(max(0, daily_limit - total_transfer)),
            "pct": int(round(total_transfer * 100.0 / daily_limit)) if daily_limit else 0,
        },
        "factors": [
            {"key": "totp", "label": "TOTP 动态码", "enabled": True, "ttl": config.TOTP_WINDOW * 30},
            {"key": "sms", "label": "短信验证码", "enabled": True, "ttl": int(config.SMS_TTL_SECONDS)},
            {"key": "face", "label": "人脸确认", "enabled": True, "ttl": int(config.FACE_TTL_SECONDS)},
        ],
        "lock": {
            "locked": bool(lock_info.get("locked")),
            "reason": lock_info.get("reason") or "",
            "until": lock_info.get("until") or "",
            "events": int(lock_info.get("events") or 0),
        },
        "recent": tail,
    }


# ---------------------------------------------------------------- 汇总


def overview(conn, user_id: str, session_id: str = "") -> Dict[str, Any]:
    """业务页一次取回七个业务域。只读，不写任何表。"""
    user = user_of(conn, user_id)
    return {
        "as_of": _now_iso(),
        "user": {
            "user_id": user.get("user_id"),
            "name": user.get("name"),
            "phone": user.get("phone"),
            "profile": user.get("profile") or user.get("risk_profile") or "",
            "occupation": user.get("occupation") or "",
        },
        "bills": bills(conn, user_id),
        "subscriptions": subscriptions(conn, user_id),
        "transfers": transfers(conn, user_id),
        "wealth": wealth(conn, user_id),
        "cards": cards(conn, user_id),
        "messages": messages(conn, user_id),
        "security": security(conn, user_id, session_id),
    }
