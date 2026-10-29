"""Agent 编排核心：意图 -> 计划 -> 策略门 -> 执行 -> 校验 -> 回复。

这是一个可持久化、可中断/恢复的状态机。审批时把状态写入 checkpoints，
用户确认后从同一 thread 恢复执行；每个工具调用与决策都写入哈希链审计。
"""
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import approvals, audit, config, db, insight, llm, locks, mock_core, nlu, policy, totp
from .mock_core import DomainError, yuan


class EngineError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# ---------------- 会话 ----------------

def create_session(conn, user_id: str = config.DEFAULT_USER_ID) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        raise EngineError("USER_NOT_FOUND", "没有找到该用户", 404)
    session_id = "s_" + secrets.token_hex(6)
    thread_id = "t_" + secrets.token_hex(8)
    now = db.now_iso()
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (session_id, user_id, thread_id, now, now))
    conn.commit()
    return {"session_id": session_id, "thread_id": thread_id, "user_id": user_id}


def get_session(conn, session_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        raise EngineError("SESSION_NOT_FOUND", "会话不存在，请刷新页面重新开始", 404)
    return dict(row)


def session_snapshot(conn, session_id: str) -> Dict[str, Any]:
    sess = get_session(conn, session_id)
    user = dict(conn.execute("SELECT * FROM users WHERE user_id=?", (sess["user_id"],)).fetchone())
    pending = approvals.pending_for_session(conn, session_id)
    return {
        "session_id": session_id, "user": user,
        "accounts": mock_core.list_accounts(conn, sess["user_id"]),
        "cards": mock_core.list_cards(conn, sess["user_id"]),
        "subscriptions": mock_core.list_subscriptions(conn, sess["user_id"])["subscriptions"],
        "payees": mock_core.list_payees(conn, sess["user_id"]),
        "wealth": mock_core.list_wealth(conn, sess["user_id"]),
        "pending_approval": serialize_approval(pending) if pending else None,
        "llm_mode": llm.effective(),
        "llm_status": llm.status(),
        "demo_mode": config.DEMO_MODE,
    }


# ---------------- 事件 ----------------

class Emitter:
    def __init__(self, conn, session_id: str, sink: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.conn = conn
        self.session_id = session_id
        self.sink = sink
        self.events: List[Dict[str, Any]] = []
        row = conn.execute("SELECT COALESCE(MAX(seq),0) AS n FROM events WHERE session_id=?", (session_id,)).fetchone()
        self.seq = int(row["n"])

    def emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        # seq 是「会话内的自增号」，读-算-写必须原子：两个并发请求算到同一个 seq 会撞
        # UNIQUE(events.session_id, events.seq) 直接 500。已在调用方事务里就沿用调用方事务，
        # 否则自己开 BEGIN IMMEDIATE，被别的写事务占用时退避重试。
        own = not self.conn.in_transaction
        for attempt in range(6):
            try:
                if own:
                    self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS n FROM events WHERE session_id=?",
                                        (self.session_id,)).fetchone()
                self.seq = int(row["n"]) + 1
                self.conn.execute("INSERT INTO events (session_id, seq, type, payload_json, ts) VALUES (?,?,?,?,?)",
                                  (self.session_id, self.seq, event_type, json.dumps(payload, ensure_ascii=False), db.now_iso()))
                if own:
                    self.conn.commit()
                break
            except sqlite3.Error:
                if own and self.conn.in_transaction:
                    self.conn.rollback()
                if attempt == 5:
                    raise
                time.sleep(0.005 * (attempt + 1))
        item = {"seq": self.seq, "type": event_type, "payload": payload}
        self.events.append(item)
        if self.sink:
            self.sink(event_type, payload)


def mask_account(no: Any) -> str:
    """账户/卡号脱敏：仅保留前 4 位与后 4 位，中间固定掩码。"""
    digits = str(no or "").strip()
    if not digits:
        return ""
    if len(digits) <= 8:
        return digits[:2] + " **** " + digits[-2:]
    return digits[:4] + " **** " + digits[-4:]


def serialize_approval(ap: Dict[str, Any], preview: str = "", title: str = "") -> Dict[str, Any]:
    params = ap.get("params") or {}
    return {
        "approval_id": ap["approval_id"], "risk_level": ap["risk_level"], "challenge": {"type": ap["challenge"]},
        "title": title or params.get("title", ""), "amount_cents": params.get("amount_cents", 0),
        "currency": "CNY", "counterparty": params.get("counterparty", ""), "reasons": ap.get("reasons", []),
        "preview": preview or params.get("preview", ""), "expires_at": ap["expires_at"], "nonce": ap["nonce"],
        "counterparty_account_masked": params.get("counterparty_account_masked", ""),
        "payer_account_masked": params.get("payer_account_masked", ""),
        "source": params.get("source", "rule"), "source_detail": params.get("source_detail", ""),
        "conflicts": params.get("conflicts", []),
        "hits": params.get("hits", []),
        # v0.4.1：红色动作可选的强验证因子（totp / sms / face），前端据此渲染选择器
        "factors": params.get("factors", []),
    }


# ---------------- 计划构建 ----------------

def _step(step_id: str, title: str, tool: str, args: Dict[str, Any], risk: str = "low") -> Dict[str, Any]:
    return {"step_id": step_id, "title": title, "tool": tool, "args": args, "risk": risk}


def _account_of(conn, user_id: str, kind: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind=? ORDER BY account_id LIMIT 1", (user_id, kind)).fetchone()
    return dict(row) if row else None


def _mask_phone(phone: str) -> str:
    p = str(phone or "")
    return "手机号 ****" + p[-4:] if len(p) >= 4 else "手机号 ****"


def _resolve_payee(conn, user_id: str, slots: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    payees = mock_core.list_payees(conn, user_id)
    name = slots.get("payee_name") or ""
    phone = str(slots.get("payee_phone") or "").strip()
    if phone:
        # 手机号优先：命中通讯录里的手机号才算熟客，否则按新收款人处理（policy 会升级到高风险）
        try:
            hit = mock_core.find_payee_by_phone(conn, user_id, phone)
        except Exception:  # noqa: BLE001 - 数据层未就绪时退化为本地匹配
            hit = None
        if hit:
            return hit, False
        for p in payees:
            if str(p.get("phone") or "") == phone:
                return p, False
        if name:
            for p in payees:
                if p["name"] == name or name in p["name"]:
                    return p, False
        return None, True
    for p in payees:
        if name and (p["name"] == name or name in p["name"]):
            return p, False
    if name:
        return None, True
    return None, False


def _next_wednesday() -> str:
    now = datetime.now()
    days = (2 - now.weekday()) % 7 or 7
    d = (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    return d.isoformat(timespec="seconds")


def build_plan(conn, user_id: str, intent: Dict[str, Any], text: str) -> Dict[str, Any]:
    name = intent["name"]
    slots = dict(intent.get("slots") or {})
    steps: List[Dict[str, Any]] = []
    planner_block: Optional[Dict[str, str]] = None
    check_acc = _account_of(conn, user_id, "checking")
    from_acc = _account_of(conn, user_id, slots.get("from_kind") or "checking") or check_acc

    if name == "query_bills":
        steps.append(_step("s1", "汇总近 %d 个月账单" % int(slots.get("months") or 1), "get_bill_summary",
                           {"months": int(slots.get("months") or 1), "category": slots.get("category")}))
    elif name == "query_balance":
        steps.append(_step("s1", "查询名下账户余额", "get_accounts", {}))
    elif name == "query_transactions":
        steps.append(_step("s1", "读取最近交易流水", "get_recent_transactions", {"limit": 30}))
    elif name == "budget_plan":
        steps.append(_step("s1", "对比预算与实际支出", "budget_analysis", {}))
    elif name == "wealth_query":
        steps.append(_step("s1", "查看理财产品与持仓", "list_wealth", {}))
    elif name == "wealth_purchase":
        amount = int(slots.get("amount_cents") or 0)
        pid = slots.get("product_id") or "W001"
        prods = {p["product_id"]: p["name"] for p in mock_core.list_wealth(conn, user_id)["products"]}
        pname = slots.get("product_name") or prods.get(pid, "")
        steps.append(_step("s1", "申购「%s」%s" % (pname or "理财产品", yuan(amount)), "purchase_wealth",
                           {"product_id": pid, "amount_cents": amount, "product_name": pname}))
    elif name == "query_fx":
        frm = str(slots.get("from_currency") or "CNY").upper()
        to = str(slots.get("to_currency") or "USD").upper()
        amount = int(slots.get("amount_cents") or 0)
        steps.append(_step("s1", "查询 %s→%s 汇率" % (frm, to), "fx_convert",
                           {"from_currency": frm, "to_currency": to, "amount_cents": amount}))
    elif name == "card_query":
        steps.append(_step("s1", "查看银行卡列表", "list_cards", {}))
    elif name == "card_apply":
        steps.append(_step("s1", "申请虚拟卡", "apply_virtual_card",
                           {"daily_limit_cents": int(slots.get("daily_limit_cents") or 200000)}))
    elif name in ("card_freeze", "card_unfreeze"):
        card_id = slots.get("card_id")
        kind = slots.get("card_kind") or "credit"
        if not card_id:
            row = conn.execute("SELECT * FROM cards WHERE user_id=? AND kind=? LIMIT 1", (user_id, kind)).fetchone()
            card_id = row["card_id"] if row else None
        status = "frozen" if name == "card_freeze" else "active"
        row = conn.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone() if card_id else None
        masked = row["number_masked"] if row else ""
        kind_cn = "信用卡" if (row and row["kind"] == "credit") else ("借记卡" if row else "银行卡")
        verb = "冻结" if name == "card_freeze" else "解冻"
        steps.append(_step("s1", "%s%s %s" % (verb, kind_cn, masked), "set_card_status",
                           {"card_id": card_id, "status": status, "card_last4": masked}))
    elif name == "subscription_query":
        steps.append(_step("s1", "查看所有订阅", "list_subscriptions", {}))
    elif name == "subscription_cancel":
        steps.append(_step("s1", "取消订阅", "cancel_subscription",
                           {"sub_id": slots.get("sub_id"), "merchant": slots.get("merchant")}))
    elif name == "credit_repay":
        # v0.6.2 信用卡还款：独立意图，不走转账。出账账户默认工资卡，用户说储蓄卡则从储蓄卡扣。
        amount = int(slots.get("amount_cents") or 0)
        credit = _account_of(conn, user_id, "credit")
        payer = from_acc or check_acc
        debt = -int(credit["balance_cents"]) if credit is not None and int(credit["balance_cents"]) < 0 else 0
        if amount > 0 and debt and amount > debt:
            # 还款金额按实际欠款截断：审批卡上展示的金额必须等于真正扣款的金额
            amount = debt
        if amount > 0:
            payer_name = (payer or {}).get("name") or "工资卡"
            credit_masked = (credit or {}).get("number_masked") or "信用卡"
            steps.append(_step("s1", "从「%s」归还信用卡 %s %s" % (payer_name, credit_masked, yuan(amount)),
                               "repay_credit", {
                                   "amount_cents": amount,
                                   "from_account_id": (payer or {}).get("account_id"),
                                   "from_kind": (payer or {}).get("kind") or "checking",
                                   "credit_account_id": (credit or {}).get("account_id"),
                                   "credit_last4": credit_masked,
                               }))
    elif name == "transfer":
        payee, is_new = _resolve_payee(conn, user_id, slots)
        amount = int(slots.get("amount_cents") or 0)
        phone = str(slots.get("payee_phone") or "").strip()
        payee_name = payee["name"] if payee else (slots.get("payee_name") or (_mask_phone(phone) if phone else None))
        payee_account = (payee["account_no"] if payee else slots.get("payee_account")) or ""
        if phone and not payee_account:
            # 手机号转账：账号即手机号，掩码后作为对手方信息，避免被 policy 误判为「缺账户」
            payee_account = _mask_phone(phone)
        steps.append(_step("s1", "校验收款人信息", "get_payees", {}))
        steps.append(_step("s2", "向「%s」转账 %s" % (payee_name or "未知收款人", yuan(amount)), "create_transfer", {
            "from_account_id": from_acc["account_id"] if from_acc else None, "payee_name": payee_name,
            "payee_account": payee_account, "amount_cents": amount, "payee_is_new": is_new, "memo": text[:40],
            "internal": False,
        }))
    elif name == "cross_scenario":
        rule_parsed = nlu.parse_cross(text, {"payees": mock_core.list_payees(conn, user_id)})
        model_allocs = []
        for a in slots.get("allocations") or []:
            kind = a.get("kind")
            if kind not in ("transfer", "savings", "wealth", "credit"):
                continue
            if int(a.get("amount_cents") or 0) <= 0:
                continue
            if kind == "transfer" and not str(a.get("payee_name") or "").strip():
                continue
            model_allocs.append(a)
        parsed = {"allocations": model_allocs, "reminder": bool(slots.get("reminder"))}
        if not parsed["allocations"]:
            parsed = rule_parsed
        else:
            # 模型漏项时用规则引擎补齐同类子任务，保证组合任务不漏执行
            have = {a.get("kind") for a in parsed["allocations"]}
            for a in rule_parsed.get("allocations") or []:
                if a.get("kind") not in have:
                    parsed["allocations"].append(a)
                    have.add(a.get("kind"))
            parsed["reminder"] = bool(parsed["reminder"] or rule_parsed.get("reminder"))
        allocs = parsed.get("allocations") or []
        idx = 0
        for a in allocs:
            idx += 1
            kind = a["kind"]
            amount = int(a.get("amount_cents") or 0)
            if kind == "savings":
                steps.append(_step("s%d" % idx, "转 %s 到储蓄卡" % yuan(amount), "create_transfer", {
                    "from_account_id": check_acc["account_id"] if check_acc else None, "payee_name": "储蓄卡",
                    "payee_account": "本人账户", "amount_cents": amount, "payee_is_new": False,
                    "memo": "自动分账-储蓄", "internal": True}))
            elif kind == "wealth":
                hint = a.get("hint") or "稳健"
                pid = {"稳利宝": "W001", "增利宝": "W002", "指数": "W003", "货币": "W004", "稳健": "W001", "理财": "W001", "基金": "W003"}.get(hint, "W001")
                prods = {p["product_id"]: p["name"] for p in mock_core.list_wealth(conn, user_id)["products"]}
                steps.append(_step("s%d" % idx, "申购理财 %s" % yuan(amount), "purchase_wealth",
                                   {"product_id": pid, "amount_cents": amount, "product_name": prods.get(pid, hint)}))
            elif kind == "credit":
                credit_acc = _account_of(conn, user_id, "credit")
                steps.append(_step("s%d" % idx, "还信用卡 %s" % yuan(amount), "repay_credit", {
                    "amount_cents": amount,
                    "from_account_id": check_acc["account_id"] if check_acc else None,
                    "from_kind": "checking",
                    "credit_account_id": (credit_acc or {}).get("account_id"),
                }))
            elif kind == "transfer":
                pname = a.get("payee_name") or ""
                if pname in ("她", "他", "对方", "那个人"):
                    pname = "妈妈" if "妈" in text else ("爸爸" if "爸" in text else ("老婆" if "老婆" in text else pname))
                payee, is_new_payee = _resolve_payee(conn, user_id, {"payee_name": pname})
                steps.append(_step("s%d" % idx, "向「%s」转账 %s" % (payee["name"] if payee else pname, yuan(amount)), "create_transfer", {
                    "from_account_id": check_acc["account_id"] if check_acc else None,
                    "payee_name": payee["name"] if payee else pname,
                    "payee_account": (payee or {}).get("account_no") or a.get("payee_account") or "",
                    "amount_cents": amount, "payee_is_new": is_new_payee if payee else bool(a.get("payee_is_new")),
                    "memo": "生日礼物" if ("生日" in text) else "分账转账", "internal": False}))
        if parsed.get("reminder"):
            idx += 1
            who = "妈妈" if "妈" in text else "对方"
            steps.append(_step("s%d" % idx, "创建生日提醒", "create_reminder", {"title": "%s 生日提醒：提前准备礼物" % who, "remind_at": _next_wednesday()}))
        if "预算" in text:
            idx += 1
            steps.append(_step("s%d" % idx, "生成旅行预算建议", "budget_analysis", {}))
        if not allocs:
            amount = int(slots.get("amount_cents") or 0)
            if amount > 0:
                steps.append(_step("s1", "转账 %s" % yuan(amount), "create_transfer", {
                    "from_account_id": from_acc["account_id"] if from_acc else None,
                    "payee_name": slots.get("payee_name"), "payee_account": slots.get("payee_account") or "",
                    "amount_cents": amount, "payee_is_new": True, "internal": False}))
    elif name == "wealth_redeem":
        amount = int(slots.get("amount_cents") or 0)
        info = mock_core.list_wealth(conn, user_id)
        holdings = info.get("holdings") or []
        holding = None
        if slots.get("holding_id"):
            holding = next((h for h in holdings if h.get("holding_id") == slots["holding_id"]), None)
        if holding is None and slots.get("product_id"):
            holding = next((h for h in holdings if h.get("product_id") == slots["product_id"]), None)
        if holding is None and holdings:
            holding = holdings[0]
        pname = (holding or {}).get("name") or slots.get("product_name") or ""
        if amount <= 0 and holding:
            amount = int(holding.get("amount_cents") or 0)
        steps.append(_step("s1", "赎回「%s」%s" % (pname or "理财产品", yuan(amount)), "redeem_wealth",
                           {"product_id": (holding or {}).get("product_id") or slots.get("product_id") or "",
                            "holding_id": (holding or {}).get("holding_id") or "",
                            "amount_cents": amount, "product_name": pname}))
    elif name == "card_limit":
        row = None
        if slots.get("card_id"):
            row = conn.execute("SELECT * FROM cards WHERE card_id=? AND user_id=?", (slots["card_id"], user_id)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM cards WHERE user_id=? AND kind='credit' ORDER BY card_id LIMIT 1", (user_id,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM cards WHERE user_id=? ORDER BY card_id LIMIT 1", (user_id,)).fetchone()
        d = slots.get("daily_limit_cents")
        s = slots.get("single_limit_cents")
        steps.append(_step("s1", "调整银行卡限额", "set_card_limit",
                           {"card_id": row["card_id"] if row else None,
                            "card_last4": row["number_masked"] if row else "",
                            "daily_limit_cents": int(d) if d is not None else None,
                            "single_limit_cents": int(s) if s is not None else None}))
    elif name == "card_spend":
        amount = int(slots.get("amount_cents") or 0)
        kind_hint = "virtual" if ("虚拟卡" in text) else ("debit" if ("储蓄卡" in text or "借记卡" in text) else "credit")
        row = conn.execute("SELECT * FROM cards WHERE user_id=? AND kind=? ORDER BY card_id LIMIT 1", (user_id, kind_hint)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM cards WHERE user_id=? AND status='active' ORDER BY card_id LIMIT 1", (user_id,)).fetchone()
        merchant = "演示商户"
        m = re.search(r"在(.{1,12}?)(?:消费|刷卡|刷了|买东西)", text)
        if m:
            merchant = m.group(1).strip() or merchant
        steps.append(_step("s1", "在「%s」刷卡消费 %s" % (merchant, yuan(amount)), "card_spend",
                           {"card_id": row["card_id"] if row else None,
                            "card_last4": row["number_masked"] if row else "",
                            "amount_cents": amount, "merchant": merchant}))
    elif name == "anomaly_scan":
        steps.append(_step("s1", "扫描近 30 天异常交易", "scan_anomalies", {"days": 30}))
    elif name == "annual_report":
        steps.append(_step("s1", "生成 %d 年度账单报告" % int(slots.get("year") or datetime.now().year), "annual_report",
                           {"year": int(slots.get("year") or datetime.now().year)}))
    elif name == "scheduled_transfer":
        op = str(slots.get("op") or "create")
        if op == "list":
            steps.append(_step("s1", "查看预约转账", "list_scheduled_transfers", {"status": ""}))
        elif op == "cancel":
            sched_id = slots.get("sched_id") or ""
            if not sched_id:
                try:
                    data = mock_core.list_scheduled_transfers(conn, user_id, "pending") or {}
                    items = data.get("items") or []
                    sched_id = str(items[0].get("sched_id") or "") if items else ""
                except Exception:  # noqa: BLE001 - 数据层异常时按未找到处理
                    sched_id = ""
            steps.append(_step("s1", "取消预约转账", "cancel_scheduled_transfer", {"sched_id": sched_id}))
        else:
            payee, is_new = _resolve_payee(conn, user_id, slots)
            amount = int(slots.get("amount_cents") or 0)
            phone = str(slots.get("payee_phone") or "").strip()
            payee_name = payee["name"] if payee else (slots.get("payee_name") or (_mask_phone(phone) if phone else None))
            payee_account = (payee["account_no"] if payee else slots.get("payee_account")) or ""
            if phone and not payee_account:
                payee_account = _mask_phone(phone)
            run_at = slots.get("run_at") or ""
            steps.append(_step("s1", "预约 %s 向「%s」转 %s" % (run_at or "未来", payee_name or "未知收款人", yuan(amount)),
                               "create_scheduled_transfer", {
                                   "from_account_id": from_acc["account_id"] if from_acc else None,
                                   "payee_name": payee_name, "payee_account": payee_account,
                                   "amount_cents": amount, "run_at": run_at, "payee_is_new": is_new,
                                   "memo": text[:40]}))
    elif name == "insight":
        # v0.5.0 消费洞察：单步只读汇总；红线——savings 仅作建议，绝不在此自动执行任何写工具
        steps.append(_step("s1", "生成消费洞察", "spending_insights", {}))
    elif name in ("greeting", "unknown"):
        steps = []

    # 卡片 / 订阅缺失时给出明确阻塞
    if name in ("card_freeze", "card_unfreeze") and not (steps and steps[0]["args"].get("card_id")):
        planner_block = {"code": "CARD_NOT_FOUND", "message": "没有找到对应的银行卡，请确认卡种"}
    if name == "subscription_cancel" and not slots.get("sub_id") and not slots.get("merchant"):
        planner_block = {"code": "SUBSCRIPTION_NOT_FOUND", "message": "没有识别到要取消的订阅，请说出具体名称"}
    if name == "wealth_purchase" and not slots.get("amount_cents"):
        planner_block = {"code": "AMOUNT_REQUIRED", "message": "请告诉我申购金额"}
    if name == "transfer" and not slots.get("amount_cents"):
        planner_block = {"code": "AMOUNT_REQUIRED", "message": "请告诉我转账金额"}
    if name == "credit_repay" and not slots.get("amount_cents"):
        planner_block = {"code": "AMOUNT_REQUIRED", "message": "请告诉我还信用卡的金额，例如「还信用卡 8432 元」"}
    if name == "card_limit" and slots.get("daily_limit_cents") is None and slots.get("single_limit_cents") is None:
        planner_block = {"code": "LIMIT_REQUIRED", "message": "请告诉我新的限额，例如「日累计限额 5000 元」或「单笔限额 2000 元」"}
    if name == "card_spend" and not slots.get("amount_cents"):
        planner_block = {"code": "AMOUNT_REQUIRED", "message": "请告诉我刷卡金额，例如「用信用卡消费 300 元」"}
    if name == "scheduled_transfer":
        op = str(slots.get("op") or "create")
        if op == "create" and not slots.get("amount_cents"):
            planner_block = {"code": "AMOUNT_REQUIRED", "message": "请告诉我预约转账金额"}
        elif op == "create" and not slots.get("run_at"):
            planner_block = {"code": "TIME_REQUIRED", "message": "请告诉我什么时候转，例如「明天上午 9 点给妈妈转 100 元」"}
        elif op == "cancel" and not (steps and steps[0]["args"].get("sched_id")):
            planner_block = {"code": "SCHEDULED_NOT_FOUND", "message": "没有找到可以取消的预约转账"}
    if name not in ("greeting", "unknown") and not steps and not planner_block:
        planner_block = {"code": "NO_STEP", "message": "我没有理解到可执行的银行操作"}

    edges = []
    for i in range(len(steps) - 1):
        edges.append({"from": steps[i]["step_id"], "to": steps[i + 1]["step_id"]})
    return {"steps": steps, "edges": edges, "planner_block": planner_block}


# ---------------- 工具执行 ----------------

_SUSPECT_PATTERNS = [
    r"忽略(以上|之前|前面|所有).{0,16}(指令|提示|规则)",
    r"(立即|马上|赶紧).{0,6}(转账|汇款|把钱转)",
    r"然后(回复|回答).{0,6}(已完成|成功)",
    r"(验证码|密码|优盾).{0,8}(告诉|发我|给我|转发)",
]


def _scan_untrusted(obj: Any) -> Optional[str]:
    """扫描工具返回内容（不可信）里是否夹带了指令注入。"""
    dumped = json.dumps(obj, ensure_ascii=False)
    for pat in _SUSPECT_PATTERNS:
        m = re.search(pat, dumped)
        if m:
            return m.group(0)
    return None


def _sanitize_untrusted(obj: Any, hit: str) -> Any:
    """屏蔽可疑指令，并标记受影响的记录，供前端与评测观察。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = _sanitize_untrusted(v, hit)
        if any(str(v).startswith("[已屏蔽") for v in obj.values() if isinstance(v, str)):
            out["_untrusted"] = True
        return out
    if isinstance(obj, list):
        return [_sanitize_untrusted(v, hit) for v in obj]
    if isinstance(obj, str):
        cleaned = obj
        for pat in _SUSPECT_PATTERNS:
            cleaned = re.sub(pat, "[已屏蔽的可疑指令]", cleaned)
        return cleaned
    return obj


# ---------------- 外部只读服务 / 技能（延迟导入，缺失即降级） ----------------

def _mcp_client():
    """外部 MCP 客户端：模块缺失或加载失败时返回 None，主流程照常运行。"""
    try:
        from . import mcp_client
        return mcp_client
    except Exception:
        return None


def _skills_mod():
    try:
        from . import skills
        return skills
    except Exception:
        return None


def _plugin_enabled(plugin_id: str) -> bool:
    """插件开关：模块缺失或读取失败一律视为启用，保证停用插件前的默认行为不变。"""
    try:
        from . import plugins
        return bool(plugins.is_enabled(plugin_id))
    except Exception:  # noqa: BLE001 - 插件表异常绝不能改变既有安全行为
        return True


def _fx_rate_table() -> Dict[str, float]:
    table = dict(getattr(config, "MCP_FX_FALLBACK_RATE", {}) or {})
    table.setdefault("CNY", 1.0)
    return table


def _fx_fallback(from_cur: str, to_cur: str, amount_cents: int) -> Dict[str, Any]:
    """内置参考价：1 CNY = rate 目标币。外部不可用时用它兜底，只算参考值、不动钱。"""
    rates = _fx_rate_table()
    if to_cur not in rates or from_cur not in rates:
        return {"ok": False, "source": "fallback", "from": from_cur, "to": to_cur,
                "error": "内置参考价不支持该币种", "as_of": db.now_iso()}
    amount = amount_cents / 100.0
    cny = amount / rates[from_cur]
    rate = rates[to_cur] / rates[from_cur]
    converted = cny * rates[to_cur]
    return {"ok": True, "source": "fallback", "from": from_cur, "to": to_cur,
            "amount": round(amount, 2), "rate": round(rate, 6),
            "converted": round(converted, 2) if amount_cents > 0 else 0.0,
            "as_of": db.now_iso()}


def _run_fx_convert(conn, session_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """汇率换算：优先外部 fx_convert（只读），失败/关闭时回退内置参考价。"""
    from_cur = str(args.get("from_currency") or "CNY").upper()
    to_cur = str(args.get("to_currency") or "USD").upper()
    amount_cents = int(args.get("amount_cents") or 0)
    # 纯汇率查询（用户没给金额）时以 1 元作为探针金额,只为换取 rate; 回复里不展示折算金额。
    probe_cents = amount_cents if amount_cents > 0 else 100
    if not _plugin_enabled("mcp-fx"):
        # 插件被停用：不发任何外部请求，直接按内置参考价估算，审计里 source 记 disabled
        data = _fx_fallback(from_cur, to_cur, amount_cents)
        if isinstance(data, dict):
            data["source"] = "disabled"
            data["plugin_disabled"] = True
        audit.append(conn, "mcp_call", session_id=session_id, tool="fx_convert",
                     args={"from": from_cur, "to": to_cur, "amount_cents": amount_cents},
                     result={"source": "disabled", "rate": (data or {}).get("rate")}, risk_level="low")
        return data
    client = _mcp_client()
    if client is not None and getattr(config, "MCP_ENABLED", False):
        try:
            # 契约（external/fx_server.py）：{"base","quote","amount_cents"}，返回 {rate, converted_cents, base, quote, ts}
            raw = client.call_tool("fx_convert", {"base": from_cur, "quote": to_cur, "amount_cents": probe_cents})
            payload = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else {}
            if isinstance(raw, dict) and raw.get("ok") and isinstance(payload, dict) and payload.get("rate") is not None:
                rate = float(payload["rate"])
                converted_cents = payload.get("converted_cents")
                converted = float(converted_cents) / 100.0 if converted_cents is not None else round(amount_cents / 100.0 * rate, 2)
                data = {"ok": True, "source": "external",
                        "from": str(payload.get("base") or from_cur).upper(),
                        "to": str(payload.get("quote") or to_cur).upper(),
                        "amount": round(amount_cents / 100.0, 2), "rate": round(rate, 6),
                        "converted": round(converted, 2) if amount_cents > 0 else 0.0,
                        "as_of": payload.get("ts") or db.now_iso()}
                audit.append(conn, "mcp_call", session_id=session_id, tool="fx_convert",
                             args={"base": from_cur, "quote": to_cur, "amount_cents": probe_cents,
                                   "probe": amount_cents <= 0},
                             result={"source": "external", "rate": data["rate"]}, risk_level="low")
                return data
            err = str((raw or {}).get("error") or "外部汇率服务返回异常")[:120] if isinstance(raw, dict) else "外部汇率服务返回异常"
            audit.append(conn, "mcp_fallback", session_id=session_id, tool="fx_convert",
                         args={"base": from_cur, "quote": to_cur, "amount_cents": probe_cents,
                               "probe": amount_cents <= 0},
                         result={"error": err}, risk_level="low")
        except Exception as exc:  # noqa: BLE001 - 外部服务不可用必须降级，不能阻断查询
            audit.append(conn, "mcp_fallback", session_id=session_id, tool="fx_convert",
                         args={"base": from_cur, "quote": to_cur}, result={"error": str(exc)[:120]}, risk_level="low")
    data = _fx_fallback(from_cur, to_cur, amount_cents)
    audit.append(conn, "mcp_call", session_id=session_id, tool="fx_convert",
                 args={"from": from_cur, "to": to_cur, "amount_cents": amount_cents},
                 result={"source": "fallback", "rate": data.get("rate")}, risk_level="low")
    return data


def _run_risk_screen(conn, ctx: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
    """转账前反欺诈预检：外部只读服务；不可用时返回 skipped，不阻断正常转账。"""
    args = step.get("args") or {}
    out = {"tool": "risk_screen", "ok": False, "source": "skipped",
           "payee_name": args.get("payee_name") or "", "amount_cents": int(args.get("amount_cents") or 0)}
    if not _plugin_enabled("mcp-risk"):
        # 插件被停用：跳过外部核查（只减少外部依赖，不放松 policy 自身的安全规则）
        out.update({"ok": False, "source": "disabled",
                    "reason": "外部反欺诈核查未执行（插件已停用）"})
        audit.append(conn, "mcp_call", session_id=ctx.get("session_id", ""), tool="risk_screen",
                     args={"payee_name": out["payee_name"], "amount_cents": out["amount_cents"]},
                     result={"source": "disabled", "ok": False,
                             "reason": "外部反欺诈核查未执行（插件已停用）"}, risk_level="medium")
        return out
    client = _mcp_client()
    if client is None or not getattr(config, "MCP_ENABLED", False):
        return out
    try:
        # 契约（external/risk_server.py）：{"payee_name","payee_account_tail","amount_cents"}
        # 返回 {hit, list_name, score, reasons, payee_name, payee_account_tail, amount_cents, ts}
        raw = client.call_tool("risk_screen", {"payee_name": out["payee_name"],
                                               "payee_account_tail": str(args.get("payee_account") or "")[-4:],
                                               "amount_cents": out["amount_cents"]})
        payload = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else {}
        if isinstance(raw, dict) and raw.get("ok") and isinstance(payload, dict):
            out.update({"ok": True, "source": "external",
                        "hit": bool(payload.get("hit")),
                        "score": int(payload.get("score") or 0),
                        "list_name": str(payload.get("list_name") or ""),
                        "reasons": [str(r) for r in (payload.get("reasons") or [])][:5],
                        "data": payload})
        else:
            err = str((raw or {}).get("error") or "外部风控服务返回异常")[:120] if isinstance(raw, dict) else "外部风控服务返回异常"
            out.update({"ok": False, "source": "degraded", "error": err})
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "source": "fallback", "error": str(exc)[:120]})
    audit.append(conn, "mcp_call", session_id=ctx.get("session_id", ""), tool="risk_screen",
                 args={"payee_name": out["payee_name"], "amount_cents": out["amount_cents"]},
                 result={"source": out["source"], "ok": out["ok"]}, risk_level="medium")
    return out


def _pre_execute_guard(conn, ctx: Dict[str, Any], step: Dict[str, Any]) -> None:
    """真实动钱之前的外部风控闸门：外部判定高风险/命中名单时直接拦截（只加严，不放松）。"""
    if step.get("tool") != "create_transfer":
        return
    screening = _run_risk_screen(conn, ctx, step)
    ctx.setdefault("mcp_calls", []).append(screening)
    if str(screening.get("source") or "") == "disabled":
        # 外部核查被插件停用：不拦截，但要在回复里备注（policy 安全规则仍然生效）
        ctx["risk_plugin_disabled"] = True
        return
    payload = screening.get("data") or {}
    level = str(payload.get("risk_level") or payload.get("riskLevel") or "").lower()
    score = int(payload.get("score") or 0)
    threshold = int(getattr(config, "MCP_RISK_BLOCK_SCORE", 60))
    if screening.get("ok") and (payload.get("blocked") or payload.get("blocked_reason") or level == "high"):
        raise DomainError("RISK_BLOCKED", "反欺诈服务判定该笔转账风险过高，已拦截，未发生资金变动")
    if screening.get("ok") and (payload.get("list_name") or score >= threshold):
        raise DomainError("RISK_BLOCKED", "反欺诈服务命中风险名单（风险分 %d，阈值 %d），已拦截，未发生资金变动" % (score, threshold))


def _exec_tool(conn, user_id: str, session_id: str, thread_id: str, tool: str, args: Dict[str, Any], idem: str,
               approval_id: str = "") -> Dict[str, Any]:
    if tool == "get_accounts":
        return {"accounts": mock_core.list_accounts(conn, user_id), "total_cents": mock_core.account_total(conn, user_id)}
    if tool == "get_recent_transactions":
        return {"transactions": mock_core.list_transactions(conn, user_id, limit=int(args.get("limit") or 30))}
    if tool == "get_bill_summary":
        data = mock_core.bill_summary(conn, user_id, int(args.get("months") or 1))
        if args.get("category"):
            data["filtered_category"] = [c for c in data["by_category"] if c["category"] == args["category"]]
        return data
    if tool == "budget_analysis":
        return mock_core.budget_analysis(conn, user_id)
    if tool == "list_wealth":
        return mock_core.list_wealth(conn, user_id)
    if tool == "get_payees":
        return {"payees": mock_core.list_payees(conn, user_id)}
    if tool == "list_cards":
        return {"cards": mock_core.list_cards(conn, user_id)}
    if tool == "list_subscriptions":
        return mock_core.list_subscriptions(conn, user_id)
    if tool == "fx_convert":
        return _run_fx_convert(conn, session_id, args)
    if tool == "create_transfer":
        return mock_core.create_transfer(conn, user_id, session_id, thread_id, args.get("from_account_id"),
                                         args.get("payee_name"), args.get("payee_account") or "", int(args.get("amount_cents") or 0),
                                         args.get("memo") or "", idempotency_key=idem, internal=bool(args.get("internal")))
    if tool == "purchase_wealth":
        return mock_core.purchase_wealth(conn, user_id, session_id, thread_id, args.get("product_id"), int(args.get("amount_cents") or 0))
    if tool == "repay_credit":
        return mock_core.repay_credit(conn, user_id, int(args.get("amount_cents") or 0),
                                      from_account_id=args.get("from_account_id") or "",
                                      credit_account_id=args.get("credit_account_id") or "")
    if tool == "apply_virtual_card":
        return mock_core.apply_virtual_card(conn, user_id, int(args.get("daily_limit_cents") or 200000))
    if tool == "set_card_status":
        return mock_core.set_card_status(conn, user_id, args.get("card_id"), args.get("status"))
    if tool == "cancel_subscription":
        return mock_core.cancel_subscription(conn, user_id, args.get("sub_id") or "", args.get("merchant") or "")
    if tool == "create_reminder":
        return mock_core.create_reminder(conn, user_id, args.get("title") or "提醒", args.get("remind_at") or "")
    if tool == "redeem_wealth":
        return mock_core.redeem_wealth(conn, user_id, session_id, thread_id,
                                       product_id=args.get("product_id") or "", amount_cents=int(args.get("amount_cents") or 0),
                                       holding_id=args.get("holding_id") or "")
    if tool == "set_card_limit":
        d = args.get("daily_limit_cents")
        s = args.get("single_limit_cents")
        return mock_core.set_card_limit(conn, user_id, card_id=args.get("card_id") or "",
                                        daily_limit_cents=(int(d) if d is not None else None),
                                        single_limit_cents=(int(s) if s is not None else None))
    if tool == "card_spend":
        return mock_core.card_spend(conn, user_id, session_id, thread_id, card_id=args.get("card_id") or "",
                                    amount_cents=int(args.get("amount_cents") or 0), merchant=args.get("merchant") or "")
    if tool == "scan_anomalies":
        return mock_core.scan_anomalies(conn, user_id, int(args.get("days") or 30))
    if tool == "annual_report":
        return mock_core.annual_report(conn, user_id, int(args.get("year") or 0))
    if tool == "create_scheduled_transfer":
        return mock_core.create_scheduled_transfer(
            conn, user_id, session_id, thread_id, args.get("from_account_id") or "",
            args.get("payee_name") or "", args.get("payee_account") or "",
            int(args.get("amount_cents") or 0), args.get("run_at") or "",
            memo=args.get("memo") or "", approval_id=approval_id)
    if tool == "list_scheduled_transfers":
        return mock_core.list_scheduled_transfers(conn, user_id, args.get("status") or "")
    if tool == "cancel_scheduled_transfer":
        return mock_core.cancel_scheduled_transfer(conn, user_id, args.get("sched_id") or "")
    if tool == "spending_insights":
        # v0.5.0：只读消费洞察（不改余额、不写业务表、不触发任何资金动作）
        return insight.spending_insights(conn, user_id)
    if tool == "detect_subscriptions":
        return insight.detect_subscriptions(conn, user_id, int(args.get("window_days") or 120))
    raise DomainError("TOOL_NOT_FOUND", "工具 %s 不存在" % tool)


def execute_steps(conn, ctx: Dict[str, Any], emit: Emitter) -> Dict[str, Any]:
    user_id = ctx["user_id"]
    steps = ctx["steps"]
    results: List[Dict[str, Any]] = []
    action_ids: List[str] = []
    failed: Optional[Dict[str, Any]] = None
    for step in steps:
        emit.emit("tool_start", {"step_id": step["step_id"], "tool": step["tool"],
                                 "args_preview": step["title"], "risk": step.get("risk", "low")})
        t0 = time.time()
        idem = "idem_%s_%s_%s" % (ctx["thread_id"], ctx.get("turn_key") or "t0", step["step_id"])
        try:
            _pre_execute_guard(conn, ctx, step)
            result = _exec_tool(conn, user_id, ctx["session_id"], ctx["thread_id"], step["tool"], step["args"], idem,
                                approval_id=str(ctx.get("approval_id") or ""))
            latency = int((time.time() - t0) * 1000)
            hit = _scan_untrusted(result)
            if hit:
                result = _sanitize_untrusted(result, hit)
                ctx.setdefault("untrusted", []).append({"tool": step["tool"], "matched": hit})
                audit.append(conn, "untrusted_content_blocked", session_id=ctx["session_id"], tool=step["tool"],
                             args={"matched": hit}, result={"action": "redacted"}, risk_level="high")
            risk = step.get("risk", "low")
            emit.emit("tool_end", {"step_id": step["step_id"], "tool": step["tool"], "ok": True,
                                   "latency_ms": latency, "result": result})
            audit.append(conn, "tool_result", session_id=ctx["session_id"], tool=step["tool"], args=step["args"],
                         result=result, risk_level=risk, approval_id=ctx.get("approval_id", ""))
            entry = {"step_id": step["step_id"], "tool": step["tool"], "ok": True, "result": result}
            results.append(entry)
            if step["tool"] == "create_transfer" and isinstance(result, dict) and "balance_after_cents" in result:
                chk = mock_core.verify_balance(conn, result["from_account_id"], result["balance_after_cents"])
                audit.append(conn, "verify_balance", session_id=ctx["session_id"], tool=step["tool"],
                             args={"account_id": result["from_account_id"]}, result=chk, risk_level="low",
                             approval_id=ctx.get("approval_id", ""))
                if not chk.get("ok"):
                    ctx["verify_issue"] = True
            if isinstance(result, dict) and result.get("action_id"):
                action_ids.append(result["action_id"])
        except DomainError as exc:
            latency = int((time.time() - t0) * 1000)
            emit.emit("tool_end", {"step_id": step["step_id"], "tool": step["tool"], "ok": False,
                                   "latency_ms": latency, "error": {"code": exc.code, "message": exc.message}})
            audit.append(conn, "tool_error", session_id=ctx["session_id"], tool=step["tool"], args=step["args"],
                         result={"code": exc.code, "message": exc.message}, risk_level=step.get("risk", "low"),
                         approval_id=ctx.get("approval_id", ""))
            failed = {"code": exc.code, "message": exc.message, "tool": step["tool"]}
            results.append({"step_id": step["step_id"], "tool": step["tool"], "ok": False,
                            "error": {"code": exc.code, "message": exc.message}})
            break
    return {"results": results, "action_ids": action_ids, "failed": failed}


# ---------------- 回复生成 ----------------

def _find(results: List[Dict[str, Any]], tool: str) -> Optional[Dict[str, Any]]:
    for r in results:
        if r["tool"] == tool and r.get("ok"):
            return r["result"]
    return None


def _insight_reply(data: Dict[str, Any]) -> str:
    """v0.5.0：把 spending_insights 结果写成 ≤180 字的确定性中文摘要（数字只取自工具返回）。"""
    totals = data.get("totals") or {}
    cats = data.get("top_categories") or []
    budget = data.get("budget") or {}
    savings = data.get("savings") or []
    out_cents = int(totals.get("out_cents") or 0)
    prev_cents = int(totals.get("prev_out_cents") or 0)
    delta = int(totals.get("delta_pct") or 0)
    if prev_cents <= 0:
        trend = "上期没有支出记录"
    elif delta >= 0:
        trend = "比上期多 %d%%" % delta
    else:
        trend = "比上期少 %d%%" % abs(delta)
    parts = ["近 30 天你一共支出 **%s**，%s。" % (yuan(out_cents), trend)]
    if cats:
        top = cats[0]
        parts.append("最大分类是「%s」%s（占 %d%%）。" % (str(top.get("category") or ""), yuan(int(top.get("cents") or 0)),
                                                        int(top.get("pct") or 0)))
    monthly = int(totals.get("potential_save_monthly_cents") or 0)
    yearly = sum(int(s.get("yearly_cents") or 0) for s in savings if str(s.get("kind") or "") == "subscription")
    if monthly > 0:
        parts.append("每月可省约 %s（年化 %s），主要来自订阅。" % (yuan(monthly), yuan(yearly)))
    else:
        parts.append("暂未发现可以取消的周期性订阅。")
    # 固定周期扣费（房租/还款/水电等）单列说明，不计入上面的可省金额
    fixed = data.get("fixed_cycles") or {}
    fixed_count = int(fixed.get("count") or 0)
    if fixed_count > 0:
        parts.append("另有 %d 项固定周期扣费（房租/还款等），不计入可省金额。" % fixed_count)
    projected = int(budget.get("projected_cents") or 0)
    budget_cents = int(budget.get("budget_cents") or 0)
    over = int(budget.get("over_cents") or 0)
    if over > 0:
        parts.append("按当前节奏预计支出 %s，预算 %s，预计超支 %s。" % (yuan(projected), yuan(budget_cents), yuan(over)))
    else:
        parts.append("按当前节奏预计支出 %s，预算 %s，还剩 %s。" % (yuan(projected), yuan(budget_cents),
                                                          yuan(max(0, budget_cents - projected))))
    return "".join(parts)


def build_reply(intent: Dict[str, Any], results: List[Dict[str, Any]], action_ids: List[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
    name = intent["name"]
    slots = intent.get("slots") or {}
    undo = None
    parts: List[str] = []
    if name == "query_bills":
        d = _find(results, "get_bill_summary") or {}
        cats = d.get("by_category") or []
        top = cats[0] if cats else None
        parts.append("近 %d 个月你一共支出 **%s**，收入 %s。" % (d.get("months", 1), d.get("total_out_display", "¥0.00"), yuan(d.get("total_in_cents", 0))))
        if top:
            parts.append("花得最多的是「%s」，%s，占 %.0f%%。" % (top["category"], yuan(top["amount_cents"]), top["ratio"] * 100))
            if len(cats) > 1:
                parts.append("其次是「%s」%s。" % (cats[1]["category"], yuan(cats[1]["amount_cents"])))
        if slots.get("category"):
            filtered = d.get("filtered_category") or []
            if filtered:
                parts.append("其中「%s」共 %s。" % (slots["category"], yuan(filtered[0]["amount_cents"])))
    elif name == "query_balance":
        d = _find(results, "get_accounts") or {}
        accs = d.get("accounts") or []
        parts.append("你名下共 %d 个账户，合计 %s（不含信用卡负债）。" % (len(accs), yuan(d.get("total_cents", 0))))
        for a in accs[:3]:
            parts.append("%s %s：%s。" % (a["name"], a["number_masked"], yuan(a["balance_cents"])))
    elif name == "query_transactions":
        d = _find(results, "get_recent_transactions") or {}
        txns = d.get("transactions") or []
        parts.append("最近 %d 笔交易（展示最新 %d 笔）：" % (len(txns), min(6, len(txns))))
        for tx in txns[:6]:
            direction = "收入" if tx.get("direction") == "in" else "支出"
            parts.append("· %s %s %s %s" % (str(tx.get("ts", ""))[:10], direction, yuan(int(tx.get("amount_cents") or 0)), tx.get("counterparty", "")))
        untrusted = [tx for tx in txns if tx.get("_untrusted")]
        if untrusted:
            parts.append("其中 %d 笔交易的备注里有可疑指令，已按不可信内容处理。" % len(untrusted))
    elif name == "budget_plan":
        d = _find(results, "budget_analysis") or {}
        parts.append("本月支出 %s，储蓄率 %.1f%%。" % (d.get("total_out_display", "¥0.00"), (d.get("savings_rate") or 0) * 100))
        for s in (d.get("suggestions") or [])[:3]:
            parts.append("· " + s)
    elif name == "wealth_query":
        d = _find(results, "list_wealth") or {}
        parts.append("你当前持有理财 %s。" % d.get("holding_total_display", "¥0.00"))
        for p in (d.get("products") or [])[:4]:
            parts.append("· %s（%s，年化 %.2f%%，起购 %s）" % (p["name"], p["risk_level"], p["annual_rate"], yuan(p["min_amount_cents"])))
    elif name == "wealth_purchase":
        d = _find(results, "purchase_wealth") or {}
        parts.append("已为你申购「%s」%s，工资卡余额变为 **%s**。" % (d.get("product_name", ""), d.get("amount_display", ""), yuan(d.get("balance_after_cents", 0))))
        parts.append("已写入审计链，60 秒内可撤销。")
        undo = {"action_id": d.get("action_id"), "kind": "wealth"}
    elif name == "card_query":
        d = _find(results, "list_cards") or {}
        parts.append("你名下共有 %d 张卡。" % len(d.get("cards") or []))
        for c in (d.get("cards") or []):
            parts.append("· %s %s（%s）" % (c["kind"], c["number_masked"], "正常" if c["status"] == "active" else "已冻结"))
    elif name == "card_apply":
        d = _find(results, "apply_virtual_card") or {}
        parts.append("虚拟卡已开通：%s，单日限额 %s，可随时冻结。已写入审计链。" % (d.get("number_masked", ""), d.get("daily_limit_display", "")))
    elif name in ("card_freeze", "card_unfreeze"):
        d = _find(results, "set_card_status") or {}
        parts.append("已%s卡 %s，当前状态：%s。" % ("冻结" if name == "card_freeze" else "解冻", d.get("number_masked", ""), d.get("status")))
    elif name == "subscription_query":
        d = _find(results, "list_subscriptions") or {}
        parts.append("你有 %d 个进行中的订阅，每月合计 **%s**（每年 %s）。" % (d.get("active_count", 0), d.get("monthly_total_display", ""), yuan(d.get("yearly_total_cents", 0))))
        for s in (d.get("subscriptions") or [])[:5]:
            parts.append("· %s ¥%.2f/月（下次扣费 %s）" % (s["merchant"], s["amount_cents"] / 100.0, s["next_charge"]))
    elif name == "subscription_cancel":
        d = _find(results, "cancel_subscription") or {}
        parts.append("已取消「%s」订阅，每月节省 %s。" % (d.get("merchant", ""), d.get("saved_monthly_display", "")))
    elif name == "transfer":
        d = _find(results, "create_transfer") or {}
        parts.append("已向「%s」转账 **%s**，账户余额变为 %s。" % (d.get("payee_name", ""), d.get("amount_display", ""), d.get("balance_after_display", "")))
        parts.append("已写入审计链，60 秒内可撤销。")
        undo = {"action_id": d.get("action_id"), "kind": "transfer"}
    elif name == "credit_repay":
        d = _find(results, "repay_credit") or {}
        payer = d.get("from_account_name") or "工资卡"
        parts.append("已从「%s」扣款 **%s** 归还信用卡，信用卡当前待还 %s。" % (
            payer, d.get("paid_display", ""), yuan(int(d.get("debt_after_cents") or 0))))
        if int(d.get("debt_after_cents") or 0):
            parts.append("剩余待还 %s，可继续还款。" % yuan(int(d.get("debt_after_cents") or 0)))
        else:
            parts.append("信用卡欠款已结清。")
        parts.append("已写入审计链；信用卡还款到账后不可撤销。")
    elif name == "cross_scenario":
        parts.append("已按你的要求完成：")
        for r in results:
            if not r.get("ok"):
                continue
            res = r["result"]
            if r["tool"] == "create_transfer":
                parts.append("· 转 %s 给「%s」，余额 %s" % (res.get("amount_display", ""), res.get("payee_name", ""), res.get("balance_after_display", "")))
            elif r["tool"] == "purchase_wealth":
                parts.append("· 申购「%s」%s" % (res.get("product_name", ""), res.get("amount_display", "")))
            elif r["tool"] == "repay_credit":
                parts.append("· 还信用卡 %s，剩余待还 %s" % (res.get("paid_display", ""), yuan(res.get("debt_after_cents", 0))))
            elif r["tool"] == "create_reminder":
                parts.append("· 已创建提醒：%s（%s）" % (res.get("title", ""), res.get("remind_at", "")[:10]))
        parts.append("全部动作已写入审计链。")
        if action_ids:
            undo = {"action_id": action_ids[0], "kind": "transfer"}
    elif name == "query_fx":
        d = _find(results, "fx_convert") or {}
        frm = str(d.get("from") or slots.get("from_currency") or "CNY").upper()
        to = str(d.get("to") or slots.get("to_currency") or "USD").upper()
        rate = d.get("rate")
        amount = float(d.get("amount") or 0)
        converted = float(d.get("converted") or 0)
        disabled = str(d.get("source") or "") == "disabled"
        if rate is None:
            prefix = "汇率插件已停用，" if disabled else ""
            parts.append(prefix + "暂时查不到 %s 对 %s 的汇率，请稍后再试。" % (frm, to))
        else:
            rate_txt = ("%.6f" % float(rate)).rstrip("0").rstrip(".")
            pair = "%s %s ≈ %s %s，" % ("{:,.2f}".format(amount), frm, "{:,.2f}".format(converted), to) if amount > 0 else ""
            as_of = str(d.get("as_of") or "")[:19].replace("T", " ")
            if disabled:
                parts.append("汇率插件已停用，按内置参考价 %s 估算：%s1 %s = %s %s。" % (rate_txt, pair, frm, rate_txt, to))
            elif d.get("source") == "external":
                parts.append("按外部实时汇率（fx_convert）：%s1 %s = %s %s，报价时间 %s。" % (pair, frm, rate_txt, to, as_of))
            else:
                parts.append("外部汇率服务暂不可用，按内置参考价 %s 估算：%s1 %s = %s %s。" % (rate_txt, pair, frm, rate_txt, to))
            parts.append("仅供参考，汇价随市场波动，实际成交价以柜面为准。")
    elif name == "wealth_redeem":
        d = _find(results, "redeem_wealth") or {}
        parts.append("已赎回「%s」%s，资金已回到活期账户，余额变为 **%s**。"
                     % (d.get("product_name", ""), d.get("amount_display", ""), d.get("balance_after_display", "")))
        parts.append("已写入审计链。")
    elif name == "card_limit":
        d = _find(results, "set_card_limit") or {}
        after = d.get("after") or {}
        parts.append("已调整卡 %s 的限额：日累计 %s，单笔 %s，调整后立即生效。"
                     % (d.get("number_masked", ""),
                        yuan(int(after.get("daily_limit_cents") or 0)),
                        yuan(int(after.get("single_limit_cents") or 0))))
        parts.append("已写入审计链，可随时再改回。")
    elif name == "card_spend":
        d = _find(results, "card_spend") or {}
        parts.append("已在「%s」用它消费 **%s**，今天已用 %s / 日限额 %s，剩余可用 %s。"
                     % (d.get("merchant", ""), d.get("amount_display", ""),
                        yuan(int(d.get("daily_used_cents") or 0)), yuan(int(d.get("daily_limit_cents") or 0)),
                        yuan(int(d.get("remaining_cents") or 0))))
        parts.append("已写入审计链。")
    elif name == "anomaly_scan":
        d = _find(results, "scan_anomalies") or {}
        items = d.get("anomalies") or []
        scanned = int(d.get("scanned_count") or 0)
        days = int(d.get("days") or 30)
        if not items:
            parts.append("我扫描了近 %d 天共 %d 笔交易，**没有发现异常交易**。单笔异常阈值 %s，超过即会提示。"
                         % (days, scanned, yuan(int(d.get("threshold_cents") or 0))))
        else:
            parts.append("我扫描了近 %d 天共 %d 笔交易，发现 **%d 笔可疑**（单笔异常阈值 %s，P99 %s）："
                         % (days, scanned, len(items), yuan(int(d.get("threshold_cents") or 0)), yuan(int(d.get("p99_cents") or 0))))
            for a in items[:6]:
                rule = str(a.get("rule") or "")
                parts.append("· %s %s 在「%s」%s（%s；规则 %s）" % (str(a.get("ts", ""))[:10], a.get("amount_display", ""),
                                                             a.get("counterparty", ""), a.get("detail", ""),
                                                             a.get("level", ""), rule or "未命名"))
            parts.append("如确认不是你本人操作，建议立刻冻结卡片并联系银行。")
    elif name == "annual_report":
        d = _find(results, "annual_report") or {}
        parts.append("%s 年度账单：收入 **%s**，支出 **%s**，结余 **%s**，储蓄率 %.1f%%，共 %d 笔交易。"
                     % (d.get("year", ""), yuan(int(d.get("total_in_cents") or 0)), yuan(int(d.get("total_out_cents") or 0)),
                        yuan(int(d.get("net_cents") or 0)), float(d.get("savings_rate") or 0) * 100, int(d.get("txn_count") or 0)))
        cats = d.get("by_category") or []
        if cats:
            parts.append("花得最多的三类：" + "、".join("%s %s（%.0f%%）" % (c.get("category", ""), yuan(int(c.get("amount_cents") or 0)),
                                                                  float(c.get("ratio") or 0) * 100) for c in cats[:3]) + "。")
        peak = d.get("peak_month") or {}
        if peak.get("month"):
            parts.append("支出最高的月份是 %s（%s）。" % (str(peak.get("month")), yuan(int(peak.get("out_cents") or 0))))
        parts.append("月均支出 %s。" % yuan(int(d.get("avg_monthly_out_cents") or 0)))
    elif name == "scheduled_transfer":
        op = str(slots.get("op") or "create")
        if op == "list":
            d = _find(results, "list_scheduled_transfers") or {}
            items = d.get("items") or []
            if not items:
                parts.append("你当前**没有**预约中的转账。")
            else:
                parts.append("你有 %d 笔预约转账：" % len(items))
                for s in items[:6]:
                    parts.append("· %s 向「%s」%s（%s）" % (str(s.get("run_at", ""))[:16].replace("T", " "),
                                                          s.get("payee_name", ""), s.get("amount_display", ""), s.get("status", "")))
        elif op == "cancel":
            d = _find(results, "cancel_scheduled_transfer") or {}
            parts.append("已取消预约转账 %s，这笔资金不会变动。已写入审计链。" % (d.get("sched_id", "")))
        else:
            d = _find(results, "create_scheduled_transfer") or {}
            parts.append("已预约在 **%s** 向「%s」转 **%s**，创建时已完成一次审批，到期由调度器自动执行，届时不再打扰你。"
                         % (str(d.get("run_at", ""))[:16].replace("T", " "), d.get("payee_name", ""), d.get("amount_display", "")))
            parts.append("已写入审计链。")
    elif name == "insight":
        # v0.5.0：确定性摘要（不含任何模型自由度）；savings 只作为建议文案，不执行
        parts.append(_insight_reply(_find(results, "spending_insights") or {}))
    elif name == "greeting":
        parts.append("你好，我是 ZeroStep 零步银行助手。你可以直接说：查账单、转账、买理财、管卡片、取消订阅，或者试试「发工资了帮我分账」。")
    else:
        parts.append("我还没理解这句话。你可以说：帮我查这个月账单 / 给妈妈转 200 元 / 申购 1000 元稳利宝 / 取消爱奇艺订阅。")
    return "".join(parts), undo


# ---------------- 主流程 ----------------

def _approval_preview(conn, user_id: str, steps: List[Dict[str, Any]]) -> Tuple[str, int, str]:
    amount = 0
    counterparty = ""
    preview = ""
    for s in steps:
        if s["tool"] == "create_transfer":
            amount += int(s["args"].get("amount_cents") or 0)
            counterparty = s["args"].get("payee_name") or counterparty
            acc = conn.execute("SELECT * FROM accounts WHERE account_id=?", (s["args"].get("from_account_id"),)).fetchone()
            if acc:
                after = int(acc["balance_cents"]) - int(s["args"].get("amount_cents") or 0)
                preview = "执行后「%s」余额将从 %s 变为 %s" % (acc["name"], yuan(int(acc["balance_cents"])), yuan(after))
        elif s["tool"] == "purchase_wealth":
            amount += int(s["args"].get("amount_cents") or 0)
            counterparty = s["args"].get("product_name") or counterparty
            acc = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
            if acc:
                after = int(acc["balance_cents"]) - int(s["args"].get("amount_cents") or 0)
                preview = "执行后工资卡余额将从 %s 变为 %s" % (yuan(int(acc["balance_cents"])), yuan(after))
        elif s["tool"] == "repay_credit":
            amount += int(s["args"].get("amount_cents") or 0)
            counterparty = counterparty or "信用卡还款"
            payer = None
            if s["args"].get("from_account_id"):
                payer = conn.execute("SELECT * FROM accounts WHERE account_id=?", (s["args"].get("from_account_id"),)).fetchone()
            if payer is None:
                payer = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind='checking' ORDER BY account_id LIMIT 1", (user_id,)).fetchone()
            if payer:
                after = int(payer["balance_cents"]) - int(s["args"].get("amount_cents") or 0)
                preview = "执行后「%s」余额将从 %s 变为 %s（信用卡还款到账后不可撤销）" % (
                    payer["name"], yuan(int(payer["balance_cents"])), yuan(after))
        elif s["tool"] == "create_scheduled_transfer":
            amount += int(s["args"].get("amount_cents") or 0)
            counterparty = s["args"].get("payee_name") or counterparty
            acc = conn.execute("SELECT * FROM accounts WHERE account_id=?", (s["args"].get("from_account_id"),)).fetchone()
            if acc:
                after = int(acc["balance_cents"]) - int(s["args"].get("amount_cents") or 0)
                preview = "到期执行后「%s」余额将从 %s 变为 %s（当前不动账）" % (acc["name"], yuan(int(acc["balance_cents"])), yuan(after))
        elif s["tool"] == "card_spend":
            amount += int(s["args"].get("amount_cents") or 0)
            counterparty = s["args"].get("merchant") or counterparty
            preview = "执行后该卡将扣款 %s，商户「%s」" % (yuan(int(s["args"].get("amount_cents") or 0)), s["args"].get("merchant") or "")
        elif s["tool"] == "redeem_wealth":
            amount += int(s["args"].get("amount_cents") or 0)
            counterparty = s["args"].get("product_name") or counterparty
            preview = "执行后「%s」%s 回到活期账户" % (s["args"].get("product_name") or "理财", yuan(int(s["args"].get("amount_cents") or 0)))
        elif s["tool"] == "set_card_limit":
            d = s["args"].get("daily_limit_cents")
            sg = s["args"].get("single_limit_cents")
            bits = []
            if d is not None:
                bits.append("日累计 %s" % yuan(int(d)))
            if sg is not None:
                bits.append("单笔 %s" % yuan(int(sg)))
            counterparty = counterparty or "银行卡限额"
            preview = "执行后该卡限额改为：%s" % ("、".join(bits) or "未变化")
    return preview, amount, counterparty


def _approval_party_account(conn, steps: List[Dict[str, Any]]) -> str:
    for s in steps:
        if s["tool"] == "create_transfer":
            return mask_account(s["args"].get("payee_account") or "")
        if s["tool"] == "purchase_wealth":
            return "理财账户"
        if s["tool"] == "set_card_status":
            row = conn.execute("SELECT number_masked FROM cards WHERE card_id=?", (s["args"].get("card_id"),)).fetchone()
            if row:
                return row["number_masked"]
        if s["tool"] == "apply_virtual_card":
            return "虚拟卡（待生成）"
        if s["tool"] in ("card_spend", "set_card_limit"):
            row = conn.execute("SELECT number_masked FROM cards WHERE card_id=?", (s["args"].get("card_id"),)).fetchone()
            if row:
                return row["number_masked"]
            return str(s["args"].get("card_last4") or "")
        if s["tool"] == "create_scheduled_transfer":
            return mask_account(s["args"].get("payee_account") or "") or "手机号转账"
        if s["tool"] == "redeem_wealth":
            return "理财账户"
        if s["tool"] == "repay_credit":
            if s["args"].get("credit_last4"):
                return str(s["args"].get("credit_last4"))
            row = conn.execute("SELECT number_masked FROM accounts WHERE kind='credit' ORDER BY account_id LIMIT 1").fetchone()
            if row:
                return row["number_masked"]
            return "信用卡"
    return ""


def _approval_payer_account(conn, steps: List[Dict[str, Any]]) -> str:
    for s in steps:
        acc_id = s["args"].get("from_account_id")
        if acc_id:
            row = conn.execute("SELECT number_masked FROM accounts WHERE account_id=?", (acc_id,)).fetchone()
            if row:
                return row["number_masked"]
        hint = s["args"].get("from_kind")
        if hint:
            row = conn.execute("SELECT number_masked FROM accounts WHERE kind=? ORDER BY account_id LIMIT 1", (hint,)).fetchone()
            if row:
                return row["number_masked"]
    return ""


def _approval_conflicts(intent: Dict[str, Any]) -> List[str]:
    """模型抽取与用户原话不一致时，把差异摆到审批卡上，让人而不是模型做最终判断。"""
    slots = intent.get("slots") or {}
    out: List[str] = []
    if slots.get("amount_adjusted"):
        out.append("模型给出的金额在原文中找不到出处，已改用原文金额")
    if slots.get("amount_clamped"):
        out.append("金额超过单笔安全上限，已按上限截断并转人工")
    return out


def _blocked_reply(blocked: Dict[str, str]) -> str:
    code = blocked.get("code", "")
    mapping = {
        "PROMPT_INJECTION": "已拦截：检测到提示注入攻击。工具返回或消息里含有「忽略指令/转账」之类的越权指令，我不会执行，也不会把它当成用户意图。",
        "CREDENTIAL_PHISHING": "已拦截：任何向你索取验证码、密码或优盾的请求都可能是诈骗。银行和客服永远不会向你索要动态验证码，请勿提供。",
        "UNAUTHORIZED_ACCESS": "已拦截：我只能操作你本人名下的账户，无法查询或操作他人的账户。",
        "SUSPICIOUS_PAYEE": "已拦截：收款人信息不明，为避免被冒充诈骗，请先核实收款人身份。",
        "LIMIT_EXCEEDED": "已拦截：单笔金额超过演示系统安全上限（5 万元），需要到柜面办理。",
        "INSUFFICIENT_FUNDS": "已拦截：账户可用余额不足，未执行任何资金操作。",
        "NEW_PAYEE_UNVERIFIED": "已拦截：首次向新收款人转账，必须先补充并核实收款人账户信息，防止冒充熟人诈骗。",
        "PAYEE_REQUIRED": "已拦截：没有识别到合法收款人，请补充收款人姓名与账户。",
        "NO_OUTSTANDING_DEBT": "已拦截：当前信用卡没有待还欠款，未执行任何资金操作。",
        "AMOUNT_INVALID": "已拦截：金额必须大于 0，未执行任何资金操作。",
        "INVALID_FROM_ACCOUNT": "已拦截：不能用信用卡还信用卡，请选择工资卡或储蓄卡作为出账账户。",
        "AMOUNT_REQUIRED": "请告诉我还款金额，例如「还信用卡 8432 元」。",
    }
    return mapping.get(code, "已拦截：" + blocked.get("message", "该操作不符合安全策略"))


def _save_checkpoint(conn, ctx: Dict[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO checkpoints (thread_id, state_json, updated_at) VALUES (?,?,?)",
                 (ctx["thread_id"], json.dumps(ctx, ensure_ascii=False), db.now_iso()))
    conn.commit()


def _load_checkpoint(conn, thread_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT state_json FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchone()
    return json.loads(row["state_json"]) if row else None


def _finalize(conn, ctx: Dict[str, Any], exec_result: Dict[str, Any], emit: Emitter) -> Dict[str, Any]:
    user_id = ctx["user_id"]
    failed = exec_result["failed"]
    results = exec_result["results"]
    if failed:
        reply = _blocked_reply(failed) if failed["code"] in (
            "INSUFFICIENT_FUNDS", "LIMIT_EXCEEDED", "NO_OUTSTANDING_DEBT", "AMOUNT_INVALID",
            "INVALID_FROM_ACCOUNT", "AMOUNT_REQUIRED") else "操作未完成：" + failed["message"]
        status = "blocked"
        undo = None
        audit.append(conn, "turn_blocked", session_id=ctx["session_id"], tool=failed.get("tool", ""),
                     result=failed, risk_level="high", approval_id=ctx.get("approval_id", ""))
    else:
        reply, undo = build_reply(ctx["intent"], results, exec_result["action_ids"])
        if ctx.get("risk_plugin_disabled") and str((ctx.get("intent") or {}).get("name") or "") in ("transfer", "cross_scenario"):
            reply += "\n（外部反欺诈核查未执行（插件已停用），policy 安全规则仍然生效）"
        status = "ok"
        # 写后回读校验在 execute_steps 每笔转账后立即完成，这里只读取结果
        if ctx.get("verify_issue"):
            reply += "（注意：回读校验发现余额不一致，已标记审计异常）"
            status = "blocked"
    skills = (ctx.get("intent") or {}).get("skills") or []
    if skills:
        audit.append(conn, "skill_matched", session_id=ctx["session_id"], tool="skill_router",
                     args={"intent": ctx["intent"]["name"]}, result={"skills": skills}, risk_level="low",
                     approval_id=ctx.get("approval_id", ""))
    if ctx.get("untrusted"):
        reply = "⚠️ 安全提示：检测到工具返回内容中含可疑指令（提示注入），已屏蔽并按不可信数据处理，不会执行其中任何转账要求。" + chr(10) + reply
        audit.append(conn, "injection_ignored", session_id=ctx["session_id"], tool="security_guard",
                     args={"hits": ctx["untrusted"]}, result={"status": "ignored"}, risk_level="high", approval_id=ctx.get("approval_id", ""))
    emit.emit("message", {"delta": reply})
    audit.append(conn, "response", session_id=ctx["session_id"], tool="respond",
                 args={"intent": ctx["intent"]["name"], "source": ctx["intent"].get("source", "rule")},
                 result={"status": status}, risk_level="low", approval_id=ctx.get("approval_id", ""))
    out = {"session_id": ctx["session_id"], "thread_id": ctx["thread_id"], "status": status, "reply": reply,
           "blocked": failed,
           "intent": ctx["intent"], "plan": {"steps": [{"step_id": s["step_id"], "title": s["title"], "tool": s["tool"], "risk": s.get("risk", "low")} for s in ctx["steps"]], "edges": ctx.get("edges", [])},
           "executed": results, "approval": None, "undo": undo,
           "policy_hits": ctx.get("policy_hits") or [], "mcp_calls": ctx.get("mcp_calls") or [],
           "audit": audit.entries(conn, ctx["session_id"], 20), "events": emit.events}
    # v0.5.0：洞察结果随 turn 返回，供 SSE / 前端「本月的三件事」卡片下推（只读）
    if str((ctx.get("intent") or {}).get("name") or "") == "insight":
        insight_data = _find(results, "spending_insights")
        if insight_data:
            out["insight"] = insight_data
    return out


def run_turn(conn, session_id: str, text: str, sink: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    sess = get_session(conn, session_id)
    user_id = sess["user_id"]
    emit = Emitter(conn, session_id, sink)
    emit.emit("session", {"session_id": session_id, "thread_id": sess["thread_id"]})
    lock_state = locks.info(conn, session_id)
    if lock_state.get("locked"):
        # v0.4.1 异常熔断：连续可疑/被拦操作达阈值后，会话在冷却期内只读拒答
        reply = "会话因连续可疑指令已被临时锁定，请在 %d 秒后再试。锁定期间不会执行任何资金动作。" % int(lock_state.get("remaining_s") or 0)
        emit.emit("security_lock", lock_state)
        emit.emit("message", {"delta": reply})
        audit.append(conn, "turn_locked", session_id=session_id, tool="security_lock", result=lock_state, risk_level="high", actor="system")
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "locked", "code": "SESSION_LOCKED", "reply": reply,
                "locked_until": lock_state.get("locked_until", ""), "lock_remaining_s": int(lock_state.get("remaining_s") or 0),
                "blocked": {"code": "SESSION_LOCKED", "message": reply}, "intent": None,
                "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}
    ctx_doc = {"payees": mock_core.list_payees(conn, user_id), "products": mock_core.list_wealth(conn, user_id)["products"],
               "subscriptions": mock_core.list_subscriptions(conn, user_id)["subscriptions"], "cards": mock_core.list_cards(conn, user_id),
               "accounts": mock_core.list_accounts(conn, user_id), "risk_level": "稳健型"}
    intent = llm.parse(text, ctx_doc)
    sk = _skills_mod()
    if sk is not None:
        try:
            intent["skills"] = list(sk.match(intent) or [])
        except Exception:  # noqa: BLE001 - 技能路由失败不影响主流程
            intent["skills"] = []
    else:
        intent["skills"] = []
    emit.emit("intent", {"name": intent["name"], "confidence": intent["confidence"], "source": intent["source"], "slots": intent["slots"],
                         "model": intent.get("model", ""), "latency_ms": intent.get("latency_ms"), "reason": intent.get("reason", "")})

    if intent["name"] == "attack":
        at = intent.get("attack_type") or "prompt_injection"
        code = {"prompt_injection": "PROMPT_INJECTION", "credential_phishing": "CREDENTIAL_PHISHING",
                "unauthorized_access": "UNAUTHORIZED_ACCESS", "suspicious_payee": "SUSPICIOUS_PAYEE"}.get(at, "PROMPT_INJECTION")
        blocked = {"code": code, "message": "检测到攻击行为：%s" % at}
        emit.emit("plan", {"steps": [], "edges": []})
        emit.emit("tool_end", {"step_id": "guard", "tool": "security_guard", "ok": False, "error": blocked})
        audit.append(conn, "attack_blocked", session_id=session_id, tool="security_guard", args={"text": text}, result=blocked, risk_level="high")
        security = locks.record_security_event(conn, session_id, user_id, "attack_blocked", blocked["message"])
        reply = _blocked_reply(blocked)
        if security.get("just_locked"):
            reply += "\n\n（%d 分钟内第 %d 次可疑操作，会话已临时锁定 %d 秒，期间不会执行任何资金动作。）" % (
                int(security.get("window_s") or 600) // 60, int(security.get("count") or 0), int(security.get("remaining_s") or 0))
            emit.emit("security_lock", security)
        emit.emit("message", {"delta": reply})
        audit.append(conn, "response", session_id=session_id, tool="respond", args={"intent": "attack"}, result={"status": "blocked"}, risk_level="high")
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "blocked", "reply": reply, "blocked": blocked,
                "intent": intent, "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "security": security, "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    plan = build_plan(conn, user_id, intent, text)
    steps = plan["steps"]
    for s in steps:
        s["risk"] = policy.max_risk("low", s.get("risk", "low"))
    emit.emit("plan", {"steps": [{"step_id": s["step_id"], "title": s["title"], "tool": s["tool"], "risk": s.get("risk", "low")} for s in steps], "edges": plan["edges"]})

    ctx = {"session_id": session_id, "thread_id": sess["thread_id"], "user_id": user_id, "intent": intent,
           "steps": steps, "edges": plan["edges"], "status": "running", "approval_id": "",
           "turn_key": "tk_" + secrets.token_hex(6)}

    if plan["planner_block"]:
        blocked = plan["planner_block"]
        audit.append(conn, "turn_blocked", session_id=session_id, tool="planner", args={"text": text}, result=blocked, risk_level="medium")
        security = locks.record_security_event(conn, session_id, user_id, "turn_blocked", blocked.get("message", ""))
        reply = _blocked_reply(blocked)
        if security.get("just_locked"):
            reply += "\n\n（%d 分钟内第 %d 次被拦操作，会话已临时锁定 %d 秒。）" % (
                int(security.get("window_s") or 600) // 60, int(security.get("count") or 0), int(security.get("remaining_s") or 0))
            emit.emit("security_lock", security)
        emit.emit("message", {"delta": reply})
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "blocked", "reply": reply, "blocked": blocked,
                "intent": intent, "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "security": security, "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    if intent["name"] in ("greeting", "unknown"):
        reply, _undo = build_reply(intent, [], [])
        emit.emit("message", {"delta": reply})
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "ok", "reply": reply, "intent": intent,
                "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    evaluation = policy.evaluate(conn, user_id, steps, intent)
    ctx["policy_hits"] = evaluation.get("hits") or []
    audit.append(conn, "policy_evaluated", session_id=session_id, tool="policy_gate",
                 args={"intent": intent["name"]}, result={"risk_level": evaluation["risk_level"],
                 "requires_approval": evaluation["requires_approval"], "hits": ctx["policy_hits"]},
                 risk_level=evaluation["risk_level"])
    for s in steps:
        s["risk"] = policy.max_risk(s.get("risk", "low"), evaluation["risk_level"] if evaluation["risk_level"] in ("low", "medium", "high") else "high")
    if evaluation["blocked"]:
        blocked = evaluation["blocked"]
        audit.append(conn, "turn_blocked", session_id=session_id, tool="policy_gate", args={"steps": steps}, result=blocked, risk_level="high")
        security = locks.record_security_event(conn, session_id, user_id, "turn_blocked", blocked.get("message", ""))
        reply = _blocked_reply(blocked)
        if security.get("just_locked"):
            reply += "\n\n（%d 分钟内第 %d 次被拦操作，会话已临时锁定 %d 秒。）" % (
                int(security.get("window_s") or 600) // 60, int(security.get("count") or 0), int(security.get("remaining_s") or 0))
            emit.emit("security_lock", security)
        emit.emit("tool_end", {"step_id": "gate", "tool": "policy_gate", "ok": False, "error": blocked})
        emit.emit("message", {"delta": reply})
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "blocked", "reply": reply, "blocked": blocked,
                "intent": intent, "plan": {"steps": steps, "edges": plan["edges"]}, "executed": [], "approval": None, "undo": None,
                "policy_hits": evaluation.get("hits") or [], "mcp_calls": [], "security": security,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    if evaluation["requires_approval"]:
        preview, amount, counterparty = _approval_preview(conn, user_id, steps)
        title = evaluation.get("summary") or (steps[0]["title"] if steps else "待确认操作")
        params = {"title": title, "amount_cents": amount, "counterparty": counterparty, "preview": preview, "steps": steps,
                  "source": intent.get("source", "rule"), "source_detail": intent.get("reason", ""),
                  "conflicts": _approval_conflicts(intent),
                  "counterparty_account_masked": _approval_party_account(conn, steps),
                  "payer_account_masked": _approval_payer_account(conn, steps),
                  "hits": ctx.get("policy_hits") or [],
                  "factors": ["totp", "sms", "face"] if evaluation["risk_level"] == "high" else []}
        ap = approvals.create(conn, session_id, sess["thread_id"], intent["name"], params, evaluation["risk_level"], evaluation["reasons"], evaluation["challenge"])
        ctx["approval_id"] = ap["approval_id"]
        ctx["status"] = "awaiting_approval"
        _save_checkpoint(conn, ctx)
        typed = serialize_approval(ap, preview=preview, title=title)
        audit.append(conn, "approval_required", session_id=session_id, tool="policy_gate", args={"steps": steps}, result={"risk_level": evaluation["risk_level"], "approval_id": ap["approval_id"]}, risk_level=evaluation["risk_level"], approval_id=ap["approval_id"])
        emit.emit("approval_required", typed)
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "awaiting_approval", "reply": "", "intent": intent,
                "plan": {"steps": steps, "edges": plan["edges"]}, "executed": [], "approval": typed, "undo": None,
                "policy_hits": ctx.get("policy_hits") or [], "mcp_calls": [],
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    exec_result = execute_steps(conn, ctx, emit)
    return _finalize(conn, ctx, exec_result, emit)


def _decide_and_audit(conn, approval_id: str, status: str, session_id: str, ap: Dict[str, Any],
                      event_type: str, result: Dict[str, Any]) -> None:
    """把审批决策（CAS）与它的审计记录放进同一个事务。

    原实现先 mark(approved) 再 audit.append，两步之间失败就会留下「审批已通过、审计没写、
    资金没动」的卡死单；并发下 audit 的 seq 竞争直接 500。现在一个事务内完成，任一失败整单回滚，
    审批单仍是 pending，用户可重试。
    """
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not approvals.mark(conn, approval_id, status, commit=False):
            raise EngineError("APPROVAL_ALREADY_DECIDED", "该审批已处理过，不能重复提交", 409)
        audit.append(conn, event_type, session_id=session_id, tool=ap["action"], args=ap["params"],
                     result=result, risk_level=ap["risk_level"], approval_id=approval_id)
        if own:
            conn.commit()
    except BaseException:
        if own and conn.in_transaction:
            conn.rollback()
        raise


def resume(conn, session_id: str, approval_id: str, decision: str, code: str, method: str = "", sink=None) -> Dict[str, Any]:
    """v0.4.1：method 指定强验证因子（totp / sms / face），缺省按 totp，保持旧调用方兼容。

    安全修复点（P1）：
    - 审批单绑定会话：check 收到 session_id，跨会话提交直接拒绝（IDOR）。
    - 参数绑定：审批时审批单里的 steps 必须与 checkpoint 里待执行的 steps 一致，
      防止「审批 A 却执行 B」。
    - 二次决策：mark 是 CAS，未命中即 409 APPROVAL_ALREADY_DECIDED，且不再写 approval_granted。
    """
    sess = get_session(conn, session_id)
    try:
        ok, err, ap = approvals.check(conn, approval_id, decision, code, sess["user_id"],
                                      method=method, session_id=session_id)
    except TypeError:  # 旧版 approvals.check 不接受新增关键字时回退，避免升级顺序造成 500
        ok, err, ap = approvals.check(conn, approval_id, decision, code, sess["user_id"], method=method)
    if not ok:
        status = {"APPROVAL_EXPIRED": 410, "APPROVAL_ALREADY_DECIDED": 409,
                  "APPROVAL_SESSION_MISMATCH": 403, "APPROVAL_PARAMS_MISMATCH": 400}.get(err, 400)
        raise EngineError(err, {"INVALID_OTP": "动态验证码不正确或已过期，请重新获取", "APPROVAL_EXPIRED": "审批已过期，请重新发起",
                                "INVALID_SMS": "短信验证码不正确或已过期，请重新获取",
                                "INVALID_FACE": "人脸确认已过期，请重新发起",
                                "INVALID_METHOD": "不支持的校验方式，请选择动态码、短信或人脸",
                                "APPROVAL_SESSION_MISMATCH": "该审批单不属于当前会话，已拒绝",
                                "APPROVAL_PARAMS_MISMATCH": "审批参数与会话状态不一致，已拒绝执行",
                                "APPROVAL_ALREADY_DECIDED": "该审批已处理过，不能重复提交"}.get(err, "审批处理失败"), status)
    emit = Emitter(conn, session_id, sink)
    ctx = _load_checkpoint(conn, ap["thread_id"])
    if not ctx:
        raise EngineError("CHECKPOINT_NOT_FOUND", "找不到待恢复的执行状态，请重新发起", 410)
    approved_steps = (ap.get("params") or {}).get("steps")
    if approvals.params_digest({"steps": approved_steps}) != approvals.params_digest({"steps": ctx.get("steps")}):
        raise EngineError("APPROVAL_PARAMS_MISMATCH", "审批参数与会话状态不一致，已拒绝执行", 400)
    if decision == "reject":
        _decide_and_audit(conn, approval_id, "rejected", session_id, ap, "approval_rejected", {"decision": "reject"})
        reply = "好的，已取消这笔操作，没有发生任何资金变动。"
        emit.emit("message", {"delta": reply})
        emit.emit("done", {"status": "ok", "thread_id": ap["thread_id"]})
        return {"session_id": session_id, "thread_id": ap["thread_id"], "status": "ok", "reply": reply, "intent": ctx.get("intent"),
                "plan": {"steps": ctx.get("steps", []), "edges": ctx.get("edges", [])}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}
    used_method = (method or "totp") if ap["challenge"] == "totp" else "confirm"
    _decide_and_audit(conn, approval_id, "approved", session_id, ap, "approval_granted",
                      {"decision": "approve", "challenge": ap["challenge"], "method": used_method})
    ctx["approval_id"] = approval_id
    exec_result = execute_steps(conn, ctx, emit)
    return _finalize(conn, ctx, exec_result, emit)


def undo_action(conn, session_id: str, action_id: str, sink=None) -> Dict[str, Any]:
    sess = get_session(conn, session_id)
    emit = Emitter(conn, session_id, sink)
    transfer = conn.execute("SELECT * FROM transfers WHERE transfer_id=? AND user_id=?", (action_id, sess["user_id"])).fetchone()
    try:
        if transfer:
            result = mock_core.undo_transfer(conn, sess["user_id"], action_id)
        else:
            result = mock_core.undo_purchase(conn, sess["user_id"], action_id)
    except DomainError as exc:
        raise EngineError(exc.code, exc.message, 400)
    audit.append(conn, "undo", session_id=session_id, tool="undo", args={"action_id": action_id}, result=result, risk_level="medium")
    emit.emit("undo_done", {"action_id": action_id, "message": result["message"]})
    return {"undone": True, "status": "ok", "action_id": action_id, "message": result["message"],
            "reply": result["message"], "audit": audit.entries(conn, session_id, 10), "events": emit.events}
