"""Agent 编排核心：意图 -> 计划 -> 策略门 -> 执行 -> 校验 -> 回复。

这是一个可持久化、可中断/恢复的状态机。审批时把状态写入 checkpoints，
用户确认后从同一 thread 恢复执行；每个工具调用与决策都写入哈希链审计。
"""
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import approvals, audit, config, db, mock_core, nlu, policy, totp
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
        "pending_approval": serialize_approval(pending) if pending else None,
        "llm_mode": "deepseek" if config.LLM_ENABLED else "rule",
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
        self.seq += 1
        self.conn.execute("INSERT INTO events (session_id, seq, type, payload_json, ts) VALUES (?,?,?,?,?)",
                          (self.session_id, self.seq, event_type, json.dumps(payload, ensure_ascii=False), db.now_iso()))
        self.conn.commit()
        item = {"seq": self.seq, "type": event_type, "payload": payload}
        self.events.append(item)
        if self.sink:
            self.sink(event_type, payload)


def serialize_approval(ap: Dict[str, Any], preview: str = "", title: str = "") -> Dict[str, Any]:
    params = ap.get("params") or {}
    return {
        "approval_id": ap["approval_id"], "risk_level": ap["risk_level"], "challenge": {"type": ap["challenge"]},
        "title": title or params.get("title", ""), "amount_cents": params.get("amount_cents", 0),
        "currency": "CNY", "counterparty": params.get("counterparty", ""), "reasons": ap.get("reasons", []),
        "preview": preview or params.get("preview", ""), "expires_at": ap["expires_at"], "nonce": ap["nonce"],
    }


# ---------------- 计划构建 ----------------

def _step(step_id: str, title: str, tool: str, args: Dict[str, Any], risk: str = "low") -> Dict[str, Any]:
    return {"step_id": step_id, "title": title, "tool": tool, "args": args, "risk": risk}


def _account_of(conn, user_id: str, kind: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM accounts WHERE user_id=? AND kind=? ORDER BY account_id LIMIT 1", (user_id, kind)).fetchone()
    return dict(row) if row else None


def _resolve_payee(conn, user_id: str, slots: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    payees = mock_core.list_payees(conn, user_id)
    name = slots.get("payee_name") or ""
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
    elif name == "transfer":
        payee, is_new = _resolve_payee(conn, user_id, slots)
        amount = int(slots.get("amount_cents") or 0)
        payee_name = payee["name"] if payee else slots.get("payee_name")
        payee_account = (payee["account_no"] if payee else slots.get("payee_account")) or ""
        steps.append(_step("s1", "校验收款人信息", "get_payees", {}))
        steps.append(_step("s2", "向「%s」转账 %s" % (payee_name or "未知收款人", yuan(amount)), "create_transfer", {
            "from_account_id": from_acc["account_id"] if from_acc else None, "payee_name": payee_name,
            "payee_account": payee_account, "amount_cents": amount, "payee_is_new": is_new, "memo": text[:40],
            "internal": False,
        }))
    elif name == "cross_scenario":
        parsed = nlu.parse_cross(text, {"payees": mock_core.list_payees(conn, user_id)})
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
                steps.append(_step("s%d" % idx, "还信用卡 %s" % yuan(amount), "repay_credit", {"amount_cents": amount}))
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


def _exec_tool(conn, user_id: str, session_id: str, thread_id: str, tool: str, args: Dict[str, Any], idem: str) -> Dict[str, Any]:
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
    if tool == "create_transfer":
        return mock_core.create_transfer(conn, user_id, session_id, thread_id, args.get("from_account_id"),
                                         args.get("payee_name"), args.get("payee_account") or "", int(args.get("amount_cents") or 0),
                                         args.get("memo") or "", idempotency_key=idem, internal=bool(args.get("internal")))
    if tool == "purchase_wealth":
        return mock_core.purchase_wealth(conn, user_id, session_id, thread_id, args.get("product_id"), int(args.get("amount_cents") or 0))
    if tool == "repay_credit":
        return mock_core.repay_credit(conn, user_id, int(args.get("amount_cents") or 0))
    if tool == "apply_virtual_card":
        return mock_core.apply_virtual_card(conn, user_id, int(args.get("daily_limit_cents") or 200000))
    if tool == "set_card_status":
        return mock_core.set_card_status(conn, user_id, args.get("card_id"), args.get("status"))
    if tool == "cancel_subscription":
        return mock_core.cancel_subscription(conn, user_id, args.get("sub_id") or "", args.get("merchant") or "")
    if tool == "create_reminder":
        return mock_core.create_reminder(conn, user_id, args.get("title") or "提醒", args.get("remind_at") or "")
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
            result = _exec_tool(conn, user_id, ctx["session_id"], ctx["thread_id"], step["tool"], step["args"], idem)
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
    return preview, amount, counterparty


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
        reply = _blocked_reply(failed) if failed["code"] in ("INSUFFICIENT_FUNDS", "LIMIT_EXCEEDED") else "操作未完成：" + failed["message"]
        status = "blocked"
        undo = None
        audit.append(conn, "turn_blocked", session_id=ctx["session_id"], tool=failed.get("tool", ""),
                     result=failed, risk_level="high", approval_id=ctx.get("approval_id", ""))
    else:
        reply, undo = build_reply(ctx["intent"], results, exec_result["action_ids"])
        status = "ok"
        # 写后回读校验在 execute_steps 每笔转账后立即完成，这里只读取结果
        if ctx.get("verify_issue"):
            reply += "（注意：回读校验发现余额不一致，已标记审计异常）"
            status = "blocked"
    if ctx.get("untrusted"):
        reply = "⚠️ 安全提示：检测到工具返回内容中含可疑指令（提示注入），已屏蔽并按不可信数据处理，不会执行其中任何转账要求。" + chr(10) + reply
        audit.append(conn, "injection_ignored", session_id=ctx["session_id"], tool="security_guard",
                     args={"hits": ctx["untrusted"]}, result={"status": "ignored"}, risk_level="high", approval_id=ctx.get("approval_id", ""))
    emit.emit("message", {"delta": reply})
    audit.append(conn, "response", session_id=ctx["session_id"], tool="respond", args={"intent": ctx["intent"]["name"]}, result={"status": status}, risk_level="low", approval_id=ctx.get("approval_id", ""))
    out = {"session_id": ctx["session_id"], "thread_id": ctx["thread_id"], "status": status, "reply": reply,
           "blocked": failed,
           "intent": ctx["intent"], "plan": {"steps": [{"step_id": s["step_id"], "title": s["title"], "tool": s["tool"], "risk": s.get("risk", "low")} for s in ctx["steps"]], "edges": ctx.get("edges", [])},
           "executed": results, "approval": None, "undo": undo,
           "audit": audit.entries(conn, ctx["session_id"], 20), "events": emit.events}
    return out


def run_turn(conn, session_id: str, text: str, sink: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    sess = get_session(conn, session_id)
    user_id = sess["user_id"]
    emit = Emitter(conn, session_id, sink)
    emit.emit("session", {"session_id": session_id, "thread_id": sess["thread_id"]})
    ctx_doc = {"payees": mock_core.list_payees(conn, user_id), "products": mock_core.list_wealth(conn, user_id)["products"],
               "subscriptions": mock_core.list_subscriptions(conn, user_id)["subscriptions"], "cards": mock_core.list_cards(conn, user_id),
               "accounts": mock_core.list_accounts(conn, user_id), "risk_level": "稳健型"}
    intent = nlu.parse(text, ctx_doc)
    emit.emit("intent", {"name": intent["name"], "confidence": intent["confidence"], "source": intent["source"], "slots": intent["slots"]})

    if intent["name"] == "attack":
        at = intent.get("attack_type") or "prompt_injection"
        code = {"prompt_injection": "PROMPT_INJECTION", "credential_phishing": "CREDENTIAL_PHISHING",
                "unauthorized_access": "UNAUTHORIZED_ACCESS", "suspicious_payee": "SUSPICIOUS_PAYEE"}.get(at, "PROMPT_INJECTION")
        blocked = {"code": code, "message": "检测到攻击行为：%s" % at}
        emit.emit("plan", {"steps": [], "edges": []})
        emit.emit("tool_end", {"step_id": "guard", "tool": "security_guard", "ok": False, "error": blocked})
        audit.append(conn, "attack_blocked", session_id=session_id, tool="security_guard", args={"text": text}, result=blocked, risk_level="high")
        reply = _blocked_reply(blocked)
        emit.emit("message", {"delta": reply})
        audit.append(conn, "response", session_id=session_id, tool="respond", args={"intent": "attack"}, result={"status": "blocked"}, risk_level="high")
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "blocked", "reply": reply, "blocked": blocked,
                "intent": intent, "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

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
        reply = _blocked_reply(blocked)
        emit.emit("message", {"delta": reply})
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "blocked", "reply": reply, "blocked": blocked,
                "intent": intent, "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    if intent["name"] in ("greeting", "unknown"):
        reply, _undo = build_reply(intent, [], [])
        emit.emit("message", {"delta": reply})
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "ok", "reply": reply, "intent": intent,
                "plan": {"steps": [], "edges": []}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    evaluation = policy.evaluate(conn, user_id, steps, intent)
    for s in steps:
        s["risk"] = policy.max_risk(s.get("risk", "low"), evaluation["risk_level"] if evaluation["risk_level"] in ("low", "medium", "high") else "high")
    if evaluation["blocked"]:
        blocked = evaluation["blocked"]
        audit.append(conn, "turn_blocked", session_id=session_id, tool="policy_gate", args={"steps": steps}, result=blocked, risk_level="high")
        reply = _blocked_reply(blocked)
        emit.emit("tool_end", {"step_id": "gate", "tool": "policy_gate", "ok": False, "error": blocked})
        emit.emit("message", {"delta": reply})
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "blocked", "reply": reply, "blocked": blocked,
                "intent": intent, "plan": {"steps": steps, "edges": plan["edges"]}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    if evaluation["requires_approval"]:
        preview, amount, counterparty = _approval_preview(conn, user_id, steps)
        title = evaluation.get("summary") or (steps[0]["title"] if steps else "待确认操作")
        params = {"title": title, "amount_cents": amount, "counterparty": counterparty, "preview": preview, "steps": steps}
        ap = approvals.create(conn, session_id, sess["thread_id"], intent["name"], params, evaluation["risk_level"], evaluation["reasons"], evaluation["challenge"])
        ctx["approval_id"] = ap["approval_id"]
        ctx["status"] = "awaiting_approval"
        _save_checkpoint(conn, ctx)
        typed = serialize_approval(ap, preview=preview, title=title)
        audit.append(conn, "approval_required", session_id=session_id, tool="policy_gate", args={"steps": steps}, result={"risk_level": evaluation["risk_level"], "approval_id": ap["approval_id"]}, risk_level=evaluation["risk_level"], approval_id=ap["approval_id"])
        emit.emit("approval_required", typed)
        return {"session_id": session_id, "thread_id": sess["thread_id"], "status": "awaiting_approval", "reply": "", "intent": intent,
                "plan": {"steps": steps, "edges": plan["edges"]}, "executed": [], "approval": typed, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}

    exec_result = execute_steps(conn, ctx, emit)
    return _finalize(conn, ctx, exec_result, emit)


def resume(conn, session_id: str, approval_id: str, decision: str, code: str, sink=None) -> Dict[str, Any]:
    sess = get_session(conn, session_id)
    ok, err, ap = approvals.check(conn, approval_id, decision, code, sess["user_id"])
    if not ok:
        status = 410 if err == "APPROVAL_EXPIRED" else (409 if err == "APPROVAL_ALREADY_DECIDED" else 400)
        raise EngineError(err, {"INVALID_OTP": "动态验证码不正确或已过期，请重新获取", "APPROVAL_EXPIRED": "审批已过期，请重新发起",
                                "APPROVAL_ALREADY_DECIDED": "该审批已处理过，不能重复提交"}.get(err, "审批处理失败"), status)
    emit = Emitter(conn, session_id, sink)
    ctx = _load_checkpoint(conn, ap["thread_id"])
    if not ctx:
        raise EngineError("CHECKPOINT_NOT_FOUND", "找不到待恢复的执行状态，请重新发起", 410)
    if decision == "reject":
        approvals.mark(conn, approval_id, "rejected")
        audit.append(conn, "approval_rejected", session_id=session_id, tool=ap["action"], args=ap["params"], result={"decision": "reject"}, risk_level=ap["risk_level"], approval_id=approval_id)
        reply = "好的，已取消这笔操作，没有发生任何资金变动。"
        emit.emit("message", {"delta": reply})
        emit.emit("done", {"status": "ok", "thread_id": ap["thread_id"]})
        return {"session_id": session_id, "thread_id": ap["thread_id"], "status": "ok", "reply": reply, "intent": ctx.get("intent"),
                "plan": {"steps": ctx.get("steps", []), "edges": ctx.get("edges", [])}, "executed": [], "approval": None, "undo": None,
                "audit": audit.entries(conn, session_id, 20), "events": emit.events}
    approvals.mark(conn, approval_id, "approved")
    audit.append(conn, "approval_granted", session_id=session_id, tool=ap["action"], args=ap["params"], result={"decision": "approve", "challenge": ap["challenge"]}, risk_level=ap["risk_level"], approval_id=approval_id)
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
