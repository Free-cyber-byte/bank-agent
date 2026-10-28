"""模拟银行核心：账户、流水、转账、理财、卡片、订阅、提醒。

所有资金操作都在这里以确定性代码完成，LLM 只负责理解意图，不接触账本。
金额单位：分。
"""
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import config, db


class DomainError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def yuan(cents: int) -> str:
    return "¥{:,.2f}".format(cents / 100.0)


# ---------------- 账户与查询 ----------------

def get_account(conn, account_id: str, user_id: str):
    row = conn.execute("SELECT * FROM accounts WHERE account_id=? AND user_id=?", (account_id, user_id)).fetchone()
    if not row:
        raise DomainError("ACCOUNT_NOT_FOUND", "账户不存在或不属于当前用户")
    return row


def list_accounts(conn, user_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY kind, account_id", (user_id,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["balance_display"] = yuan(r["balance_cents"])
        out.append(item)
    return out


def account_total(conn, user_id: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(balance_cents),0) AS s FROM accounts WHERE user_id=? AND kind!='credit'", (user_id,)).fetchone()
    return int(row["s"])


def list_transactions(conn, user_id: str, account_id: Optional[str] = None, limit: int = 20,
                      category: Optional[str] = None, months: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM transactions WHERE user_id=?"
    args: List[Any] = [user_id]
    if account_id:
        sql += " AND account_id=?"
        args.append(account_id)
    if category:
        sql += " AND category=?"
        args.append(category)
    if months:
        cutoff = (datetime.now() - timedelta(days=30 * months)).isoformat(timespec="seconds")
        sql += " AND ts>=?"
        args.append(cutoff)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def bill_summary(conn, user_id: str, months: int = 1) -> Dict[str, Any]:
    months = max(1, min(int(months or 1), 12))
    cutoff = (datetime.now() - timedelta(days=30 * months)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT direction, amount_cents, category, counterparty, ts FROM transactions WHERE user_id=? AND ts>=?",
        (user_id, cutoff),
    ).fetchall()
    outflow = sum(r["amount_cents"] for r in rows if r["direction"] == "out")
    inflow = sum(r["amount_cents"] for r in rows if r["direction"] == "in")
    cats: Dict[str, int] = {}
    merchants: Dict[str, int] = {}
    monthly: Dict[str, int] = {}
    for r in rows:
        if r["direction"] != "out":
            continue
        cats[r["category"]] = cats.get(r["category"], 0) + r["amount_cents"]
        merchants[r["counterparty"]] = merchants.get(r["counterparty"], 0) + r["amount_cents"]
        mk = r["ts"][:7]
        monthly[mk] = monthly.get(mk, 0) + r["amount_cents"]
    by_category = sorted(
        [{"category": k, "amount_cents": v, "ratio": round(v / outflow, 4) if outflow else 0} for k, v in cats.items()],
        key=lambda x: -x["amount_cents"],
    )
    top_merchants = [{"merchant": k, "amount_cents": v} for k, v in sorted(merchants.items(), key=lambda kv: -kv[1])[:5]]
    trend = [{"month": k, "out_cents": v} for k, v in sorted(monthly.items())]
    return {
        "months": months, "total_out_cents": outflow, "total_in_cents": inflow,
        "net_cents": inflow - outflow, "by_category": by_category,
        "top_merchants": top_merchants, "monthly_trend": trend, "txn_count": len(rows),
        "total_out_display": yuan(outflow),
    }


def budget_analysis(conn, user_id: str) -> Dict[str, Any]:
    budgets = {r["category"]: r["monthly_cents"] for r in conn.execute("SELECT * FROM budgets WHERE user_id=?", (user_id,)).fetchall()}
    summary = bill_summary(conn, user_id, 1)
    over, within = [], []
    for c in summary["by_category"]:
        cap = budgets.get(c["category"])
        if cap is None:
            continue
        item = {"category": c["category"], "spent_cents": c["amount_cents"], "budget_cents": cap,
                "ratio": round(c["amount_cents"] / cap, 3), "remaining_cents": cap - c["amount_cents"]}
        (over if c["amount_cents"] > cap else within).append(item)
    over.sort(key=lambda x: -x["spent_cents"])
    suggestions = []
    if over:
        top = over[0]
        suggestions.append("本月「%s」已超预算 %s，建议下月压缩 20%%" % (top["category"], yuan(top["spent_cents"] - top["budget_cents"])))
    total_out = summary["total_out_cents"]
    monthly_income = 2500000
    savings_rate = (monthly_income - total_out) / monthly_income if monthly_income else 0
    if savings_rate < 0.2:
        suggestions.append("储蓄率 %.1f%%，低于 20%% 的健康线；建议发薪日先自动转出 10%% 到储蓄卡" % (savings_rate * 100))
    else:
        suggestions.append("本月储蓄率 %.1f%%，保持得不错" % (savings_rate * 100))
    sub_total = int(conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM subscriptions WHERE user_id=? AND status='active'", (user_id,)).fetchone()["s"])
    suggestions.append("订阅类支出每月合计 %s，取消不常用的会员可以立省" % yuan(sub_total))
    return {"budgets": budgets, "over_budget": over, "within_budget": within, "suggestions": suggestions,
            "total_out_cents": total_out, "total_out_display": yuan(total_out), "savings_rate": round(savings_rate, 4)}


# ---------------- 收款人 ----------------

def list_payees(conn, user_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM payees WHERE user_id=? ORDER BY name", (user_id,)).fetchall()]


def find_payee(conn, user_id: str, text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    rows = list_payees(conn, user_id)
    for r in rows:
        if r["name"] and (r["name"] in text or text in r["name"]):
            return r
    return None


def add_payee(conn, user_id: str, name: str, bank: str, account_no: str, phone: str = "") -> Dict[str, Any]:
    payee_id = "p_" + secrets.token_hex(4)
    conn.execute(
        "INSERT INTO payees (payee_id,user_id,name,bank,account_no,created_at,is_new,phone) VALUES (?,?,?,?,?,?,?,?)",
        (payee_id, user_id, name, bank, account_no, db.now_iso(), 1, str(phone or "")),
    )
    conn.commit()
    return {"payee_id": payee_id, "name": name, "bank": bank, "account_no": account_no,
            "phone": str(phone or ""), "is_new": 1}


# ---------------- 转账 ----------------

def _balance(conn, account_id: str) -> int:
    return int(conn.execute("SELECT balance_cents FROM accounts WHERE account_id=?", (account_id,)).fetchone()["balance_cents"])


def _record_txn(conn, account_id: str, user_id: str, direction: str, amount_cents: int, category: str,
                counterparty: str, memo: str, balance_after: int, source: str = "agent") -> str:
    txn_id = "T" + secrets.token_hex(6).upper()
    conn.execute(
        "INSERT INTO transactions (txn_id, account_id, user_id, ts, direction, amount_cents, category, counterparty, memo, balance_after_cents, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (txn_id, account_id, user_id, db.now_iso(), direction, amount_cents, category, counterparty, memo, balance_after, source),
    )
    return txn_id


def today_transfer_total(conn, user_id: str) -> int:
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS s FROM transfers WHERE user_id=? AND status='executed' AND created_at>=?",
        (user_id, since),
    ).fetchone()
    return int(row["s"])


def create_transfer(conn, user_id: str, session_id: str, thread_id: str, from_account_id: str,
                    payee_name: str, payee_account: str, amount_cents: int, memo: str = "",
                    idempotency_key: str = "", internal: bool = False) -> Dict[str, Any]:
    if amount_cents <= 0:
        raise DomainError("AMOUNT_INVALID", "转账金额必须大于 0")
    if amount_cents > config.HARD_SINGLE_LIMIT_CENTS:
        raise DomainError("LIMIT_EXCEEDED", "单笔金额超过演示系统上限 5 万元")
    acc = get_account(conn, from_account_id, user_id)
    before = int(acc["balance_cents"])
    available = before if acc["kind"] != "credit" else int(acc["credit_limit_cents"]) + before
    if amount_cents > available:
        raise DomainError("INSUFFICIENT_FUNDS", "账户可用余额不足，当前可用 %s" % yuan(available),
                          {"available_cents": available})
    key = idempotency_key or ("idem_" + secrets.token_hex(8))
    exist = conn.execute("SELECT * FROM transfers WHERE idempotency_key=?", (key,)).fetchone()
    if exist:
        d = dict(exist)
        d.update({"deduplicated": True, "action_id": d["transfer_id"]})
        return d
    after = before - amount_cents
    conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (after, from_account_id))
    if internal:
        to = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='savings' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
        if to:
            conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (int(to["balance_cents"]) + amount_cents, to["account_id"]))
            _record_txn(conn, to["account_id"], user_id, "in", amount_cents, "转账", "本人储蓄卡", memo or "内部转账", int(to["balance_cents"]) + amount_cents)
    transfer_id = "tr_" + secrets.token_hex(6)
    created = db.now_iso()
    conn.execute(
        "INSERT INTO transfers (transfer_id, session_id, thread_id, user_id, from_account_id, payee_name, payee_account, amount_cents, status, memo, idempotency_key, balance_before_cents, balance_after_cents, created_at, undone_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (transfer_id, session_id, thread_id, user_id, from_account_id, payee_name, payee_account, amount_cents,
         "executed", memo, key, before, after, created, None),
    )
    txn_id = _record_txn(conn, from_account_id, user_id, "out", amount_cents, "转账", payee_name, memo, after)
    conn.commit()
    return {
        "transfer_id": transfer_id, "action_id": transfer_id, "txn_id": txn_id, "amount_cents": amount_cents,
        "amount_display": yuan(amount_cents), "payee_name": payee_name, "payee_account": payee_account,
        "from_account_id": from_account_id, "balance_before_cents": before, "balance_after_cents": after,
        "balance_after_display": yuan(after), "status": "executed", "created_at": created,
        "undo_deadline": (datetime.now() + timedelta(seconds=config.UNDO_WINDOW_SECONDS)).isoformat(timespec="seconds"),
        "internal": internal,
    }


def undo_transfer(conn, user_id: str, action_id: str) -> Dict[str, Any]:
    t = conn.execute("SELECT * FROM transfers WHERE transfer_id=? AND user_id=?", (action_id, user_id)).fetchone()
    if not t:
        raise DomainError("ACTION_NOT_FOUND", "没有找到可撤销的操作")
    t = dict(t)
    if t["status"] != "executed":
        raise DomainError("ACTION_NOT_UNDOABLE", "该操作当前状态不可撤销")
    created = datetime.fromisoformat(t["created_at"])
    if datetime.now().astimezone() - created > timedelta(seconds=config.UNDO_WINDOW_SECONDS):
        raise DomainError("UNDO_EXPIRED", "已超过 %d 秒撤销窗口" % config.UNDO_WINDOW_SECONDS)
    conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (int(t["balance_before_cents"]), t["from_account_id"]))
    to = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='savings' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
    if t["payee_name"] == "储蓄卡" and to:
        conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (int(to["balance_cents"]) - int(t["amount_cents"]), to["account_id"]))
    _record_txn(conn, t["from_account_id"], user_id, "in", int(t["amount_cents"]), "撤销", t["payee_name"], "转账撤销冲正", int(t["balance_before_cents"]))
    conn.execute("UPDATE transfers SET status='undone', undone_at=? WHERE transfer_id=?", (db.now_iso(), t["transfer_id"]))
    conn.commit()
    return {"undone": True, "action_id": t["transfer_id"], "amount_cents": t["amount_cents"],
            "message": "已撤销，资金已回到原账户", "balance_cents": int(t["balance_before_cents"])}


# ---------------- 理财 ----------------

def list_wealth(conn, user_id: str) -> Dict[str, Any]:
    products = [dict(r) for r in conn.execute("SELECT * FROM wealth_products ORDER BY risk_level").fetchall()]
    holdings = [dict(r) for r in conn.execute(
        "SELECT h.*, p.name, p.risk_level, p.annual_rate FROM wealth_holdings h JOIN wealth_products p ON h.product_id=p.product_id WHERE h.user_id=? AND h.status='holding'",
        (user_id,)).fetchall()]
    total = sum(h["amount_cents"] for h in holdings)
    return {"products": products, "holdings": holdings, "holding_total_cents": total, "holding_total_display": yuan(total)}


def purchase_wealth(conn, user_id: str, session_id: str, thread_id: str, product_id: str, amount_cents: int) -> Dict[str, Any]:
    p = conn.execute("SELECT * FROM wealth_products WHERE product_id=?", (product_id,)).fetchone()
    if not p:
        raise DomainError("PRODUCT_NOT_FOUND", "没有找到该理财产品")
    if amount_cents < int(p["min_amount_cents"]):
        raise DomainError("BELOW_MIN_AMOUNT", "起购金额为 %s" % yuan(int(p["min_amount_cents"])))
    acc = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
    before = int(acc["balance_cents"])
    if amount_cents > before:
        raise DomainError("INSUFFICIENT_FUNDS", "工资卡余额不足，当前 %s" % yuan(before))
    after = before - amount_cents
    conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (after, acc["account_id"]))
    holding_id = "h_" + secrets.token_hex(4)
    conn.execute("INSERT INTO wealth_holdings VALUES (?,?,?,?,?,?)", (holding_id, user_id, product_id, amount_cents, "holding", db.now_iso()))
    _record_txn(conn, acc["account_id"], user_id, "out", amount_cents, "理财", p["name"], "理财申购", after)
    conn.commit()
    return {"action_id": holding_id, "holding_id": holding_id, "product_id": product_id, "product_name": p["name"],
            "amount_cents": amount_cents, "amount_display": yuan(amount_cents), "balance_before_cents": before,
            "balance_after_cents": after, "status": "holding", "created_at": db.now_iso(),
            "undo_deadline": (datetime.now() + timedelta(seconds=config.UNDO_WINDOW_SECONDS)).isoformat(timespec="seconds")}


def undo_purchase(conn, user_id: str, action_id: str) -> Dict[str, Any]:
    h = conn.execute("SELECT * FROM wealth_holdings WHERE holding_id=? AND user_id=?", (action_id, user_id)).fetchone()
    if not h:
        raise DomainError("ACTION_NOT_FOUND", "没有找到可撤销的理财申购")
    h = dict(h)
    if h["status"] != "holding":
        raise DomainError("ACTION_NOT_UNDOABLE", "该申购当前状态不可撤销")
    bought = datetime.fromisoformat(h["bought_at"])
    if datetime.now().astimezone() - bought > timedelta(seconds=config.UNDO_WINDOW_SECONDS):
        raise DomainError("UNDO_EXPIRED", "已超过撤销窗口")
    acc = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
    conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (int(acc["balance_cents"]) + int(h["amount_cents"]), acc["account_id"]))
    conn.execute("UPDATE wealth_holdings SET status='undone' WHERE holding_id=?", (action_id,))
    _record_txn(conn, acc["account_id"], user_id, "in", int(h["amount_cents"]), "撤销", "理财申购撤销", "理财申购撤销冲正", int(acc["balance_cents"]))
    conn.commit()
    return {"undone": True, "action_id": action_id, "message": "理财申购已撤销，资金已退回工资卡"}


# ---------------- 卡片 ----------------

def list_cards(conn, user_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM cards WHERE user_id=?", (user_id,)).fetchall()]


def apply_virtual_card(conn, user_id: str, daily_limit_cents: int = 200000) -> Dict[str, Any]:
    card_id = "card_" + secrets.token_hex(4)
    last4 = str(secrets.randbelow(9000) + 1000)
    number_masked = "**** " + last4
    conn.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)",
                 (card_id, user_id, "virtual", number_masked, "active", daily_limit_cents, daily_limit_cents, db.now_iso()))
    conn.commit()
    return {"card_id": card_id, "number_masked": number_masked, "status": "active",
            "daily_limit_cents": daily_limit_cents, "daily_limit_display": yuan(daily_limit_cents)}


def set_card_status(conn, user_id: str, card_id: str, status: str) -> Dict[str, Any]:
    card = conn.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (card_id, user_id)).fetchone()
    if not card:
        raise DomainError("CARD_NOT_FOUND", "没有找到该银行卡")
    conn.execute("UPDATE cards SET status=? WHERE card_id=?", (status, card_id))
    conn.commit()
    return {"card_id": card_id, "status": status, "number_masked": card["number_masked"]}


# ---------------- 订阅 ----------------

def list_subscriptions(conn, user_id: str) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY amount_cents DESC", (user_id,)).fetchall()]
    active = [r for r in rows if r["status"] == "active"]
    monthly = sum(r["amount_cents"] for r in active)
    return {"subscriptions": rows, "active_count": len(active), "monthly_total_cents": monthly,
            "monthly_total_display": yuan(monthly), "yearly_total_cents": monthly * 12}


def cancel_subscription(conn, user_id: str, sub_id: str = "", merchant: str = "") -> Dict[str, Any]:
    row = None
    if sub_id:
        row = conn.execute("SELECT * FROM subscriptions WHERE sub_id=? AND user_id=?", (sub_id, user_id)).fetchone()
    elif merchant:
        row = conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND merchant LIKE ?", (user_id, "%" + merchant + "%")).fetchone()
    if not row:
        raise DomainError("SUBSCRIPTION_NOT_FOUND", "没有找到该订阅")
    if row["status"] != "active":
        raise DomainError("SUBSCRIPTION_ALREADY_CANCELLED", "该订阅已经取消")
    conn.execute("UPDATE subscriptions SET status='cancelled' WHERE sub_id=?", (row["sub_id"],))
    conn.commit()
    return {"sub_id": row["sub_id"], "merchant": row["merchant"], "status": "cancelled",
            "saved_monthly_cents": row["amount_cents"], "saved_monthly_display": yuan(row["amount_cents"])}


# ---------------- 提醒 ----------------

def create_reminder(conn, user_id: str, title: str, remind_at: str = "") -> Dict[str, Any]:
    reminder_id = "rm_" + secrets.token_hex(4)
    when = remind_at or (datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=7)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO reminders VALUES (?,?,?,?,?,?)", (reminder_id, user_id, title, when, "pending", db.now_iso()))
    conn.commit()
    return {"reminder_id": reminder_id, "title": title, "remind_at": when, "status": "pending"}


def repay_credit(conn, user_id: str, amount_cents: int) -> Dict[str, Any]:
    acc = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='credit' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
    if not acc:
        raise DomainError("ACCOUNT_NOT_FOUND", "没有找到信用卡账户")
    before = int(acc["balance_cents"])
    if before >= 0:
        raise DomainError("NO_OUTSTANDING_DEBT", "当前信用卡没有待还欠款")
    after = min(0, before + amount_cents)
    paid = after - before
    conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (after, acc["account_id"]))
    _record_txn(conn, acc["account_id"], user_id, "in", paid, "还款", "工资卡", "信用卡还款", after)
    conn.commit()
    return {"action_id": "repay_" + secrets.token_hex(4), "account_id": acc["account_id"],
            "paid_cents": paid, "paid_display": yuan(paid),
            "debt_before_cents": -before, "debt_after_cents": -after}


def verify_balance(conn, account_id: str, expected_cents: int) -> Dict[str, Any]:
    actual = _balance(conn, account_id)
    return {"ok": actual == expected_cents, "expected_cents": expected_cents, "actual_cents": actual}


# ==================== v0.4.0 数据层新增能力 ====================
# 理财赎回、卡额度/卡消费、异常交易扫描、年度账单、手机号找收款人、预约转账。

def _p99(values: List[int]) -> int:
    """最近秩法 P99（无样本返回 0），用于异常交易金额阈值。"""
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    k = max(1, (99 * len(ordered) + 99) // 100)
    return int(ordered[k - 1])


def _hours_between(left, right) -> float:
    """两条 ISO 时间相差的小时数；解析失败返回 -1。"""
    try:
        a = datetime.fromisoformat(str(left))
        b = datetime.fromisoformat(str(right))
        return abs((b - a).total_seconds()) / 3600.0
    except (TypeError, ValueError):
        return -1.0


def _parse_run_at(value: str) -> datetime:
    """解析预约时间：支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / 带时区 ISO；失败抛 SCHEDULE_INVALID。"""
    text = str(value or "").strip()
    if not text:
        raise DomainError("SCHEDULE_INVALID", "预约时间不能为空，请提供 YYYY-MM-DD HH:MM")
    candidate = text.replace(" ", "T", 1) if " " in text else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise DomainError("SCHEDULE_INVALID", "无法识别的时间 %s，请提供 YYYY-MM-DD HH:MM" % text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _first_account(conn, user_id: str, kind: str = "checking"):
    """优先取指定 kind 的第一个账户，没有则回退该用户第一个账户。"""
    row = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind=? ORDER BY account_id LIMIT 1", (user_id, kind)).fetchone()
    if row:
        return row
    return conn.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY account_id LIMIT 1", (user_id,)).fetchone()


CARD_KIND_ORDER = "CASE kind WHEN 'virtual' THEN 0 WHEN 'credit' THEN 1 ELSE 2 END"


def _active_card(conn, user_id: str, card_id: str = ""):
    if card_id:
        return conn.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (card_id, user_id)).fetchone()
    return conn.execute(
        "SELECT * FROM cards WHERE user_id=? AND status='active' ORDER BY " + CARD_KIND_ORDER + ", card_id LIMIT 1",
        (user_id,),
    ).fetchone()


def _any_card(conn, user_id: str, card_id: str = ""):
    """消费用取卡：显式 card_id 优先；否则先取 active 卡，再退到任意卡（便于报出冻结状态）。"""
    if card_id:
        return conn.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (card_id, user_id)).fetchone()
    row = _active_card(conn, user_id)
    if row:
        return row
    return conn.execute("SELECT * FROM cards WHERE user_id=? ORDER BY " + CARD_KIND_ORDER + ", card_id LIMIT 1", (user_id,)).fetchone()


def redeem_wealth(conn, user_id: str, session_id: str, thread_id: str, product_id: str = "",
                  amount_cents: int = 0, holding_id: str = "") -> Dict[str, Any]:
    """理财赎回：按买入时间先进先出扣减持仓，本金回到工资卡（无工资卡回第一个账户）。"""
    amount_cents = int(amount_cents or 0)
    if amount_cents <= 0:
        raise DomainError("AMOUNT_INVALID", "赎回金额必须大于 0")
    sql = "SELECT * FROM wealth_holdings WHERE user_id=? AND status='holding'"
    args: List[Any] = [user_id]
    if holding_id:
        sql += " AND holding_id=?"
        args.append(holding_id)
    if product_id:
        sql += " AND product_id=?"
        args.append(product_id)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY bought_at, holding_id", args).fetchall()]
    total_holding = sum(int(r["amount_cents"]) for r in rows)
    if total_holding < amount_cents:
        raise DomainError("INSUFFICIENT_HOLDING", "可赎回持仓不足，当前持仓 %s" % yuan(total_holding),
                          {"holding_cents": total_holding})
    deducted: List[Dict[str, Any]] = []
    remaining = amount_cents
    for row in rows:
        if remaining <= 0:
            break
        take = min(int(row["amount_cents"]), remaining)
        left = int(row["amount_cents"]) - take
        conn.execute("UPDATE wealth_holdings SET amount_cents=?, status=? WHERE holding_id=?",
                     (left, "holding" if left > 0 else "redeemed", row["holding_id"]))
        deducted.append({"holding_id": row["holding_id"], "product_id": row["product_id"], "amount_cents": take})
        remaining -= take
    first = deducted[0]
    prod = conn.execute("SELECT name FROM wealth_products WHERE product_id=?", (first["product_id"],)).fetchone()
    product_name = str(prod["name"]) if prod else ""
    acc = _first_account(conn, user_id, "checking")
    if not acc:
        raise DomainError("ACCOUNT_NOT_FOUND", "没有可入账的账户")
    after = int(acc["balance_cents"]) + amount_cents
    conn.execute("UPDATE accounts SET balance_cents=? WHERE account_id=?", (after, acc["account_id"]))
    redeemed_at = db.now_iso()
    _record_txn(conn, acc["account_id"], user_id, "in", amount_cents, "赎回", product_name or "理财", "理财赎回",
                after, source="redeem")
    conn.commit()
    return {
        "holding_id": first["holding_id"], "product_id": first["product_id"], "product_name": product_name,
        "amount_cents": amount_cents, "amount_display": yuan(amount_cents),
        "balance_after_cents": after, "balance_after_display": yuan(after),
        "status": "redeemed", "redeemed_at": redeemed_at, "action_id": "rd_" + secrets.token_hex(6),
    }


def set_card_limit(conn, user_id: str, card_id: str = "", daily_limit_cents: Optional[int] = None,
                   single_limit_cents: Optional[int] = None) -> Dict[str, Any]:
    """调整卡额度：只更新传入的非 None 字段；单笔不能大于单日。"""
    card = _active_card(conn, user_id, card_id)
    if not card:
        raise DomainError("CARD_NOT_FOUND", "没有找到该银行卡")
    before = {"daily_limit_cents": int(card["daily_limit_cents"]), "single_limit_cents": int(card["single_limit_cents"])}
    daily = before["daily_limit_cents"] if daily_limit_cents is None else int(daily_limit_cents)
    single = before["single_limit_cents"] if single_limit_cents is None else int(single_limit_cents)
    if daily_limit_cents is not None and daily <= 0:
        raise DomainError("LIMIT_INVALID", "单日限额必须大于 0，当前传入 %s" % yuan(daily))
    if single_limit_cents is not None and single <= 0:
        raise DomainError("LIMIT_INVALID", "单笔限额必须大于 0，当前传入 %s" % yuan(single))
    # 只有本次明确同时给出单笔额度时才校验「单笔 <= 单日」：仅调日限额时允许旧单笔值暂时高于日限额
    if single_limit_cents is not None and daily > 0 and single > daily:
        raise DomainError("LIMIT_INVALID", "单笔限额 %s 不能超过单日限额 %s" % (yuan(single), yuan(daily)))
    conn.execute("UPDATE cards SET daily_limit_cents=?, single_limit_cents=? WHERE card_id=?", (daily, single, card["card_id"]))
    conn.commit()
    after = {"daily_limit_cents": daily, "single_limit_cents": single}
    return {
        "card_id": card["card_id"], "number_masked": card["number_masked"], "kind": card["kind"],
        "daily_limit_cents": daily, "daily_limit_display": yuan(daily),
        "single_limit_cents": single, "single_limit_display": yuan(single),
        "before": before, "after": after,
    }


def card_spend(conn, user_id: str, session_id: str, thread_id: str, card_id: str = "",
               amount_cents: int = 0, merchant: str = "") -> Dict[str, Any]:
    """卡消费：校验卡状态与单笔/单日限额，通过后写入 card_txns 流水。"""
    amount_cents = int(amount_cents or 0)
    card = _any_card(conn, user_id, card_id)
    if not card:
        raise DomainError("CARD_NOT_FOUND", "没有找到该银行卡")
    if str(card["status"]) != "active":
        raise DomainError("CARD_FROZEN", "卡片当前状态为冻结，无法消费")
    if amount_cents <= 0:
        raise DomainError("AMOUNT_INVALID", "消费金额必须大于 0")
    single = int(card["single_limit_cents"] or 0)
    daily = int(card["daily_limit_cents"] or 0)
    if single > 0 and amount_cents > single:
        raise DomainError("CARD_SINGLE_LIMIT", "超出单笔限额 %s，本笔 %s" % (yuan(single), yuan(amount_cents)))
    today = datetime.now().astimezone().date().isoformat()
    used = int(conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS s FROM card_txns WHERE card_id=? AND user_id=? AND ts LIKE ?",
        (card["card_id"], user_id, today + "%"),
    ).fetchone()["s"])
    if daily > 0 and used + amount_cents > daily:
        raise DomainError("CARD_DAILY_LIMIT", "超出单日限额 %s，今日已用 %s，本笔 %s"
                          % (yuan(daily), yuan(used), yuan(amount_cents)))
    txn_id = "ct_" + secrets.token_hex(6)
    ts = db.now_iso()
    conn.execute("INSERT INTO card_txns VALUES (?,?,?,?,?,?,?)",
                 (txn_id, card["card_id"], user_id, ts, amount_cents, str(merchant or ""), "success"))
    conn.commit()
    new_used = used + amount_cents
    remaining = max(0, daily - new_used) if daily > 0 else 0
    return {
        "txn_id": txn_id, "card_id": card["card_id"], "number_masked": card["number_masked"],
        "amount_cents": amount_cents, "amount_display": yuan(amount_cents), "merchant": str(merchant or ""),
        "status": "success", "ts": ts,
        "daily_used_cents": new_used, "daily_used_display": yuan(new_used),
        "daily_limit_cents": daily, "daily_limit_display": yuan(daily),
        "remaining_cents": remaining, "remaining_display": yuan(remaining),
    }


def scan_anomalies(conn, user_id: str, days: int = 30) -> Dict[str, Any]:
    """异常交易识别：金额离群 / 重复扣款 / 深夜大额。无异常返回空 list，不抛异常。"""
    days = max(1, int(days or 30))
    now = datetime.now().astimezone()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    outs = [dict(r) for r in conn.execute(
        "SELECT txn_id, ts, amount_cents, category, counterparty FROM transactions "
        "WHERE user_id=? AND direction='out' ORDER BY ts", (user_id,)).fetchall()]
    all_amounts = [int(r["amount_cents"]) for r in outs]
    p99 = _p99(all_amounts)
    threshold = max(1000 * config.YUAN, p99)
    window = [r for r in outs if str(r["ts"]) >= cutoff]
    anomalies: List[Dict[str, Any]] = []
    for r in window:
        amount = int(r["amount_cents"])
        if amount > threshold:
            anomalies.append({
                "txn_id": r["txn_id"], "ts": r["ts"], "amount_cents": amount, "amount_display": yuan(amount),
                "counterparty": str(r["counterparty"] or ""), "category": str(r["category"] or ""),
                "rule": "amount_outlier", "level": "high",
                "detail": "单笔 %s > 阈值 %s（历史 P99 %s）" % (yuan(amount), yuan(threshold), yuan(p99)),
            })
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for r in window:
        grouped.setdefault((str(r["counterparty"] or ""), int(r["amount_cents"])), []).append(r)
    for (counterparty, amount), rows in grouped.items():
        if len(rows) < 2:
            continue
        ordered = sorted(rows, key=lambda x: str(x["ts"]))
        for i in range(1, len(ordered)):
            gap = _hours_between(ordered[i - 1]["ts"], ordered[i]["ts"])
            if 0 <= gap <= 24:
                anomalies.append({
                    "txn_id": ordered[i]["txn_id"], "ts": ordered[i]["ts"], "amount_cents": amount,
                    "amount_display": yuan(amount), "counterparty": counterparty,
                    "category": str(ordered[i]["category"] or ""),
                    "rule": "duplicate_charge", "level": "medium",
                    "detail": "%.1f 小时内同一收款方「%s」同额 %s 重复出现（第 %d 笔）"
                              % (gap, counterparty, yuan(amount), i + 1),
                })
    for r in window:
        amount = int(r["amount_cents"])
        if amount <= 500 * config.YUAN:
            continue
        try:
            hour = datetime.fromisoformat(str(r["ts"])).hour
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 5:
            anomalies.append({
                "txn_id": r["txn_id"], "ts": r["ts"], "amount_cents": amount, "amount_display": yuan(amount),
                "counterparty": str(r["counterparty"] or ""), "category": str(r["category"] or ""),
                "rule": "late_night_large", "level": "medium",
                "detail": "%02d 点深夜大额支出 %s（> %s）" % (hour, yuan(amount), yuan(500 * config.YUAN)),
            })
    rule_counts: Dict[str, int] = {}
    for item in anomalies:
        rule_counts[item["rule"]] = rule_counts.get(item["rule"], 0) + 1
    anomalies.sort(key=lambda x: str(x["ts"]), reverse=True)
    return {
        "days": days, "scanned_count": len(window), "anomalies": anomalies[:10],
        "rule_counts": rule_counts, "p99_cents": p99, "threshold_cents": threshold,
    }


def annual_report(conn, user_id: str, year: int = 0) -> Dict[str, Any]:
    """年度账单：只统计该自然年的收支，按月份/分类/商户聚合。"""
    try:
        year = int(year or 0)
    except (TypeError, ValueError):
        year = 0
    if not year:
        year = datetime.now().astimezone().year
    prefix = "%04d-" % year
    rows = [dict(r) for r in conn.execute(
        "SELECT ts, direction, amount_cents, category, counterparty FROM transactions WHERE user_id=? AND ts LIKE ?",
        (user_id, prefix + "%")).fetchall()]
    total_out = 0
    total_in = 0
    months_map: Dict[str, Dict[str, Any]] = {}
    cats: Dict[str, int] = {}
    merchants: Dict[str, int] = {}
    for r in rows:
        amount = int(r["amount_cents"])
        month_key = str(r["ts"])[:7]
        bucket = months_map.setdefault(month_key, {"month": month_key, "out_cents": 0, "in_cents": 0})
        if str(r["direction"]) == "out":
            total_out += amount
            bucket["out_cents"] += amount
            category = str(r["category"] or "其他")
            cats[category] = cats.get(category, 0) + amount
            merchant = str(r["counterparty"] or "")
            merchants[merchant] = merchants.get(merchant, 0) + amount
        else:
            total_in += amount
            bucket["in_cents"] += amount
    months = [months_map[k] for k in sorted(months_map)]
    by_category = sorted(
        [{"category": k, "amount_cents": v, "ratio": round(v / total_out, 4) if total_out else 0}
         for k, v in cats.items()],
        key=lambda x: -x["amount_cents"],
    )
    top_merchants = [{"merchant": k, "amount_cents": v}
                     for k, v in sorted(merchants.items(), key=lambda kv: -kv[1])[:5]]
    peak = max(months, key=lambda m: m["out_cents"]) if months else {"month": "", "out_cents": 0}
    avg_monthly = int(round(total_out / len(months))) if months else 0
    return {
        "year": year, "total_out_cents": total_out, "total_in_cents": total_in,
        "net_cents": total_in - total_out,
        "savings_rate": round((total_in - total_out) / total_in, 4) if total_in else 0,
        "txn_count": len(rows), "months": months, "by_category": by_category,
        "top_merchants": top_merchants,
        "peak_month": {"month": peak["month"], "out_cents": peak["out_cents"]},
        "avg_monthly_out_cents": avg_monthly,
    }


def find_payee_by_phone(conn, user_id: str, phone: str) -> Optional[Dict[str, Any]]:
    """按手机号精确匹配收款人（忽略空格/横线）；找不到返回 None，不抛异常。"""
    try:
        target = re.sub(r"[\s\-]", "", str(phone or ""))
        if not target:
            return None
        rows = conn.execute("SELECT * FROM payees WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            item = dict(r)
            stored = re.sub(r"[\s\-]", "", str(item.get("phone") or ""))
            if stored and stored == target:
                return item
        return None
    except Exception:
        return None


# ---------------- 预约转账 ----------------

def create_scheduled_transfer(conn, user_id: str, session_id: str, thread_id: str, from_account_id: str,
                              payee_name: str, payee_account: str, amount_cents: int, run_at: str,
                              memo: str = "", approval_id: str = "") -> Dict[str, Any]:
    """创建预约转账（只登记，不扣款；到期由 scheduler.tick 执行）。"""
    amount_cents = int(amount_cents or 0)
    if amount_cents <= 0:
        raise DomainError("AMOUNT_INVALID", "预约转账金额必须大于 0")
    parsed = _parse_run_at(run_at)
    sched_id = "sch_" + secrets.token_hex(6)
    created_at = db.now_iso()
    run_iso = _iso(parsed)
    seconds_until = int((parsed - datetime.now().astimezone()).total_seconds())
    conn.execute(
        "INSERT INTO scheduled_transfers (sched_id,user_id,session_id,thread_id,from_account_id,payee_name,"
        "payee_account,amount_cents,memo,run_at,status,transfer_id,approval_id,created_at,executed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'pending','',?,?,?)",
        (sched_id, user_id, str(session_id or ""), str(thread_id or ""), from_account_id, payee_name,
         str(payee_account or ""), amount_cents, str(memo or ""), run_iso, str(approval_id or ""), created_at, ""),
    )
    conn.commit()
    return {
        "sched_id": sched_id, "run_at": run_iso, "status": "pending", "amount_cents": amount_cents,
        "amount_display": yuan(amount_cents), "payee_name": payee_name,
        "payee_account": str(payee_account or ""), "created_at": created_at,
        "seconds_until": seconds_until,
    }


def list_scheduled_transfers(conn, user_id: str, status: str = "") -> Dict[str, Any]:
    """列出预约转账（按 run_at 升序），附带 pending/总数。"""
    where = "WHERE user_id=?"
    args: List[Any] = [user_id]
    if status:
        where += " AND status=?"
        args.append(str(status))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM scheduled_transfers " + where + " ORDER BY run_at, created_at", args).fetchall()]
    items = [{
        "sched_id": r["sched_id"], "run_at": r["run_at"], "status": r["status"],
        "amount_cents": int(r["amount_cents"]), "amount_display": yuan(int(r["amount_cents"])),
        "payee_name": r["payee_name"], "payee_account": r["payee_account"],
        "created_at": r["created_at"], "executed_at": r["executed_at"], "transfer_id": r["transfer_id"],
    } for r in rows]
    pending = conn.execute("SELECT COUNT(*) AS n FROM scheduled_transfers WHERE user_id=? AND status='pending'",
                           (user_id,)).fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM scheduled_transfers WHERE user_id=?", (user_id,)).fetchone()["n"]
    return {"items": items, "pending_count": int(pending), "total_count": int(total)}


def cancel_scheduled_transfer(conn, user_id: str, sched_id: str = "") -> Dict[str, Any]:
    """取消预约转账；sched_id 为空时取消最近创建的一条 pending。"""
    row = None
    if sched_id:
        row = conn.execute("SELECT * FROM scheduled_transfers WHERE sched_id=? AND user_id=?",
                           (sched_id, user_id)).fetchone()
        if not row:
            raise DomainError("SCHEDULE_NOT_FOUND", "没有找到该预约转账")
    else:
        row = conn.execute(
            "SELECT * FROM scheduled_transfers WHERE user_id=? AND status='pending' "
            "ORDER BY created_at DESC, run_at DESC LIMIT 1", (user_id,)).fetchone()
        if not row:
            raise DomainError("SCHEDULE_NOT_FOUND", "当前没有可取消的预约转账")
    item = dict(row)
    if item["status"] != "pending":
        raise DomainError("SCHEDULE_NOT_CANCELLABLE", "该预约转账已%s，无法取消"
                          % ("执行" if item["status"] == "executed" else "结束"))
    conn.execute("UPDATE scheduled_transfers SET status='cancelled' WHERE sched_id=?", (item["sched_id"],))
    conn.commit()
    return {
        "sched_id": item["sched_id"], "status": "cancelled", "run_at": item["run_at"],
        "amount_display": yuan(int(item["amount_cents"])), "payee_name": item["payee_name"],
    }


def due_scheduled_transfers(conn, now_iso: str = "") -> List[Dict[str, Any]]:
    """取出已到期（run_at <= now）且仍 pending 的预约转账。"""
    moment = str(now_iso or db.now_iso())
    rows = conn.execute(
        "SELECT * FROM scheduled_transfers WHERE status='pending' AND run_at<=? ORDER BY run_at, sched_id",
        (moment,)).fetchall()
    return [dict(r) for r in rows]


def mark_scheduled(conn, sched_id: str, status: str, transfer_id: str = "", executed_at: str = "") -> Dict[str, Any]:
    """更新预约转账状态并返回最新记录。"""
    row = conn.execute("SELECT * FROM scheduled_transfers WHERE sched_id=?", (sched_id,)).fetchone()
    if not row:
        raise DomainError("SCHEDULE_NOT_FOUND", "没有找到该预约转账")
    item = dict(row)
    exec_at = str(executed_at or "")
    if not exec_at:
        exec_at = db.now_iso() if status in ("executed", "failed") else str(item["executed_at"] or "")
    conn.execute("UPDATE scheduled_transfers SET status=?, transfer_id=?, executed_at=? WHERE sched_id=?",
                 (status, str(transfer_id or item["transfer_id"] or ""), exec_at, sched_id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM scheduled_transfers WHERE sched_id=?", (sched_id,)).fetchone())


def execute_due_transfers(conn, now_iso: str = "") -> Dict[str, Any]:
    """执行所有到期预约：逐条调 create_transfer，成功/失败都落状态；永不抛异常。"""
    result: Dict[str, Any] = {"checked": 0, "executed": [], "failed": [], "ts": db.now_iso()}
    try:
        due = due_scheduled_transfers(conn, now_iso)
    except Exception:
        return result
    for row in due:
        sched_id = str(row["sched_id"])
        amount = int(row["amount_cents"])
        result["checked"] += 1
        try:
            tr = create_transfer(conn, str(row["user_id"]), str(row["session_id"]), str(row["thread_id"]),
                                 str(row["from_account_id"]), str(row["payee_name"]), str(row["payee_account"]),
                                 amount, memo=str(row["memo"] or ""), idempotency_key="sched_" + sched_id,
                                 internal=False)
            transfer_id = str(tr.get("transfer_id") or "")
            mark_scheduled(conn, sched_id, "executed", transfer_id=transfer_id)
            result["executed"].append({
                "sched_id": sched_id, "transfer_id": transfer_id, "amount_cents": amount,
                "amount_display": yuan(amount), "payee_name": str(row["payee_name"]),
                "run_at": str(row["run_at"]),
            })
        except DomainError as exc:
            _mark_schedule_failed(conn, sched_id)
            result["failed"].append({"sched_id": sched_id, "code": exc.code, "message": exc.message,
                                     "amount_cents": amount, "payee_name": str(row["payee_name"])})
        except Exception as exc:  # 单条异常不影响其它预约，也不向上抛
            _mark_schedule_failed(conn, sched_id)
            result["failed"].append({"sched_id": sched_id, "code": "INTERNAL_ERROR",
                                     "message": "预约执行失败：%s" % exc,
                                     "amount_cents": amount, "payee_name": str(row["payee_name"])})
    return result


def _mark_schedule_failed(conn, sched_id: str) -> None:
    try:
        mark_scheduled(conn, sched_id, "failed")
    except Exception:
        pass
