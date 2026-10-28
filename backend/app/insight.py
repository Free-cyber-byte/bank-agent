"""消费洞察与订阅识别（v0.5.0，只读纯函数，零第三方依赖）。

两个公开函数都只读数据库、不写任何业务表，金额单位全部为整数分：
  spending_insights(conn, user_id)  -> 本月支出/分类/异常/预算/可省建议（A1 冻结结构）
  detect_subscriptions(conn, user_id, window_days=120) -> 从流水识别周期扣费（A2 冻结结构）

口径（与 mock_core 保持一致，便于测试直算）：
- totals / top_categories / budget.spent_cents 复用 mock_core.bill_summary(conn, user_id, 1)，
  即「近 30 天」滚动窗口；上期 = 再往前的 30 天（用于环比）。
- delta_pct 为整数四舍五入（负值远离 0）；上期为 0 时写 0。
- anomalies 直接复用 mock_core.scan_anomalies，取前 5 条。
- potential_save_monthly_cents 只累加 savings 里 kind="subscription" 的 monthly_cents（未登记订阅优先）。
- 周期扣费分两类：subscription_like（category=订阅 或命中已登记订阅）才计入「可省订阅」；
  其余（房租/还款/水电/通讯/储蓄转账）进 fixed_cycles 单列说明，不计入可省金额。
"""
import calendar
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import mock_core
from .mock_core import yuan

# 周期扣费识别参数（契约 A2）
_PERIOD_MIN_DAYS = 25
_PERIOD_MAX_DAYS = 35
_AMOUNT_TOLERANCE = 0.10
_DEFAULT_WINDOW_DAYS = 120
_MAX_MERCHANT_SUFFIXES = ("-自动扣款", " 月费")
_TOP_CATEGORY_LIMIT = 5
_ANOMALY_LIMIT = 5
_SAVING_CATEGORY = "订阅"


def _pct(cur_cents: int, prev_cents: int) -> int:
    """整数百分比四舍五入（负值远离 0）；上期为 0 时返回 0。"""
    if prev_cents <= 0:
        return 0
    value = (int(cur_cents) - int(prev_cents)) * 100.0 / float(prev_cents)
    if value >= 0:
        return int(value + 0.5)
    return -int(-value + 0.5)


def _share(cents: int, total_cents: int) -> int:
    """份额百分比（整数四舍五入）：分类金额占总支出的比例。"""
    if int(total_cents) <= 0:
        return 0
    return int(int(cents) * 100.0 / float(total_cents) + 0.5)


def _display_merchant(raw: str) -> str:
    """展示用商户名：去首尾空白与常见自动扣款后缀，保留原大小写。"""
    name = str(raw or "").strip()
    changed = True
    while changed:
        changed = False
        for suffix in _MAX_MERCHANT_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip()
                changed = True
    return name


def _normalize_merchant(raw: str) -> str:
    """匹配用商户名：去后缀后小写归一。"""
    return _display_merchant(raw).lower()


def _between(now: datetime, days_start: int, days_end: int) -> Tuple[str, str]:
    """返回 [now-days_start, now-days_end) 的 ISO 时间边界（字符串比较即可筛选）。"""
    return ((now - timedelta(days=days_start)).isoformat(timespec="seconds"),
            (now - timedelta(days=days_end)).isoformat(timespec="seconds"))


def _out_rows(conn, user_id: str, start_ts: str, end_ts: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT ts, amount_cents, category, counterparty FROM transactions WHERE user_id=? AND direction='out' AND ts>=?"
    params: List[Any] = [user_id, start_ts]
    if end_ts:
        sql += " AND ts<?"
        params.append(end_ts)
    sql += " ORDER BY ts"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _prev_aggregate(conn, user_id: str) -> Tuple[int, Dict[str, int]]:
    """上期（近 30 天之前的那 30 天）出账合计与分类合计。"""
    start_ts, end_ts = _between(datetime.now(), 60, 30)
    total = 0
    cats: Dict[str, int] = {}
    for row in _out_rows(conn, user_id, start_ts, end_ts):
        amount = int(row["amount_cents"] or 0)
        total += amount
        cat = str(row["category"] or "其他")
        cats[cat] = cats.get(cat, 0) + amount
    return total, cats


def _budget_snapshot(conn, user_id: str, out_cents: int) -> Dict[str, int]:
    """预算预测：预算合计 / 近 30 天已花 / 按节奏外推整月 / 预计超支 / 本月剩余天数。"""
    budget_cents = 0
    try:
        row = conn.execute("SELECT COALESCE(SUM(monthly_cents),0) AS s FROM budgets WHERE user_id=?", (user_id,)).fetchone()
        budget_cents = int((row["s"] if row else 0) or 0)
    except Exception:  # noqa: BLE001 - 预算表缺失时按 0 处理，洞察不能因此失败
        budget_cents = 0
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projected = int(round(int(out_cents) * days_in_month / 30.0))
    return {
        "budget_cents": budget_cents,
        "spent_cents": int(out_cents),
        "projected_cents": projected,
        "over_cents": max(0, projected - budget_cents),
        "days_left": max(0, days_in_month - today.day),
    }


def _anomalies(conn, user_id: str) -> List[Dict[str, Any]]:
    """复用 mock_core.scan_anomalies，只取前 5 条并压成洞察卡片需要的字段。"""
    items: List[Dict[str, Any]] = []
    try:
        data = mock_core.scan_anomalies(conn, user_id, 30) or {}
    except Exception:  # noqa: BLE001 - 异常扫描失败不应拖垮洞察
        return items
    for a in (data.get("anomalies") or [])[:_ANOMALY_LIMIT]:
        items.append({
            "date": str(a.get("ts") or "")[:10],
            "merchant": str(a.get("counterparty") or ""),
            "cents": int(a.get("amount_cents") or 0),
            "reason": str(a.get("detail") or ""),
        })
    return items


def _subscription_rows(conn, user_id: str) -> Dict[str, Dict[str, Any]]:
    """已登记订阅：归一化商户名 -> 行。"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,)).fetchall()
    except Exception:  # noqa: BLE001 - 表缺失时按无登记处理
        return out
    for r in rows:
        row = dict(r)
        key = _normalize_merchant(row.get("merchant") or "")
        if key:
            out[key] = row
    return out


def _match_subscription(norm_merchant: str, registered: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按归一化商户名比对登记订阅：先全等，再双向包含（兼容「爱奇艺」vs「爱奇艺 VIP」）。"""
    if not norm_merchant:
        return None
    if norm_merchant in registered:
        return registered[norm_merchant]
    for key, row in registered.items():
        if key and (key in norm_merchant or norm_merchant in key):
            return row
    return None


def detect_subscriptions(conn, user_id: str, window_days: int = _DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    """从出账流水识别周期扣费（确定性规则，只读）。

    规则（契约 A2）：同一商户窗口内出现 >=2 次、相邻间隔 25—35 天、相邻金额差 <=10%，
    判定为周期扣费；monthly_cents 取最近一次金额；confidence = 60 + 10×(次数-2)，上限 30。
    """
    try:
        days = int(window_days or _DEFAULT_WINDOW_DAYS)
    except (TypeError, ValueError):
        days = _DEFAULT_WINDOW_DAYS
    days = max(1, min(days, 3650))
    start_ts = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in _out_rows(conn, user_id, start_ts):
        display = _display_merchant(row.get("counterparty") or "")
        key = _normalize_merchant(row.get("counterparty") or "")
        if not key:
            continue
        groups.setdefault(key, []).append({**row, "display": display})

    registered = _subscription_rows(conn, user_id)
    detected: List[Dict[str, Any]] = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        ordered = sorted(rows, key=lambda r: str(r.get("ts") or ""))
        ok = True
        prev_date: Optional[date] = None
        prev_amount = 0
        for r in ordered:
            try:
                cur_date = date.fromisoformat(str(r.get("ts") or "")[:10])
            except ValueError:
                ok = False
                break
            amount = int(r.get("amount_cents") or 0)
            if prev_date is not None:
                gap = (cur_date - prev_date).days
                if gap < _PERIOD_MIN_DAYS or gap > _PERIOD_MAX_DAYS:
                    ok = False
                    break
                if prev_amount <= 0 or abs(amount - prev_amount) > prev_amount * _AMOUNT_TOLERANCE:
                    ok = False
                    break
            prev_date = cur_date
            prev_amount = amount
        if not ok:
            continue
        latest = ordered[-1]
        occurrences = len(ordered)
        sub_row = _match_subscription(key, registered)
        category = str(latest.get("category") or "")
        detected.append({
            "merchant": str(latest.get("display") or latest.get("counterparty") or ""),
            "monthly_cents": int(latest.get("amount_cents") or 0),
            "occurrences": occurrences,
            "last_date": str(latest.get("ts") or "")[:10],
            "confidence": min(60 + 10 * (occurrences - 2), 90),
            "registered": sub_row is not None,
            "sub_id": str(sub_row.get("sub_id") or "") if sub_row else "",
            # v0.5.0 补充：最近一笔交易的分类与「像不像订阅」的判断（供洞察区分订阅/固定周期扣费）
            "category": category,
            "subscription_like": bool(sub_row) or category == _SAVING_CATEGORY,
        })

    detected.sort(key=lambda x: (-int(x["monthly_cents"]), x["merchant"]))
    registered_count = sum(1 for d in detected if d.get("registered"))
    unregistered_monthly = sum(int(d["monthly_cents"]) for d in detected if not d.get("registered"))
    return {"window_days": days, "registered_count": registered_count,
            "unregistered_monthly_cents": int(unregistered_monthly), "detected": detected}


def _savings(conn, user_id: str, top_categories: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    """可省建议：只把 subscription_like 的周期扣款算作「可省订阅」，另单列固定周期扣费。

    返回 (savings, potential_save_monthly_cents, fixed_cycles)：
    - 订阅类（未登记优先）+ 一条分类优化建议；
    - fixed_cycles 单列房租/还款/水电/通讯等周期扣费，count / monthly_cents / items 只做说明，不计入可省金额。
    """
    savings: List[Dict[str, Any]] = []
    try:
        detected = detect_subscriptions(conn, user_id, _DEFAULT_WINDOW_DAYS) or {}
    except Exception:  # noqa: BLE001 - 识别失败时退化为空列表，洞察仍可用
        detected = {}
    registered = _subscription_rows(conn, user_id)
    cat_rows = _out_rows(conn, user_id, (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds"))
    merchant_cats: Dict[str, str] = {}
    for row in cat_rows:
        cat = str(row.get("category") or "")
        if cat:
            merchant_cats.setdefault(_normalize_merchant(row.get("counterparty") or ""), cat)

    # 1) 订阅类：先放未登记的（更值得提醒），再放已登记的；固定周期扣费单列不计入
    entries: List[Dict[str, Any]] = []
    fixed_items: List[Dict[str, Any]] = []
    seen = set()
    for d in detected.get("detected") or []:
        norm = _normalize_merchant(d.get("merchant") or "")
        category = str(d.get("category") or "") or merchant_cats.get(norm, "")
        like = d.get("subscription_like")
        if like is None:  # 兼容旧结构（未带 subscription_like 的调用方）
            like = bool(d.get("registered")) or category == _SAVING_CATEGORY or any(
                _normalize_merchant(g.get("merchant") or "") == norm for g in registered.values())
        if not like:
            fixed_items.append({
                "merchant": str(d.get("merchant") or ""),
                "monthly_cents": int(d.get("monthly_cents") or 0),
                "category": category,
            })
            continue
        if norm in seen:
            continue
        seen.add(norm)
        monthly = int(d.get("monthly_cents") or 0)
        entries.append({
            "kind": "subscription",
            "title": str(d.get("merchant") or ""),
            "monthly_cents": monthly,
            "detail": "连续 %d 次自动扣费，最近一次 %s 扣款 %s" % (
                int(d.get("occurrences") or 0), str(d.get("last_date") or ""), yuan(monthly)),
            "action": {"kind": "subscription_cancel", "merchant": str(d.get("merchant") or "")},
            "registered": bool(d.get("registered")),
            "sub_id": str(d.get("sub_id") or ""),
        })
    for norm, row in registered.items():
        if norm in seen:
            continue
        seen.add(norm)
        monthly = int(row.get("amount_cents") or 0)
        entries.append({
            "kind": "subscription",
            "title": str(row.get("merchant") or ""),
            "monthly_cents": monthly,
            "detail": "登记中的每月订阅，每月扣款 %s" % yuan(monthly),
            "action": {"kind": "subscription_cancel", "merchant": str(row.get("merchant") or "")},
            "registered": True,
            "sub_id": str(row.get("sub_id") or ""),
        })
    entries.sort(key=lambda x: (x["registered"], -int(x["monthly_cents"]), x["title"]))
    potential = 0
    for item in entries:
        potential += int(item["monthly_cents"])
        savings.append({
            "id": "sv%d" % (len(savings) + 1),
            "kind": "subscription",
            "title": item["title"],
            "monthly_cents": int(item["monthly_cents"]),
            "yearly_cents": int(item["monthly_cents"]) * 12,
            "detail": item["detail"],
            "action": item["action"],
        })

    # 2) 一条分类优化建议（若该分类本月超预算，按超支金额；否则按压缩 10% 估算）
    category_title = ""
    category_monthly = 0
    category_detail = ""
    over_map: Dict[str, int] = {}
    try:
        budget_data = mock_core.budget_analysis(conn, user_id) or {}
        for item in budget_data.get("over_budget") or []:
            over_map[str(item.get("category") or "")] = int(item.get("spent_cents") or 0) - int(item.get("budget_cents") or 0)
    except Exception:  # noqa: BLE001 - 预算分析失败时只用压缩估算
        over_map = {}
    preferred = [c for c in top_categories if str(c.get("category") or "") in over_map]
    if not preferred:
        preferred = [c for c in top_categories if str(c.get("category") or "") not in ("居住", _SAVING_CATEGORY)]
    target = (preferred or top_categories or [None])[0]
    if target:
        category_title = str(target.get("category") or "")
        spent = int(target.get("cents") or 0)
        if category_title in over_map:
            category_monthly = max(0, over_map[category_title])
            category_detail = "近 30 天「%s」已超预算 %s，压回预算内即可省下" % (category_title, yuan(category_monthly))
        else:
            category_monthly = max(100, spent // 10)
            category_detail = "近 30 天「%s」支出 %s，减少 10%% 约省 %s/月" % (category_title, yuan(spent), yuan(category_monthly))
        if category_monthly > 0:
            savings.append({
                "id": "sv%d" % (len(savings) + 1),
                "kind": "category",
                "title": "「%s」支出优化" % category_title,
                "monthly_cents": int(category_monthly),
                "yearly_cents": int(category_monthly) * 12,
                "detail": category_detail,
                "action": {"kind": "query_bills", "category": category_title},
            })
    fixed_items.sort(key=lambda x: -int(x["monthly_cents"]))
    fixed_cycles = {
        "count": len(fixed_items),
        "monthly_cents": int(sum(int(x["monthly_cents"]) for x in fixed_items)),
        "items": fixed_items,
    }
    return savings, int(potential), fixed_cycles


def spending_insights(conn, user_id: str) -> Dict[str, Any]:
    """生成消费洞察（只读，A1 冻结返回结构）。"""
    today = date.today()
    bill = mock_core.bill_summary(conn, user_id, 1)
    out_cents = int(bill.get("total_out_cents") or 0)
    in_cents = int(bill.get("total_in_cents") or 0)
    prev_out, prev_cats = _prev_aggregate(conn, user_id)

    top_categories: List[Dict[str, Any]] = []
    for c in (bill.get("by_category") or [])[:_TOP_CATEGORY_LIMIT]:
        category = str(c.get("category") or "")
        cents = int(c.get("amount_cents") or 0)
        prev_cents = int(prev_cats.get(category, 0))
        top_categories.append({
            "category": category,
            "cents": cents,
            "pct": _share(cents, out_cents),
            "prev_cents": prev_cents,
            "delta_pct": _pct(cents, prev_cents),
        })

    savings, potential, fixed_cycles = _savings(conn, user_id, top_categories)
    return {
        "as_of": today.isoformat(),
        "window": {"months": 1},
        "totals": {
            "out_cents": out_cents,
            "in_cents": in_cents,
            "prev_out_cents": int(prev_out),
            "delta_pct": _pct(out_cents, prev_out),
            "potential_save_monthly_cents": int(potential),
        },
        "top_categories": top_categories,
        "anomalies": _anomalies(conn, user_id),
        "budget": _budget_snapshot(conn, user_id, out_cents),
        "savings": savings,
        # v0.5.0 追加（A1 冻结字段之外的新增键）：固定周期扣费单独说明，不计入 potential
        "fixed_cycles": fixed_cycles,
    }
