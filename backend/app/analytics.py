"""仪表盘与分析聚合（v0.7.0，只读）。

设计约束（与 docs/architecture.md §7 对齐）：
- **只读**：本模块只执行 SELECT，不写 transactions / events / audit；调用前后审计链必须保持一致。
- **零依赖**：sqlite3 + 标准库，不引用第三方。
- **口径复用**：账户、流水、预算、订阅、异常、持仓全部取自 mock_core 的既有实现，
  本模块只负责「按时间窗聚合」与「前端可直接渲染的结构」，
  不复制业务规则（预算上限、异常阈值、订阅状态机都留在原处）。
- **失败可预期**：参数非法抛 RangeError，由 HTTP 层转 400；不抛未捕获异常。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import audit, mock_core

# 仪表盘支持的窗口：kind -> 天数（"6m"/"180d" 等价）
WINDOWS = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "6m": 180, "1y": 365}

# 分析页支持的时间粒度
GRANULARITIES = ("day", "week", "month")

# 单次分析请求覆盖的最大天数：超过就让客户端换粒度，避免大范围日粒度把响应撑爆
MAX_RANGE_DAYS = 400
MAX_DAY_BUCKETS = 120


class RangeError(ValueError):
    """时间窗/日期参数非法（HTTP 层转 400，不写审计）。"""


# ---------------------------------------------------------------- 工具函数

def _now() -> datetime:
    return datetime.now()


def _yuan(cents: int) -> str:
    return mock_core.yuan(int(cents))


MIN_BASELINE_CENTS = 10000  # 基准期金额不足 ¥100 时不给环比，避免天文百分比


def _pct(cur: int, prev: int) -> Optional[int]:
    """环比：基准期缺失或过小时返回 None（前端显示「—」），不伪造百分比。"""
    if not prev or abs(int(prev)) < MIN_BASELINE_CENTS:
        return None
    return int(round((int(cur) - int(prev)) * 100.0 / abs(int(prev))))


def _share(cents: int, total: int) -> float:
    if not total:
        return 0.0
    return round(int(cents) * 100.0 / int(total), 2)


def _month_start(d: date, offset_months: int = 0) -> date:
    """取 d 所在月的 1 号，并前后平移 offset_months 个月。"""
    y, m = d.year, d.month - offset_months
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return date(y, m, 1)


def _month_key(d: date) -> str:
    return "%04d-%02d" % (d.year, d.month)


def _parse_window(raw: str) -> Tuple[int, str]:
    """把 window 参数解析成 (天数, 规范名)。非法值按 30d 处理并回写规范名。"""
    text = str(raw or "").strip().lower()
    if not text:
        return WINDOWS["30d"], "30d"
    if text in WINDOWS:
        return WINDOWS[text], text
    digits = text.rstrip("d").strip()
    if digits.isdigit():
        days = int(digits)
        if 1 <= days <= 365:
            return days, "%dd" % days
    return WINDOWS["30d"], "30d"


def _parse_day(raw: str, field: str) -> date:
    text = str(raw or "").strip()
    if not text:
        raise RangeError("%s 不能为空，格式 YYYY-MM-DD" % field)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise RangeError("%s 不是合法日期（YYYY-MM-DD）：%s" % (field, text))


# ---------------------------------------------------------------- 数据抽取

def _monthly_flows(conn, user_id: str, months: int = 6) -> List[Dict[str, Any]]:
    """近 N 个自然月（含当月）的收支，缺失月补 0。"""
    today = date.today()
    first = _month_start(today, months - 1)
    rows = conn.execute(
        "SELECT substr(ts,1,7) AS mk, direction, COALESCE(SUM(amount_cents),0) AS total "
        "FROM transactions WHERE user_id=? AND ts>=? GROUP BY mk, direction",
        (user_id, first.isoformat()),
    ).fetchall()
    acc: Dict[str, Dict[str, int]] = {}
    for r in rows:
        item = acc.setdefault(str(r["mk"]), {"in": 0, "out": 0})
        key = "in" if str(r["direction"]) == "in" else "out"
        item[key] += int(r["total"] or 0)
    series: List[Dict[str, Any]] = []
    for i in range(months - 1, -1, -1):
        mk = _month_key(_month_start(today, i))
        item = acc.get(mk, {"in": 0, "out": 0})
        series.append({"month": mk, "in_cents": item["in"], "out_cents": item["out"],
                       "net_cents": item["in"] - item["out"]})
    return series


def _daily_flows(conn, user_id: str, days: int = 14) -> List[Dict[str, Any]]:
    """近 N 天逐日收支，缺失日补 0（sparkline 用，长度固定）。"""
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT substr(ts,1,10) AS dk, direction, COALESCE(SUM(amount_cents),0) AS total "
        "FROM transactions WHERE user_id=? AND ts>=? GROUP BY dk, direction",
        (user_id, start.isoformat()),
    ).fetchall()
    acc: Dict[str, Dict[str, int]] = {}
    for r in rows:
        item = acc.setdefault(str(r["dk"]), {"in": 0, "out": 0})
        key = "in" if str(r["direction"]) == "in" else "out"
        item[key] += int(r["total"] or 0)
    out: List[Dict[str, Any]] = []
    for i in range(days):
        dk = (start + timedelta(days=i)).isoformat()
        item = acc.get(dk, {"in": 0, "out": 0})
        out.append({"day": dk, "in_cents": item["in"], "out_cents": item["out"]})
    return out


def _account_map(conn, user_id: str) -> Dict[str, Dict[str, Any]]:
    return {str(a["account_id"]): a for a in mock_core.list_accounts(conn, user_id)}


def _budget_rows(conn, user_id: str) -> List[Dict[str, Any]]:
    """预算执行：固定 6 类全部返回（没花钱的也返回，前端才不会跳行）。"""
    caps = {str(r["category"]): int(r["monthly_cents"]) for r in
            conn.execute("SELECT category, monthly_cents FROM budgets WHERE user_id=?", (user_id,)).fetchall()}
    bill = mock_core.bill_summary(conn, user_id, 1)
    spent = {str(c["category"]): int(c["amount_cents"]) for c in bill.get("by_category") or []}
    rows: List[Dict[str, Any]] = []
    for category, cap in caps.items():
        used = spent.get(category, 0)
        ratio = (used * 100.0 / cap) if cap else 0.0
        if used > cap:
            state = "over"
        elif ratio >= 80:
            state = "warn"
        else:
            state = "ok"
        rows.append({
            "category": category,
            "limit_cents": cap,
            "used_cents": used,
            "remaining_cents": cap - used,
            "pct": int(round(ratio)),
            "state": state,
        })
    rows.sort(key=lambda x: (-int(x["pct"]), x["category"]))
    return rows


def _subscriptions_due(conn, user_id: str, within_days: int = 7) -> List[Dict[str, Any]]:
    today = date.today()
    items: List[Dict[str, Any]] = []
    for row in conn.execute(
        "SELECT sub_id, merchant, amount_cents, cycle, next_charge, status FROM subscriptions "
        "WHERE user_id=? AND status='active'", (user_id,),
    ).fetchall():
        try:
            due = date.fromisoformat(str(row["next_charge"])[:10])
        except (TypeError, ValueError):
            continue
        days = (due - today).days
        if days > within_days:
            continue
        items.append({
            "sub_id": row["sub_id"], "name": row["merchant"], "amount_cents": int(row["amount_cents"]),
            "amount_display": _yuan(int(row["amount_cents"])), "cycle": row["cycle"],
            "next_charge": str(row["next_charge"])[:10], "days_left": days,
            "state": "due" if days <= 0 else "soon",
        })
    items.sort(key=lambda x: (x["days_left"], -x["amount_cents"]))
    return items


def _pending_approvals(conn, user_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM approvals a JOIN sessions s ON a.session_id=s.session_id "
        "WHERE s.user_id=? AND a.status='pending'", (user_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


def _chain_state(conn) -> Dict[str, Any]:
    result = audit.verify(conn)
    return {"valid": bool(result.get("valid")), "checked": int(result.get("checked") or 0)}


# ---------------------------------------------------------------- 仪表盘

def dashboard(conn, user_id: str, window: str = "30d") -> Dict[str, Any]:
    """首页仪表盘：一屏回答「钱在哪、这个月赚没赚、有没有事要处理」。

    只读，不写审计；同一次调用内所有口径都取自同一时刻的库状态。
    """
    days, window_name = _parse_window(window)
    today = date.today()

    accounts = mock_core.list_accounts(conn, user_id)
    wealth = mock_core.list_wealth(conn, user_id)
    holdings_cents = int(wealth.get("holding_total_cents") or 0)
    balances = [int(a["balance_cents"]) for a in accounts]
    credit_cents = sum(int(a["balance_cents"]) for a in accounts if str(a.get("kind")) == "credit")
    available_cents = sum(int(a["balance_cents"]) for a in accounts if str(a.get("kind")) != "credit")
    net_worth_cents = sum(balances) + holdings_cents
    credit_due_cents = max(0, -credit_cents)

    months = _monthly_flows(conn, user_id, 7)
    current = months[-1] if months else {"month": _month_key(today), "in_cents": 0, "out_cents": 0, "net_cents": 0}
    prev = months[-2] if len(months) > 1 else {"in_cents": 0, "out_cents": 0, "net_cents": 0}
    daily = _daily_flows(conn, user_id, 14)

    bill = mock_core.bill_summary(conn, user_id, 1)
    anomalies = mock_core.scan_anomalies(conn, user_id, days)
    anomaly_items = anomalies.get("anomalies") or []
    scheduled = mock_core.list_scheduled_transfers(conn, user_id, "pending")
    subs_due = _subscriptions_due(conn, user_id, 7)

    by_category = []
    for c in (bill.get("by_category") or [])[:6]:
        by_category.append({
            "category": c.get("category"),
            "cents": int(c.get("amount_cents") or 0),
            "display": _yuan(int(c.get("amount_cents") or 0)),
            "pct": _share(int(c.get("amount_cents") or 0), int(bill.get("total_out_cents") or 0)),
        })

    acc_map = {str(a["account_id"]): a for a in accounts}
    recent = []
    for txn in mock_core.list_transactions(conn, user_id, limit=8):
        acc = acc_map.get(str(txn.get("account_id"))) or {}
        recent.append({
            "txn_id": txn.get("txn_id"),
            "ts": txn.get("ts"),
            "direction": txn.get("direction"),
            "amount_cents": int(txn.get("amount_cents") or 0),
            "amount_display": _yuan(int(txn.get("amount_cents") or 0)),
            "category": txn.get("category"),
            "counterparty": txn.get("counterparty"),
            "account_last4": acc.get("number_masked") or "",
            "account_name": acc.get("name") or "",
        })

    todo_rows: List[Dict[str, Any]] = []
    pending = _pending_approvals(conn, user_id)
    if pending:
        todo_rows.append({"kind": "approval", "label": "待你确认的操作", "count": pending,
                          "detail": "点开工作台里的审批卡继续", "target": "home"})
    if scheduled.get("pending_count"):
        items = (scheduled.get("items") or [])[:3]
        todo_rows.append({
            "kind": "scheduled", "label": "预约转账", "count": int(scheduled["pending_count"]),
            "detail": "；".join("%s %s" % (i.get("run_at", "")[:16].replace("T", " "), i.get("amount_display", ""))
                              for i in items),
            "target": "home",
        })
    if subs_due:
        todo_rows.append({
            "kind": "subscription", "label": "7 天内自动扣费", "count": len(subs_due),
            "detail": "；".join("%s %s（%s 天）" % (s["name"], s["amount_display"], s["days_left"]) for s in subs_due[:3]),
            "target": "zone:analytics",
        })
    if anomaly_items:
        todo_rows.append({
            "kind": "anomaly", "label": "异常消费", "count": len(anomaly_items),
            "detail": str(anomaly_items[0].get("detail") or "")[:60], "target": "zone:analytics",
        })
    if pending == 0 and not scheduled.get("pending_count") and not subs_due and not anomaly_items:
        todo_rows.append({"kind": "clear", "label": "没有待处理事项", "count": 0,
                          "detail": "资金链路上没有异常，也没有等你确认的操作", "target": ""})

    chain = _chain_state(conn)
    return {
        "as_of": _now().isoformat(timespec="seconds"),
        "window": {"kind": window_name, "days": days,
                    "from": (today - timedelta(days=days - 1)).isoformat(), "to": today.isoformat()},
        "kpis": {
            "net_worth_cents": net_worth_cents,
            "net_worth_display": _yuan(net_worth_cents),
            "available_cents": available_cents,
            "available_display": _yuan(available_cents),
            "holdings_cents": holdings_cents,
            "holdings_display": _yuan(holdings_cents),
            "credit_due_cents": credit_due_cents,
            "credit_due_display": _yuan(credit_due_cents),
            "month_out_cents": int(current["out_cents"]),
            "month_out_display": _yuan(int(current["out_cents"])),
            "month_in_cents": int(current["in_cents"]),
            "month_in_display": _yuan(int(current["in_cents"])),
            "net_flow_cents": int(current["net_cents"]),
            "net_flow_display": _yuan(int(current["net_cents"])),
            "month": current["month"],
            "deltas": {
                "month_out_pct": _pct(int(current["out_cents"]), int(prev["out_cents"])),
                "month_in_pct": _pct(int(current["in_cents"]), int(prev["in_cents"])),
                "net_flow_pct": _pct(int(current["net_cents"]), int(prev["net_cents"])),
            },
            "prev": {"month": prev.get("month"), "out_cents": int(prev["out_cents"]),
                     "in_cents": int(prev["in_cents"]), "net_cents": int(prev["net_cents"])},
            "sparklines": {
                "out_cents": [item["out_cents"] for item in daily],
                "in_cents": [item["in_cents"] for item in daily],
                "days": [item["day"] for item in daily],
            },
        },
        "trend": {"months": months[1:] if len(months) > 6 else months, "unit": "cents"},
        "budget": _budget_rows(conn, user_id),
        "composition": {
            "total_out_cents": int(bill.get("total_out_cents") or 0),
            "by_category": by_category,
            "by_account": [{
                "account_id": a["account_id"], "name": a.get("name"), "kind": a.get("kind"),
                "number_masked": a.get("number_masked"), "balance_cents": int(a["balance_cents"]),
                "balance_display": _yuan(int(a["balance_cents"])),
            } for a in accounts],
            "holdings_cents": holdings_cents,
            "holdings_display": _yuan(holdings_cents),
        },
        "todo": {"rows": todo_rows, "pending_approvals": pending,
                 "anomaly_count": len(anomaly_items), "subscriptions_due": len(subs_due),
                 "scheduled_pending": int(scheduled.get("pending_count") or 0),
                 "chain": chain},
        "recent": recent,
    }


# ---------------------------------------------------------------- 分析页

def _bucket_key(d: date, granularity: str) -> str:
    if granularity == "day":
        return d.isoformat()
    if granularity == "week":
        iso = d.isocalendar()
        return "%04d-W%02d" % (iso[0], iso[1])
    return _month_key(d)


def _bucket_range(start: date, end: date, granularity: str) -> List[str]:
    """按粒度铺满整个区间，保证图表 X 轴连续（缺失桶补 0，不跳格）。"""
    keys: List[str] = []
    if granularity == "month":
        cur = _month_start(start)
        last = _month_start(end)
        while cur <= last:
            keys.append(_month_key(cur))
            y, m = cur.year, cur.month + 1
            if m > 12:
                y, m = y + 1, 1
            cur = date(y, m, 1)
        return keys
    if granularity == "week":
        cur = start - timedelta(days=start.weekday())
        last = end
        while cur <= last:
            keys.append(_bucket_key(cur, "week"))
            cur = cur + timedelta(days=7)
        return keys
    cur = start
    while cur <= end:
        keys.append(cur.isoformat())
        cur += timedelta(days=1)
    return keys


def _range_rows(conn, user_id: str, start: date, end: date) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT txn_id, ts, direction, amount_cents, category, counterparty, account_id "
        "FROM transactions WHERE user_id=? AND ts>=? AND ts<? ORDER BY ts",
        (user_id, start.isoformat(), (end + timedelta(days=1)).isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def analytics(conn, user_id: str, start: str = "", end: str = "", granularity: str = "month") -> Dict[str, Any]:
    """分析页：可自由切换时间范围与粒度，输出与图表一一对应的桶序列。

    与仪表盘的区别是「探索」：任意区间、任意粒度、带上一周期对比与预算使用率。
    只读，不写审计。
    """
    gran = str(granularity or "month").strip().lower()
    if gran not in GRANULARITIES:
        raise RangeError("granularity 只能是 day / week / month")

    today = date.today()
    end_day = _parse_day(end, "end") if str(end or "").strip() else today
    start_day = _parse_day(start, "start") if str(start or "").strip() else end_day - timedelta(days=29)
    if start_day > end_day:
        raise RangeError("start 不能晚于 end")
    span = (end_day - start_day).days + 1
    if span > MAX_RANGE_DAYS:
        raise RangeError("时间范围最多 %d 天，当前 %d 天；更大的范围请换成 month 粒度" % (MAX_RANGE_DAYS, span))
    if gran == "day" and span > MAX_DAY_BUCKETS:
        raise RangeError("日粒度最多 %d 天，当前 %d 天；请换成 week 或 month 粒度" % (MAX_DAY_BUCKETS, span))

    rows = _range_rows(conn, user_id, start_day, end_day)
    keys = _bucket_range(start_day, end_day, gran)
    buckets: Dict[str, Dict[str, int]] = {k: {"in_cents": 0, "out_cents": 0, "count": 0} for k in keys}

    total_in = total_out = 0
    cat_cents: Dict[str, int] = {}
    merchant_cents: Dict[str, int] = {}
    merchant_count: Dict[str, int] = {}
    for r in rows:
        amount = int(r["amount_cents"] or 0)
        direction = str(r["direction"])
        try:
            ts_day = date.fromisoformat(str(r["ts"])[:10])
        except (TypeError, ValueError):
            continue
        key = _bucket_key(ts_day, gran)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["count"] += 1
        if direction == "in":
            bucket["in_cents"] += amount
            total_in += amount
            continue
        bucket["out_cents"] += amount
        total_out += amount
        category = str(r["category"] or "其他")
        cat_cents[category] = cat_cents.get(category, 0) + amount
        merchant = str(r["counterparty"] or "未知")
        merchant_cents[merchant] = merchant_cents.get(merchant, 0) + amount
        merchant_count[merchant] = merchant_count.get(merchant, 0) + 1

    # 上一周期对比：紧邻的等长区间（不含当前区间）
    prev_end = start_day - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    prev_rows = _range_rows(conn, user_id, prev_start, prev_end)
    prev_out = sum(int(r["amount_cents"] or 0) for r in prev_rows if str(r["direction"]) == "out")
    prev_in = sum(int(r["amount_cents"] or 0) for r in prev_rows if str(r["direction"]) == "in")
    prev_cats: Dict[str, int] = {}
    for r in prev_rows:
        if str(r["direction"]) != "out":
            continue
        category = str(r["category"] or "其他")
        prev_cats[category] = prev_cats.get(category, 0) + int(r["amount_cents"] or 0)

    caps = {str(r["category"]): int(r["monthly_cents"]) for r in
            conn.execute("SELECT category, monthly_cents FROM budgets WHERE user_id=?", (user_id,)).fetchall()}
    # 预算按「覆盖月份数」折算，跨多月区间才不会把 6 个月支出和单月预算比
    month_span = max(1, len([k for k in keys if gran == "month"]) if gran == "month" else 1)
    if gran != "month":
        month_span = max(1, int(round(span / 30.0)))

    categories = []
    for category, cents in sorted(cat_cents.items(), key=lambda kv: -kv[1]):
        cap = caps.get(category)
        limit = cap * month_span if cap else 0
        categories.append({
            "category": category,
            "cents": cents,
            "display": _yuan(cents),
            "pct": _share(cents, total_out),
            "prev_cents": prev_cats.get(category, 0),
            "delta_pct": _pct(cents, prev_cats.get(category, 0)),
            "budget_cents": limit,
            "budget_pct": (int(round(cents * 100.0 / limit)) if limit else None),
            "over_budget": bool(limit and cents > limit),
        })

    merchants = [{
        "merchant": name, "cents": cents, "display": _yuan(cents),
        "pct": _share(cents, total_out), "count": merchant_count.get(name, 0),
    } for name, cents in sorted(merchant_cents.items(), key=lambda kv: -kv[1])[:10]]

    anomalies = [a for a in (mock_core.scan_anomalies(conn, user_id, span).get("anomalies") or [])
                 if str(a.get("ts", ""))[:10] >= start_day.isoformat() and str(a.get("ts", ""))[:10] <= end_day.isoformat()]

    subs = []
    for item in mock_core.list_subscriptions(conn, user_id).get("subscriptions") or []:
        if str(item.get("status")) != "active":
            continue
        days_left = None
        try:
            days_left = (date.fromisoformat(str(item.get("next_charge"))[:10]) - today).days
        except (TypeError, ValueError):
            days_left = None
        subs.append({
            "sub_id": item.get("sub_id"), "name": item.get("merchant"), "cycle": item.get("cycle"),
            "amount_cents": int(item.get("amount_cents") or 0),
            "amount_display": _yuan(int(item.get("amount_cents") or 0)),
            "next_charge": str(item.get("next_charge"))[:10], "days_left": days_left,
        })
    subs.sort(key=lambda x: (999 if x["days_left"] is None else x["days_left"]))

    acc_map = {str(a["account_id"]): a for a in mock_core.list_accounts(conn, user_id)}
    txns = []
    for r in reversed(rows[-200:]):
        acc = acc_map.get(str(r.get("account_id"))) or {}
        txns.append({
            "txn_id": r.get("txn_id"), "ts": r.get("ts"), "direction": r.get("direction"),
            "amount_cents": int(r.get("amount_cents") or 0),
            "amount_display": _yuan(int(r.get("amount_cents") or 0)),
            "category": r.get("category"), "counterparty": r.get("counterparty"),
            "account_last4": acc.get("number_masked") or "",
        })
    txns.sort(key=lambda x: str(x.get("ts")), reverse=True)

    budget = mock_core.budget_analysis(conn, user_id)
    suggestions = [str(s) for s in (budget.get("suggestions") or [])][:3]
    over_budget_cents = sum(max(0, int(b["spent_cents"]) - int(b["budget_cents"])) for b in budget.get("over_budget") or [])

    series = [{"bucket": k,
               "in_cents": buckets[k]["in_cents"],
               "out_cents": buckets[k]["out_cents"],
               "net_cents": buckets[k]["in_cents"] - buckets[k]["out_cents"],
               "count": buckets[k]["count"]} for k in keys]

    return {
        "as_of": _now().isoformat(timespec="seconds"),
        "range": {
            "start": start_day.isoformat(), "end": end_day.isoformat(),
            "span_days": span, "granularity": gran, "buckets": len(keys),
            "prev": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
        },
        "totals": {
            "in_cents": total_in, "in_display": _yuan(total_in),
            "out_cents": total_out, "out_display": _yuan(total_out),
            "net_cents": total_in - total_out, "net_display": _yuan(total_in - total_out),
            "txn_count": len(rows),
            "avg_day_out_cents": int(round(total_out / float(span))) if span else 0,
            "avg_day_out_display": _yuan(int(round(total_out / float(span))) if span else 0),
            "prev_in_cents": prev_in, "prev_out_cents": prev_out,
            "out_delta_pct": _pct(total_out, prev_out),
            "in_delta_pct": _pct(total_in, prev_in),
        },
        "series": series,
        "categories": categories,
        "merchants": merchants,
        "transactions": txns[:100],
        "anomalies": anomalies[:10],
        "subscriptions": subs,
        "savings": {
            "potential_cents": over_budget_cents,
            "potential_display": _yuan(over_budget_cents),
            "savings_rate": budget.get("savings_rate"),
            "suggestions": suggestions,
        },
    }
