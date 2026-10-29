"""ZeroStep 主动触达（outbound）：把「银行有事找你」变成一条可审计的推送。

与渠道层（inbound）对称：
- 渠道层把用户说的话翻译成运行时调用；
- 推送层把系统事件翻译成给用户看的一条消息，写审计链，可选转发到
  CHANNEL_PUSH_URL（企业微信/钉钉/飞书的机器人 webhook 都能接）。

三类模板（赛题场景）：
    subscription  订阅即将自动扣费（3 天内）
    large_txn     大额支出发生（当日最大一笔）
    repayment     信用卡还款到期

设计边界（与作品主张一致）：
- 只读数据库，绝不改动任何账务数据；
- 文案里的每个数字都来自数据库查询结果，不由模型生成；
- 推送失败只降级、不抛异常：接口仍返回 200 与可读原因；
- 每次推送都写一条审计事件 push_message（event_type 固定），
  审计链可校验、可导出。

环境变量（每次调用现读，方便演示时热切换）：
    CHANNEL_PUSH_URL        推送目标 webhook；留空表示只生成不发送
    CHANNEL_PUSH_TIMEOUT    推送超时秒数，默认 5
    PUSH_SUBSCRIPTION_DAYS  订阅提前提醒天数，默认 3
    PUSH_LARGE_TXN_CENTS    大额支出阈值（分），默认 30000（300 元）
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from . import audit, config, db, mock_core

KINDS: Tuple[str, ...] = ("subscription", "large_txn", "repayment")
DEFAULT_KIND = "subscription"

_CYCLE_CN = {"monthly": "每月", "yearly": "每年", "weekly": "每周", "quarterly": "每季度"}


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def push_url() -> str:
    return _env_str("CHANNEL_PUSH_URL")


def push_timeout() -> float:
    try:
        return max(1.0, min(float(_env_str("CHANNEL_PUSH_TIMEOUT", "5")), 30.0))
    except (TypeError, ValueError):
        return 5.0


def mask_url(url: str) -> str:
    """只暴露 scheme://host:port，路径与 query 里通常带 token，一律打码。"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return "%s://%s/***" % (parsed.scheme, parsed.netloc)
    except Exception:  # noqa: BLE001 - 打码失败不影响主流程
        pass
    return "***"


def _fmt_date(iso: str) -> str:
    text = str(iso or "")
    return text[:10] if len(text) >= 10 else text


def _days_until(iso: str) -> int:
    try:
        target = datetime.fromisoformat(str(iso)[:19])
    except (TypeError, ValueError):
        return 999
    delta = target - datetime.now()
    return int(delta.total_seconds() // 86400)


def _pick_subscription(conn, user_id: str) -> Dict[str, Any]:
    """挑最紧要的一笔订阅：3 天内要扣费的优先，否则挑最近的一笔。"""
    days_ahead = max(0, _env_int("PUSH_SUBSCRIPTION_DAYS", 3))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND status='active' ORDER BY next_charge ASC", (user_id,)
    ).fetchall()]
    if not rows:
        return {}
    soon = [r for r in rows if 0 <= _days_until(r.get("next_charge", "")) <= days_ahead]
    return (soon or rows)[0]


def build_subscription(conn, user_id: str) -> Dict[str, Any]:
    sub = _pick_subscription(conn, user_id)
    if not sub:
        return {"ok": False, "code": "NO_SUBSCRIPTION", "error": "没有正在生效的订阅", "kind": "subscription",
                "title": "订阅提醒", "text": "当前没有正在续费的订阅，暂时不需要提醒。"}
    amount = mock_core.yuan(int(sub["amount_cents"]))
    cycle = _CYCLE_CN.get(str(sub.get("cycle")), str(sub.get("cycle") or "每月"))
    when = _fmt_date(sub.get("next_charge", ""))
    days = _days_until(sub.get("next_charge", ""))
    lead = "今天" if days <= 0 else ("明天" if days == 1 else "%d 天后" % days)
    title = "订阅扣费提醒：%s %s" % (sub["merchant"], amount)
    text = ("你的订阅「%s」将在 %s（%s，%s）自动扣费 %s。"
            "不再需要可以回复「取消订阅 %s」，Agent 会核对后取消并写入审计链。") % (
        sub["merchant"], when, lead, cycle, amount, sub["merchant"])
    return {"ok": True, "kind": "subscription", "title": title, "text": text,
            "data": {"sub_id": sub["sub_id"], "merchant": sub["merchant"], "amount_cents": int(sub["amount_cents"]),
                     "amount_display": amount, "cycle": sub.get("cycle"), "next_charge": sub.get("next_charge"),
                     "days_left": days}}


def build_large_txn(conn, user_id: str) -> Dict[str, Any]:
    threshold = max(1, _env_int("PUSH_LARGE_TXN_CENTS", 30000))
    since = date.today().isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE user_id=? AND direction='out' AND ts>=? ORDER BY amount_cents DESC LIMIT 1",
        (user_id, since)).fetchall()]
    window = "今天"
    # 今天的最大一笔不够阈值时，再看最近 7 天里最大的一笔（按金额判断，不硬凑窗口）
    if not rows or int(rows[0]["amount_cents"]) < threshold:
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions WHERE user_id=? AND direction='out' AND ts>=? ORDER BY amount_cents DESC LIMIT 1",
            (user_id, week_ago)).fetchall()]
        if recent and (not rows or int(recent[0]["amount_cents"]) > int(rows[0]["amount_cents"])):
            rows = recent
            window = "最近 7 天"
    if not rows:
        return {"ok": False, "code": "NO_TRANSACTION", "error": "没有可提醒的交易", "kind": "large_txn",
                "title": "大额支出提醒", "text": "最近没有支出记录，暂时不需要提醒。"}
    txn = rows[0]
    amount_cents = int(txn["amount_cents"])
    amount = mock_core.yuan(amount_cents)
    threshold_display = mock_core.yuan(threshold)
    summary = mock_core.bill_summary(conn, user_id, 1)
    total_out = int(summary.get("total_out_cents") or 0)
    ratio = (amount_cents / total_out * 100.0) if total_out else 0.0
    title = "大额支出提醒：%s %s" % (txn.get("counterparty") or "一笔支出", amount)
    if amount_cents >= threshold:
        why = "金额超过提醒阈值 %s 的%s大额支出，" % (threshold_display, window)
    else:
        why = "%s金额最大的一笔支出（未达 %s 提醒阈值），" % (window, threshold_display)
    text = ("%s你在「%s」支出 %s（%s，%s），占本月支出 %.1f%%。"
            "如非本人操作，可回复「冻结卡片」立即止付。") % (
        why, txn.get("counterparty") or "商户", amount, _fmt_date(txn.get("ts", "")),
        txn.get("category") or "消费", ratio)
    return {"ok": True, "kind": "large_txn", "title": title, "text": text,
            "data": {"txn_id": txn.get("txn_id"), "counterparty": txn.get("counterparty"),
                     "amount_cents": amount_cents, "amount_display": amount, "category": txn.get("category"),
                     "ts": txn.get("ts"), "threshold_cents": threshold, "window": window,
                     "ratio_of_month_out": round(ratio, 2)}}


def build_repayment(conn, user_id: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM accounts WHERE user_id=? AND kind='credit' ORDER BY account_id LIMIT 1", (user_id,)
    ).fetchone()
    acc = dict(row) if row else None
    if not acc:
        return {"ok": False, "code": "NO_CREDIT_ACCOUNT", "error": "没有信用卡账户", "kind": "repayment",
                "title": "还款提醒", "text": "你还没有信用卡账户，暂时不需要还款。"}
    debt = -int(acc["balance_cents"])
    if debt <= 0:
        return {"ok": True, "kind": "repayment", "title": "还款提醒：本期已结清",
                "text": "信用卡 %s 本期没有待还欠款，无需操作。" % (acc.get("number_masked") or "****"),
                "data": {"account_id": acc.get("account_id"), "debt_cents": 0}}
    amount = mock_core.yuan(debt)
    today = date.today()
    due = today.replace(day=25)
    if due <= today:
        due = (today.replace(day=1) + timedelta(days=32)).replace(day=25)
    days = (due - today).days
    title = "还款提醒：信用卡待还 %s" % amount
    text = ("信用卡 %s 当前待还 %s，最晚 %s 还款（还有 %d 天）。"
            "回复「还信用卡 %s」即可从工资卡还款（也可说「还信用卡 %s 元，用储蓄卡」，指定出账账户），"
            "需要二次确认并写入审计链。") % (
        acc.get("number_masked") or "****", amount, due.isoformat(), days, amount, amount)
    return {"ok": True, "kind": "repayment", "title": title, "text": text,
            "data": {"account_id": acc.get("account_id"), "number_masked": acc.get("number_masked"),
                     "debt_cents": debt, "debt_display": amount, "due_date": due.isoformat(), "days_left": days}}


def build_message(conn, kind: str, user_id: str) -> Dict[str, Any]:
    kind = (kind or DEFAULT_KIND).strip().lower()
    if kind not in KINDS:
        return {"ok": False, "code": "BAD_KIND", "error": "kind 只能是 %s" % " / ".join(KINDS),
                "kind": kind, "title": "", "text": ""}
    if kind == "subscription":
        return build_subscription(conn, user_id)
    if kind == "large_txn":
        return build_large_txn(conn, user_id)
    return build_repayment(conn, user_id)


def _post_push(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """转发到渠道方 webhook。永不抛异常，返回 (ok, status, latency_ms, error)。"""
    started = time.time()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048)
            ok = 200 <= int(resp.status) < 300
            return {"ok": ok, "status": int(resp.status), "latency_ms": int((time.time() - started) * 1000),
                    "error": "" if ok else str(body[:160], "utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": int(exc.code), "latency_ms": int((time.time() - started) * 1000),
                "error": "HTTP %d" % int(exc.code)}
    except urllib.error.URLError as exc:
        return {"ok": False, "status": 0, "latency_ms": int((time.time() - started) * 1000),
                "error": "推送地址不可达（%s）" % getattr(exc, "reason", exc)}
    except Exception as exc:  # noqa: BLE001 - 推送失败绝不能影响主流程
        return {"ok": False, "status": 0, "latency_ms": int((time.time() - started) * 1000),
                "error": "推送失败：%s" % exc}


def push(conn, kind: str = DEFAULT_KIND, user_id: str = "") -> Dict[str, Any]:
    """生成一条触达消息，写审计，可选推送。始终返回 200 可用的扁平 JSON。"""
    user_id = (user_id or "").strip() or config.DEFAULT_USER_ID
    built = build_message(conn, kind, user_id)
    url = push_url()
    timeout = push_timeout()
    channel = {"configured": bool(url), "url_masked": mask_url(url), "status": "skipped",
               "latency_ms": 0, "error": ""}
    pushed = False
    delivered = False
    if built.get("ok") and url:
        pushed = True
        result = _post_push(url, {"user_id": user_id, "kind": built.get("kind"), "title": built.get("title"),
                                  "text": built.get("text")}, timeout)
        channel["latency_ms"] = result["latency_ms"]
        channel["status"] = "delivered" if result["ok"] else "failed"
        channel["error"] = result["error"]
        delivered = bool(result["ok"])
    elif built.get("ok"):
        channel["status"] = "not_configured"
    else:
        channel["status"] = "skipped"

    ts = db.now_iso()
    audit_row = audit.append(
        conn, event_type="push_message", session_id="", tool="push",
        args={"kind": built.get("kind"), "user_id": user_id, "channel_configured": bool(url)},
        result={"title": built.get("title", ""), "text": built.get("text", ""),
                "pushed": pushed, "delivered": delivered, "channel": channel},
        risk_level="low", actor="system")

    out = {"ok": bool(built.get("ok")), "kind": built.get("kind") or kind, "user_id": user_id,
           "title": built.get("title", ""), "text": built.get("text", ""),
           "pushed": pushed, "delivered": delivered, "channel": channel,
           "audit_seq": audit_row.get("seq"), "ts": ts}
    if not built.get("ok"):
        out["error"] = built.get("error", "")
        out["code"] = built.get("code", "")
    return out
