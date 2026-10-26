"""模拟银行核心：账户、流水、转账、理财、卡片、订阅、提醒。

所有资金操作都在这里以确定性代码完成，LLM 只负责理解意图，不接触账本。
金额单位：分。
"""
import json
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


def add_payee(conn, user_id: str, name: str, bank: str, account_no: str) -> Dict[str, Any]:
    payee_id = "p_" + secrets.token_hex(4)
    conn.execute("INSERT INTO payees VALUES (?,?,?,?,?,?,?)", (payee_id, user_id, name, bank, account_no, db.now_iso(), 1))
    conn.commit()
    return {"payee_id": payee_id, "name": name, "bank": bank, "account_no": account_no, "is_new": 1}


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