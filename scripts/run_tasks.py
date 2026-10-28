"""ZeroStep 评测运行器：读取 tasks/t*.json，逐条在独立数据库中回放并断言。

用法：
    python scripts/run_tasks.py
    python scripts/run_tasks.py -v --filter t06,t07
    python scripts/run_tasks.py --category security --json report.json

v0.2.0 可选断言扩展（旧用例不写这些字段时行为与旧版完全一致）：
    expect.env                  该用例运行期间临时覆盖的环境变量，如 {"MCP_ENABLED": "0"}
    expect.expected_skill       技能 id 或 id 列表，断言 intent.skills 包含
    expect.expected_tools       工具名列表，断言本轮（含审批后）实际执行过
    expect.expected_rules       断言审批单 approval.hits 的规则名包含；元素可为字符串
                                或 {"rule": 名称, "contains": detail 子串, "digit": true}
    expect.reply_contains_any   回复至少包含列表中的一个关键词（用于「外部成功或降级」两可场景）
    expect.expected_attack      断言 status == blocked 且 blocked.code 等于该值
    expect.expected_attack_type 断言 intent.attack_type 等于该值

v0.5.0 新增（消费洞察 / 流式回答 / 取消；旧用例不写这些字段时行为不变）：
    expect.expected_intent_source 断言 intent.source（如 "llm"，配合 mock_llm 验证模型链路）
    mock_llm                    用例级：预置模型返回的 raw dict（patch llm.call_model，不连外网）
    expect.insight_checks       数组，对 result.insight 做与 mock_core 直算的交叉校验：
                                totals_match / top_match / delta_sign / top_delta_sign /
                                potential_formula / savings_kinds / readonly / audit_readonly /
                                anomalies_match
    expect.detect               dict，直接调用 insight.detect_subscriptions 按契约 A2 校验：
                                registered(商户列表) / registered_count / min_detected /
                                absent(不得出现的商户) / no_category(不得出现的分类) / window_days
    stream                      dict，把该用例改成进程内 HTTP+SSE 回放（t53–t55）：
                                case=full|cancel；delay_ms 块间延迟；checks 额外断言清单
"""
import argparse
import calendar
import http.client
import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import audit, config, db, engine, llm, mock_core, totp  # noqa: E402

TASKS_DIR = ROOT / "tasks"


def load_tasks(filters, category):
    tasks = []
    for path in sorted(TASKS_DIR.glob("t*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print("[WARN] 跳过无法解析的任务文件 %s：%s" % (path.name, exc))
            continue
        tid = data.get("id") or path.stem
        if filters and tid not in filters:
            continue
        if category and data.get("category") != category:
            continue
        data.setdefault("id", tid)
        tasks.append(data)
    return tasks


def executed_tools(result):
    out = []
    for item in result.get("executed") or []:
        if isinstance(item, dict) and item.get("tool"):
            out.append(item["tool"])
    return out


def plan_tools(result):
    return [s.get("tool") for s in (result.get("plan") or {}).get("steps", [])]


def blocked_code(result):
    blocked = result.get("blocked")
    if isinstance(blocked, dict):
        return blocked.get("code")
    if isinstance(blocked, str):
        return blocked
    return None


def same_set(left, right):
    return sorted(left or []) == sorted(right or [])


def _apply_env(overrides):
    """临时覆盖环境变量并原地重载 app.config。

    engine/mcp_client/skills 等模块必须在函数内动态读 config.X，重载才立即生效。
    返回 {key: 原值或 None}，供 _restore_env 还原。
    """
    saved = {}
    for key, value in (overrides or {}).items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    if overrides:
        importlib.reload(config)
    return saved


def _restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if saved:
        importlib.reload(config)


def approval_hits(result):
    """取审批单顶层暴露的 policy hits（v0.2.0 起 serialize_approval 顶层带 hits）。"""
    approval = result.get("approval") or {}
    hits = approval.get("hits")
    return hits if isinstance(hits, list) else []



# ---------------- v0.5.0：洞察交叉校验 / 流式回答与取消（进程内真 HTTP） ----------------


def _http_json(port, method, path, payload=None, timeout=30):
    """本机 HTTP 调用，返回 (状态码, 解码后的 JSON)。"""
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {"_raw": raw.decode("utf-8", "replace")}
        return resp.status, data
    finally:
        conn.close()


def _sse_collect(port, payload, events, lock=None, timeout=120):
    """读一次 /api/chat 的 SSE 响应，把 (event, data) 追加进 events。"""
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    status = 0
    headers = {}
    event_name = ""
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("event:"):
                event_name = text[6:].strip()
            elif text.startswith("data:") and event_name:
                raw = text[5:].strip()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {"_raw": raw}
                if lock is not None:
                    with lock:
                        events.append((event_name, data))
                else:
                    events.append((event_name, data))
    finally:
        conn.close()
    return status, headers


def _wait_stream(events, lock, timeout=15.0, need_deltas=2):
    """等流式阶段开始并已发出若干 delta，返回 thread_id（超时返回 None）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            start = next((p for t, p in events if t == "answer_start"), None)
            deltas = sum(1 for t, _ in events if t == "answer_delta")
            ended = any(t == "answer_end" for t, _ in events)
        if start is not None and deltas >= need_deltas:
            return start.get("thread_id")
        if ended:
            return (start or {}).get("thread_id")
        time.sleep(0.01)
    return None


def _event_payload(events, name):
    for event_type, payload in events:
        if event_type == name:
            return payload or {}
    return {}


def _order_ok(types, sequence):
    pos = -1
    for name in sequence:
        try:
            pos = types.index(name, pos + 1)
        except ValueError:
            return False
    return True


def _http_accounts_total(port, session_id):
    status, data = _http_json(port, "GET", "/api/accounts?session_id=" + str(session_id))
    return int((data or {}).get("total_cents") or 0)


def _share_pct(cents, total):
    if total <= 0:
        return 0
    return int(int(cents) * 100.0 / total + 0.5)


def _pct_independent(cur, prev):
    """独立实现的整数环比（四舍五入、负值远离 0），用于和 insight._pct 交叉校验。"""
    if prev <= 0:
        return 0
    value = (int(cur) - int(prev)) * 100.0 / int(prev)
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def _prev_window_categories(conn, user_id):
    """独立 SQL：上一窗口（now-60 天 ~ now-30 天）出账分类汇总。"""
    now = datetime.now()
    start = (now - timedelta(days=60)).isoformat(timespec="seconds")
    end = (now - timedelta(days=30)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT category, amount_cents FROM transactions "
        "WHERE user_id=? AND direction='out' AND ts>=? AND ts<?",
        (user_id, start, end),
    ).fetchall()
    cats = {}
    for row in rows:
        category = row[0]
        cats[category] = cats.get(category, 0) + int(row[1] or 0)
    return sum(cats.values()), cats


def _check_insight_checks(checks, data, conn, uid, sid, failures, info):
    """按契约 A1/A3 对 result.insight 做与 mock_core / 独立 SQL 的交叉校验。"""
    data = data or {}
    totals = data.get("totals") or {}
    top = data.get("top_categories") or []
    savings = data.get("savings") or []
    bill = mock_core.bill_summary(conn, uid, 1) or {}
    out_cents = int(totals.get("out_cents") or 0)
    prev_out, prev_cats = _prev_window_categories(conn, uid)

    if "totals_match" in checks:
        if out_cents != int(bill.get("total_out_cents") or 0):
            failures.append("洞察支出与 mock_core.bill_summary 不一致：%d vs %d"
                            % (out_cents, int(bill.get("total_out_cents") or 0)))
        if int(totals.get("in_cents") or 0) != int(bill.get("total_in_cents") or 0):
            failures.append("洞察收入与 mock_core.bill_summary 不一致：%s vs %s"
                            % (totals.get("in_cents"), bill.get("total_in_cents")))
        if int(totals.get("prev_out_cents") or 0) != prev_out:
            failures.append("洞察上期支出与独立 SQL 不一致：%s vs %d" % (totals.get("prev_out_cents"), prev_out))

    if "top_match" in checks:
        expected = (bill.get("by_category") or [])[:5]
        if len(top) != len(expected):
            failures.append("top_categories 条数应为 %d，实际 %d" % (len(expected), len(top)))
        for idx, (got, want) in enumerate(zip(top, expected)):
            if got.get("category") != want.get("category"):
                failures.append("top_categories[%d] 分类不符：期望 %s，实际 %s"
                                % (idx, want.get("category"), got.get("category")))
            if int(got.get("cents") or 0) != int(want.get("amount_cents") or 0):
                failures.append("top_categories[%d] 金额不符：期望 %s，实际 %s"
                                % (idx, want.get("amount_cents"), got.get("cents")))
            want_pct = _share_pct(int(want.get("amount_cents") or 0), int(bill.get("total_out_cents") or 0))
            if int(got.get("pct") or 0) != want_pct:
                failures.append("top_categories[%d] 占比不符：期望 %d%%，实际 %s%%"
                                % (idx, want_pct, got.get("pct")))

    delta_expected = _pct_independent(out_cents, prev_out)
    if "delta_sign" in checks:
        if int(totals.get("delta_pct") or 0) != delta_expected:
            failures.append("环比数值不符：独立直算 %d%%，实际 %s%%" % (delta_expected, totals.get("delta_pct")))
        got_delta = int(totals.get("delta_pct") or 0)
        if out_cents != prev_out and got_delta != 0:
            if (out_cents > prev_out) != (got_delta > 0):
                failures.append("环比符号错误：本月 %d 分、上月 %d 分，实际 delta_pct=%s"
                                % (out_cents, prev_out, got_delta))
    if "top_delta_sign" in checks:
        for item in top:
            cat = item.get("category")
            want = _pct_independent(int(item.get("cents") or 0), prev_cats.get(cat, 0))
            if int(item.get("delta_pct") or 0) != want:
                failures.append("分类「%s」环比应 %d%%，实际 %s%%（上期 %d 分）"
                                % (cat, want, item.get("delta_pct"), prev_cats.get(cat, 0)))

    if "potential_formula" in checks:
        subs = [s for s in savings if s.get("kind") == "subscription"]
        monthly_sum = sum(int(s.get("monthly_cents") or 0) for s in subs)
        yearly_sum = sum(int(s.get("yearly_cents") or 0) for s in subs)
        potential = int(totals.get("potential_save_monthly_cents") or 0)
        if potential != monthly_sum:
            failures.append("potential_save 应等于订阅 monthly_cents 之和：%d vs %d" % (monthly_sum, potential))
        if yearly_sum // 12 != potential:
            failures.append("potential_save 应等于订阅年化/12：%d/12=%d vs %d"
                            % (yearly_sum, yearly_sum // 12, potential))
        for item in subs:
            if int(item.get("yearly_cents") or 0) != int(item.get("monthly_cents") or 0) * 12:
                failures.append("订阅「%s」yearly_cents 不等于 monthly_cents×12" % item.get("title"))

    if "budget_formula" in checks:
        budget = data.get("budget") or {}
        row = conn.execute(
            "SELECT COALESCE(SUM(monthly_cents),0) FROM budgets WHERE user_id=?", (uid,)
        ).fetchone()
        budget_sum = int(row[0] if row else 0)
        today = datetime.now()
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        projected = int(round(out_cents * days_in_month / 30.0))
        if int(budget.get("budget_cents") or 0) != budget_sum:
            failures.append("budget.budget_cents 与 budgets 表不一致：期望 %d，实际 %s"
                            % (budget_sum, budget.get("budget_cents")))
        if int(budget.get("spent_cents") or 0) != out_cents:
            failures.append("budget.spent_cents 应等于 totals.out_cents")
        if int(budget.get("projected_cents") or 0) != projected:
            failures.append("budget.projected_cents 不符：期望 %d，实际 %s" % (projected, budget.get("projected_cents")))
        if int(budget.get("over_cents") or 0) != max(0, projected - budget_sum):
            failures.append("budget.over_cents 不符：期望 %d，实际 %s"
                            % (max(0, projected - budget_sum), budget.get("over_cents")))
        if int(budget.get("days_left") or 0) != max(0, days_in_month - today.day):
            failures.append("budget.days_left 不符：期望 %d，实际 %s"
                            % (max(0, days_in_month - today.day), budget.get("days_left")))

    if "savings_kinds" in checks:
        if not savings:
            failures.append("savings 为空，洞察应至少给出订阅/分类建议")
        for item in savings:
            if item.get("kind") not in ("subscription", "category"):
                failures.append("savings.kind 非法：%s" % item.get("kind"))
            action = item.get("action") or {}
            if action.get("kind") not in ("subscription_cancel", "query_bills"):
                failures.append("savings.action.kind 非法：%s" % action.get("kind"))
            if int(item.get("yearly_cents") or 0) != int(item.get("monthly_cents") or 0) * 12:
                failures.append("savings「%s」yearly_cents 不等于 monthly_cents×12" % item.get("title"))

    if "readonly" in checks:
        if (info.get("plan_tools") or []) != ["spending_insights"]:
            failures.append("洞察计划工具应只含 spending_insights，实际 %s" % info.get("plan_tools"))
        executed = [t for t in (info.get("executed_all") or []) if t]
        if not executed or set(executed) != {"spending_insights"}:
            failures.append("洞察实际执行应只含 spending_insights，实际 %s" % executed)
        if info.get("balance_before") != info.get("balance_after"):
            failures.append("洞察不应改动余额：%s -> %s"
                            % (info.get("balance_before"), info.get("balance_after")))

    if "audit_readonly" in checks:
        entries = audit.entries(conn, session_id=sid, limit=200) if sid else []
        banned = {"approval_required", "turn_blocked", "answer_cancelled", "undo_done",
                  "tool_start", "tool_end"}
        for entry in entries:
            event_type = entry.get("event_type")
            if event_type in banned:
                failures.append("洞察审计出现写操作事件：%s" % event_type)
            if event_type == "tool_result" and entry.get("tool") != "spending_insights":
                failures.append("洞察审计出现非只读工具：%s" % entry.get("tool"))
        if not any(e.get("event_type") == "tool_result" and e.get("tool") == "spending_insights"
                   for e in entries):
            failures.append("审计缺少 spending_insights 的只读执行记录")

    if "anomalies_match" in checks:
        expected = (mock_core.scan_anomalies(conn, uid, 30) or {}).get("anomalies") or []
        expected_compact = [{
            "date": str(a.get("ts") or "")[:10],
            "merchant": str(a.get("counterparty") or ""),
            "cents": int(a.get("amount_cents") or 0),
            "reason": str(a.get("detail") or ""),
        } for a in expected[:5]]
        if list(data.get("anomalies") or []) != expected_compact:
            failures.append("洞察 anomalies 与 mock_core.scan_anomalies 前 5 条不一致")


def _check_detect(spec, result, failures):
    """按契约 A2 校验 detect_subscriptions 的返回。"""
    detected = result.get("detected") or []
    by_merchant = {str(item.get("merchant") or ""): item for item in detected}
    registered_names = spec.get("registered") or []
    for name in registered_names:
        item = by_merchant.get(name)
        if item is None:
            failures.append("detect 未识别出已登记订阅商户「%s」" % name)
            continue
        if item.get("registered") is not True:
            failures.append("detect 中「%s」应标 registered=true" % name)
        if not item.get("sub_id"):
            failures.append("detect 中「%s」缺少 sub_id" % name)
    if spec.get("registered_count") is not None:
        want = int(spec["registered_count"])
        if int(result.get("registered_count") or 0) != want:
            failures.append("detect.registered_count 期望 %d，实际 %s" % (want, result.get("registered_count")))
    if spec.get("min_detected"):
        want = int(spec["min_detected"])
        if len(detected) < want:
            failures.append("detect 识别条数应 >= %d，实际 %d" % (want, len(detected)))
    for name in spec.get("absent") or []:
        for item in detected:
            merchant = str(item.get("merchant") or "")
            if merchant and (merchant == name or name in merchant or merchant in name):
                failures.append("detect 把一次性消费「%s」误报成周期扣费（命中「%s」）" % (name, merchant))
                break
    no_category = spec.get("no_category")
    if no_category:
        for item in detected:
            if item.get("category") == no_category:
                failures.append("detect 把「%s」分类的商户「%s」误报成周期扣费"
                                % (no_category, item.get("merchant")))
    unregistered = 0
    for item in detected:
        occurrences = int(item.get("occurrences") or 0)
        if occurrences < 2:
            failures.append("detect 商户「%s」occurrences=%d，周期扣费至少 2 次"
                            % (item.get("merchant"), occurrences))
        want_conf = min(60 + 10 * (occurrences - 2), 90)
        if int(item.get("confidence") or 0) != want_conf:
            failures.append("detect 商户「%s」confidence 应为 %d，实际 %s"
                            % (item.get("merchant"), want_conf, item.get("confidence")))
        if not item.get("registered"):
            unregistered += int(item.get("monthly_cents") or 0)
    if int(result.get("unregistered_monthly_cents") or 0) != unregistered:
        failures.append("detect.unregistered_monthly_cents 期望 %d，实际 %s"
                        % (unregistered, result.get("unregistered_monthly_cents")))


def _check_http_expect(exp, info, failures):
    if exp.get("status") and info.get("status") != exp["status"]:
        failures.append("状态不符：期望 %s，实际 %s" % (exp["status"], info.get("status")))
    if exp.get("intent") and info.get("intent") != exp["intent"]:
        failures.append("意图不符：期望 %s，实际 %s" % (exp["intent"], info.get("intent")))
    if exp.get("plan_tools") is not None and not same_set(info.get("plan_tools"), exp["plan_tools"]):
        failures.append("计划工具不符：期望 %s，实际 %s" % (exp["plan_tools"], info.get("plan_tools")))
    if exp.get("expected_tools") is not None:
        actual = info.get("executed_all") or []
        missing = [t for t in exp["expected_tools"] if t not in actual]
        if missing:
            failures.append("期望执行未发生：%s，实际执行 %s" % (missing, actual))
    reply = info.get("reply") or ""
    for needle in exp.get("reply_contains") or []:
        if needle not in reply:
            failures.append("回复缺少关键词：%s" % needle)
    if exp.get("expected_intent_source") and info.get("intent_source") != exp["expected_intent_source"]:
        failures.append("意图来源不符：期望 %s，实际 %s"
                        % (exp["expected_intent_source"], info.get("intent_source")))
    if exp.get("audit_valid"):
        chain = info.get("audit") or {}
        if not chain.get("valid"):
            failures.append("审计链校验失败：%s" % chain.get("reason"))
        if int(chain.get("checked") or 0) <= 0:
            failures.append("审计链为空")


def _run_http_task(task, verbose=False):
    """进程内起 backend/server.py，用真实 HTTP + SSE 回放流式用例（t53–t55）。"""
    tmpdir = str(ROOT / ".eval_tmp" / task["id"])
    os.makedirs(tmpdir, exist_ok=True)
    old_db = config.DB_PATH
    old_delay = os.environ.get("ZS_STREAM_DELAY_MS")
    env_saved = {}
    failures = []
    info = {}
    conn = None
    httpd = None
    serve_thread = None
    server_mod = None
    try:
        spec = task.get("stream") or {}
        env_saved = _apply_env(task.get("env"))
        os.environ["ZS_STREAM_DELAY_MS"] = str(int(spec.get("delay_ms") or 0))
        config.DB_PATH = os.path.join(tmpdir, "bank.db")
        conn = db.connect()
        db.init_schema(conn)
        db.seed(conn)
        if "server" in sys.modules:
            server_mod = importlib.reload(sys.modules["server"])
        else:
            server_mod = importlib.import_module("server")
        httpd = server_mod.Server(("127.0.0.1", 0), server_mod.Handler)
        port = httpd.server_address[1]
        serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        serve_thread.start()
        session_status, session_payload = _http_json(port, "POST", "/api/session", {})
        sid = (session_payload or {}).get("session_id")
        if not sid:
            failures.append("进程内服务创建会话失败：HTTP %s %s"
                            % (session_status, str(session_payload)[:200]))
            return False, failures, info
        exp = task.get("expect") or {}
        text = (task.get("turns") or [""])[0]
        checks = set(spec.get("checks") or [])

        if spec.get("case") == "cancel":
            pre_status, pre = _http_json(port, "POST", "/api/turn/cancel",
                                         {"thread_id": "thread_not_streaming"})
            info["pre_cancel"] = pre
            if pre_status != 200 or (pre or {}).get("cancelled") is not False:
                failures.append("非流式阶段取消应返回 200 cancelled=false，实际 HTTP %s %s"
                                % (pre_status, pre))
            elif (pre or {}).get("reason") != "not_streaming":
                failures.append("非流式阶段取消 reason 应为 not_streaming，实际 %s"
                                % (pre or {}).get("reason"))
            events = []
            lock = threading.Lock()
            reader = threading.Thread(
                target=_sse_collect,
                args=(port, {"session_id": sid, "text": text, "stream": True}, events, lock),
                daemon=True,
            )
            reader.start()
            thread_id = _wait_stream(events, lock)
            if not thread_id:
                failures.append("15 秒内未等到 answer_start（无法验证流式阶段取消）")
            total_before = _http_accounts_total(port, sid)
            cancel_status, cancel = _http_json(port, "POST", "/api/turn/cancel",
                                               {"thread_id": thread_id or ""})
            info["cancel"] = cancel
            reader.join(60)
            if reader.is_alive():
                failures.append("取消后 SSE 连接 60 秒仍未结束")
            types = [t for t, _ in events]
            info["events"] = types
            starts = [p for t, p in events if t == "answer_start"]
            deltas = [p for t, p in events if t == "answer_delta"]
            ends = [p for t, p in events if t == "answer_end"]
            notes = [p for t, p in events if t == "note"]
            done_payload = _event_payload(events, "done")
            end_payload = ends[-1] if ends else {}
            if cancel_status != 200 or (cancel or {}).get("cancelled") is not True:
                failures.append("流式阶段取消应返回 200 cancelled=true，实际 HTTP %s %s"
                                % (cancel_status, cancel))
            if not ends:
                failures.append("取消后缺少 answer_end 事件")
            if end_payload.get("cancelled") is not True:
                failures.append("取消后 answer_end.cancelled 应为 true，实际 %s"
                                % end_payload.get("cancelled"))
            if int(end_payload.get("chunks") or 0) != len(deltas):
                failures.append("answer_end.chunks=%s 与实际 delta 数 %d 不一致"
                                % (end_payload.get("chunks"), len(deltas)))
            emitted = int((cancel or {}).get("emitted_chunks") or 0)
            if emitted != int(end_payload.get("chunks") or 0):
                failures.append("cancel.emitted_chunks=%s 与 answer_end.chunks=%s 不一致"
                                % (emitted, end_payload.get("chunks")))
            if not notes or notes[-1].get("text") != "已停止输出":
                failures.append("取消后缺少 note 事件「已停止输出」")
            if "error" in types:
                failures.append("取消流程出现 error 事件：%s" % _event_payload(events, "error"))
            if "done" not in types:
                failures.append("取消流程缺少 done 事件")
            if not _order_ok(types, ["answer_start", "answer_end", "note", "done"]):
                failures.append("取消事件顺序不符：%s" % types)
            ns_status, ns_payload = _http_json(port, "POST", "/api/chat",
                                               {"session_id": sid, "text": text})
            full_reply = str((ns_payload or {}).get("reply") or "")
            joined = "".join(str(d.get("text") or "") for d in deltas)
            total_chunks = (len(full_reply) + 11) // 12
            if starts and int(starts[0].get("chars") or 0) != len(full_reply):
                failures.append("answer_start.chars=%s 与完整回答长度 %d 不一致"
                                % (starts[0].get("chars"), len(full_reply)))
            if not full_reply.startswith(joined):
                failures.append("已发出的 delta 不是完整回答的前缀：发 %d 字 / 全文 %d 字"
                                % (len(joined), len(full_reply)))
            if not (0 < int(end_payload.get("chunks") or 0) < total_chunks):
                failures.append("期望中途取消（0<chunks<%d），实际 chunks=%s"
                                % (total_chunks, end_payload.get("chunks")))
            info["status"] = (done_payload or {}).get("status")
            info["intent"] = (_event_payload(events, "intent") or {}).get("name")
            info["plan_tools"] = [s.get("tool") for s in
                                  (_event_payload(events, "plan").get("steps") or [])]
            info["executed_all"] = [p.get("tool") for t, p in events if t == "tool_end"]
            info["reply"] = full_reply
            if "audit_cancelled" in checks:
                audit_status, aud = _http_json(
                    port, "GET", "/api/audit?session_id=" + str(sid) + "&limit=100")
                entries = (aud or {}).get("entries") or []
                chain = (aud or {}).get("chain") or {}
                info["audit"] = chain
                rows = [e for e in entries if e.get("event_type") == "answer_cancelled"]
                if len(rows) != 1:
                    failures.append("审计应恰有 1 条 answer_cancelled，实际 %d 条" % len(rows))
                elif rows[0].get("tool") != "stream":
                    failures.append("answer_cancelled 审计 tool 应为 stream，实际 %s"
                                    % rows[0].get("tool"))
                if not chain.get("valid"):
                    failures.append("取消后审计链校验失败：%s" % chain.get("reason"))
            if "balance_unchanged" in checks:
                total_after = _http_accounts_total(port, sid)
                if total_before != total_after:
                    failures.append("取消不应改变余额：取消前 %d 分，取消后 %d 分"
                                    % (total_before, total_after))
            if "executed_not_undone" in checks:
                card = _event_payload(events, "insight_card")
                insight_data = card.get("insight") or {}
                if int((insight_data.get("totals") or {}).get("out_cents") or 0) <= 0:
                    failures.append("取消后应保留已执行的只读工具结果（insight_card 缺少 totals.out_cents）")
                tool_ends = [p.get("tool") for t, p in events if t == "tool_end"]
                if "spending_insights" not in tool_ends:
                    failures.append("取消后 tool_end 事件里没有 spending_insights")
        else:
            events = []
            stream_status, stream_headers = _sse_collect(
                port, {"session_id": sid, "text": text, "stream": True}, events)
            if stream_status != 200:
                failures.append("流式请求 HTTP %s（期望 200）" % stream_status)
            ctype = str((stream_headers or {}).get("content-type") or "")
            if "text/event-stream" not in ctype:
                failures.append("流式响应 Content-Type 应为 text/event-stream，实际 %s" % ctype)
            types = [t for t, _ in events]
            start_payload = _event_payload(events, "answer_start")
            deltas = [p for t, p in events if t == "answer_delta"]
            end_payload = _event_payload(events, "answer_end")
            done_payload = _event_payload(events, "done")
            if not types or types[-1] != "done":
                failures.append("流式事件最后一条应为 done，实际 %s" % types[-5:])
            if not _order_ok(types, ["session", "intent", "plan", "tool_start", "tool_end",
                                     "answer_start", "answer_end", "done"]):
                failures.append("流式事件顺序不符：%s" % types)
            indices = [int(p.get("i") or 0) for p in deltas]
            if indices != list(range(len(deltas))):
                failures.append("answer_delta 的 i 不连续：%s" % indices[:5])
            pieces = [str(p.get("text") or "") for p in deltas]
            bad_len = [i for i, piece in enumerate(pieces[:-1]) if len(piece) != 12]
            if bad_len:
                failures.append("非末块 delta 长度应为 12 字符，异常下标 %s" % bad_len[:5])
            joined = "".join(pieces)
            ns_status, ns_payload = _http_json(port, "POST", "/api/chat",
                                               {"session_id": sid, "text": text})
            full_reply = str((ns_payload or {}).get("reply") or "")
            if joined != full_reply:
                failures.append("流式拼接文本与完整回答不一致：拼 %d 字 / 全文 %d 字"
                                % (len(joined), len(full_reply)))
            if int(start_payload.get("chars") or 0) != len(full_reply):
                failures.append("answer_start.chars=%s 与回答长度 %d 不一致"
                                % (start_payload.get("chars"), len(full_reply)))
            if int(end_payload.get("chunks") or 0) != len(deltas):
                failures.append("answer_end.chunks=%s 与 delta 数 %d 不一致"
                                % (end_payload.get("chunks"), len(deltas)))
            if end_payload.get("cancelled") is not False:
                failures.append("完整流式 answer_end.cancelled 应为 false")
            if int(end_payload.get("elapsed_ms") or 0) < 0:
                failures.append("answer_end.elapsed_ms 非法")
            if (done_payload or {}).get("status") != "ok":
                failures.append("done.status 应为 ok，实际 %s" % (done_payload or {}).get("status"))
            if exp.get("intent") == "insight" and not any(t == "insight_card" for t, _ in events):
                failures.append("洞察回答缺少 insight_card 事件")
            off_events = []
            off_status, off_headers = _sse_collect(
                port, {"session_id": sid, "text": text, "stream": True, "stream_text": False},
                off_events)
            off_types = [t for t, _ in off_events]
            if off_status != 200:
                failures.append("stream_text=false 请求 HTTP %s" % off_status)
            if "message" not in off_types:
                failures.append("stream_text=false 时应下发 message 事件，实际 %s" % off_types)
            for banned in ("answer_start", "answer_delta", "answer_end"):
                if banned in off_types:
                    failures.append("stream_text=false 时不应出现 %s 事件" % banned)
            off_message = _event_payload(off_events, "message")
            if str(off_message.get("delta") or "") != full_reply:
                failures.append("stream_text=false 的 message.delta 应等于完整回答")
            info["status"] = (done_payload or {}).get("status")
            info["intent"] = (_event_payload(events, "intent") or {}).get("name")
            info["plan_tools"] = [s.get("tool") for s in
                                  (_event_payload(events, "plan").get("steps") or [])]
            info["executed_all"] = [p.get("tool") for t, p in events if t == "tool_end"]
            info["reply"] = full_reply

        if exp.get("audit_valid") and not info.get("audit"):
            audit_status, aud = _http_json(port, "GET",
                                           "/api/audit?session_id=" + str(sid) + "&limit=100")
            info["audit"] = (aud or {}).get("chain") or {}
        _check_http_expect(exp, info, failures)
    except Exception as exc:  # noqa: BLE001
        failures.append("流式用例运行异常：%s: %s" % (type(exc).__name__, exc))
        if verbose:
            traceback.print_exc()
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if serve_thread is not None:
            serve_thread.join(5)
        if server_mod is not None:
            try:
                server_mod.CONN.close()
            except Exception:  # noqa: BLE001
                pass
        if conn is not None:
            conn.close()
        try:
            _restore_env(env_saved)
        finally:
            config.DB_PATH = old_db
            if old_delay is None:
                os.environ.pop("ZS_STREAM_DELAY_MS", None)
            else:
                os.environ["ZS_STREAM_DELAY_MS"] = old_delay
        try:
            for name in os.listdir(tmpdir):
                try:
                    os.unlink(os.path.join(tmpdir, name))
                except OSError:
                    pass
            os.rmdir(tmpdir)
        except OSError:
            pass
    return (not failures), failures, info


def run_one(task, verbose=False):
    tmpdir = str(ROOT / ".eval_tmp" / task["id"])
    os.makedirs(tmpdir, exist_ok=True)
    old_db = config.DB_PATH
    env_saved = {}
    failures = []
    info = {}
    conn = None
    saved_call_model = None
    try:
        env_saved = _apply_env(task.get("env"))
        config.DB_PATH = os.path.join(tmpdir, "bank.db")
        conn = db.connect()
        db.init_schema(conn)
        db.seed(conn)
        uid = task.get("user_id") or "u001"
        session = engine.create_session(conn, uid)
        sid = session["session_id"]
        exp = task.get("expect") or {}
        info["balance_before"] = mock_core.account_total(conn, uid)
        canned_llm = task.get("mock_llm")
        if canned_llm is not None:
            saved_call_model = llm.call_model
            _canned = json.loads(json.dumps(canned_llm))
            llm.call_model = lambda *args, **kwargs: json.loads(json.dumps(_canned))
        first = None
        final = None
        for text in task.get("turns") or []:
            result = engine.run_turn(conn, sid, text)
            if first is None:
                first = result
            final = result
            approval = result.get("approval")
            if approval:
                if "approval_hits" not in info:
                    info["approval_hits"] = approval_hits(result)
                if "approval" not in info:
                    info["approval"] = {
                        "risk": approval.get("risk_level"),
                        "challenge": (approval.get("challenge") or {}).get("type"),
                    }
                if exp.get("approval") or exp.get("after"):
                    result = engine.resume(conn, sid, approval.get("approval_id"), "approve", totp.current_code(uid))
                    final = result
                    info["after_status"] = result.get("status")
                    info["executed"] = executed_tools(result)
                    info["undo"] = bool(result.get("undo"))
        if first is None:
            failures.append("任务没有 turns 或未产生结果")
            return False, failures, info

        info["intent"] = (first.get("intent") or {}).get("name")
        info["intent_source"] = (first.get("intent") or {}).get("source")
        info["status"] = first.get("status")
        info["plan_tools"] = plan_tools(first)
        info["blocked"] = blocked_code(first)
        info["reply"] = final.get("reply") if final else ""
        info["skills"] = list((first.get("intent") or {}).get("skills") or [])
        info["attack_type"] = (first.get("intent") or {}).get("attack_type")
        info["executed_all"] = executed_tools(first) + executed_tools(final or {})
        info["balance_after"] = mock_core.account_total(conn, uid)
        if exp.get("expected_intent_source") and info.get("intent_source") != exp["expected_intent_source"]:
            failures.append("意图来源不符：期望 %s，实际 %s"
                            % (exp["expected_intent_source"], info.get("intent_source")))
        insight_checks = exp.get("insight_checks") or []
        detect_spec = exp.get("detect")
        if insight_checks or detect_spec:
            from app import insight as insight_mod
            data = (final or {}).get("insight")
            if not isinstance(data, dict):
                data = insight_mod.spending_insights(conn, uid)
            info["insight"] = data
            _check_insight_checks(insight_checks, data, conn, uid, sid, failures, info)
            if detect_spec:
                detected = insight_mod.detect_subscriptions(
                    conn, uid, int(detect_spec.get("window_days") or 120))
                info["detect"] = {
                    "registered_count": detected.get("registered_count"),
                    "detected": len(detected.get("detected") or []),
                }
                _check_detect(detect_spec, detected, failures)

        if exp.get("status"):
            actual = info.get("status")
            if exp["status"] != "awaiting_approval" and exp.get("after"):
                actual = info.get("after_status") or actual
            if actual != exp["status"]:
                failures.append("状态不符：期望 %s，实际 %s" % (exp["status"], actual))
        if exp.get("intent") and info.get("intent") != exp["intent"]:
            failures.append("意图不符：期望 %s，实际 %s" % (exp["intent"], info.get("intent")))
        if exp.get("plan_tools") is not None and not same_set(info.get("plan_tools"), exp["plan_tools"]):
            failures.append("计划工具不符：期望 %s，实际 %s" % (exp["plan_tools"], info.get("plan_tools")))
        if exp.get("blocked") and info.get("blocked") != exp["blocked"]:
            failures.append("拦截码不符：期望 %s，实际 %s" % (exp["blocked"], info.get("blocked")))
        expected_skill = exp.get("expected_skill")
        if expected_skill:
            wanted_skills = expected_skill if isinstance(expected_skill, list) else [expected_skill]
            actual_skills = info.get("skills") or []
            missing_skills = [s for s in wanted_skills if s not in actual_skills]
            if missing_skills:
                failures.append("技能未命中：期望包含 %s，实际 intent.skills=%s" % (missing_skills, actual_skills))
        if exp.get("expected_tools") is not None:
            actual_tools = info.get("executed_all") or []
            missing_tools = [t for t in exp["expected_tools"] if t not in actual_tools]
            if missing_tools:
                failures.append("期望执行未发生：%s，实际执行 %s" % (missing_tools, actual_tools))
        expected_rules = exp.get("expected_rules") or []
        if expected_rules:
            hits = info.get("approval_hits") or []
            rule_names = [h.get("rule") for h in hits if isinstance(h, dict)]
            hits_by_name = {}
            for hit in hits:
                if isinstance(hit, dict) and hit.get("rule"):
                    hits_by_name.setdefault(hit["rule"], hit)
            for item in expected_rules:
                if isinstance(item, str):
                    if item not in rule_names:
                        failures.append("审批规则未命中：期望 %s，实际 hits=%s" % (item, rule_names))
                    continue
                rule = (item or {}).get("rule")
                if rule not in hits_by_name:
                    failures.append("审批规则未命中：期望 %s，实际 hits=%s" % (rule, rule_names))
                    continue
                detail = str(hits_by_name[rule].get("detail") or "")
                if item.get("contains") and item["contains"] not in detail:
                    failures.append("规则 %s 详情缺少关键词：%s，实际「%s」" % (rule, item["contains"], detail))
                if item.get("digit") and not re.search(r"\d", detail):
                    failures.append("规则 %s 详情缺少具体数字：「%s」" % (rule, detail))
        if exp.get("expected_attack_type"):
            actual_attack_type = info.get("attack_type")
            if actual_attack_type != exp["expected_attack_type"]:
                failures.append("攻击类型不符：期望 %s，实际 %s" % (exp["expected_attack_type"], actual_attack_type))
        if exp.get("expected_attack"):
            expected_code = exp["expected_attack"]
            if info.get("status") != "blocked":
                failures.append("期望被拦截（blocked），实际状态 %s" % info.get("status"))
            if info.get("blocked") != expected_code:
                failures.append("拦截码不符：期望 %s，实际 %s" % (expected_code, info.get("blocked")))
        if exp.get("approval"):
            actual_approval = info.get("approval") or {}
            if actual_approval.get("risk") != exp["approval"].get("risk"):
                failures.append("风险级不符：期望 %s，实际 %s" % (exp["approval"].get("risk"), actual_approval.get("risk")))
            if actual_approval.get("challenge") != exp["approval"].get("challenge"):
                failures.append("审批方式不符：期望 %s，实际 %s" % (exp["approval"].get("challenge"), actual_approval.get("challenge")))
        after = exp.get("after")
        if after:
            if after.get("status") and info.get("after_status") != after["status"]:
                failures.append("审批后状态不符：期望 %s，实际 %s" % (after["status"], info.get("after_status")))
            if after.get("executed") is not None and not same_set(info.get("executed"), after["executed"]):
                failures.append("执行工具不符：期望 %s，实际 %s" % (after["executed"], info.get("executed")))
            if after.get("undo") and not info.get("undo"):
                failures.append("期望返回可撤销的 action_id 与 60 秒窗口")
        text = info.get("reply") or ""
        for needle in exp.get("reply_contains") or []:
            if needle not in text:
                failures.append("回复缺少关键词：%s" % needle)
        any_needles = exp.get("reply_contains_any") or []
        if any_needles and not any(str(n) in text for n in any_needles):
            failures.append("回复未包含任一关键词：%s（实际回复：%s）"
                            % ("/".join(str(n) for n in any_needles), text[:120]))
        executed = executed_tools(first) + executed_tools(final or {})
        for bad in exp.get("forbidden_executed") or []:
            if bad in executed:
                failures.append("禁止执行的工具被执行：%s" % bad)
        for bad in exp.get("forbidden_tools") or []:
            if bad in plan_tools(first):
                failures.append("计划中出现了禁止工具：%s" % bad)
        if exp.get("audit_valid"):
            chain = audit.verify(conn)
            info["audit"] = chain
            if not chain.get("valid"):
                failures.append("审计链校验失败：%s" % chain.get("reason"))
            if chain.get("checked", 0) <= 0:
                failures.append("审计链为空")
    except Exception as exc:  # noqa: BLE001
        failures.append("运行异常：%s: %s" % (type(exc).__name__, exc))
        if verbose:
            traceback.print_exc()
    finally:
        if saved_call_model is not None:
            llm.call_model = saved_call_model
        if conn is not None:
            conn.close()
        try:
            _restore_env(env_saved)
        finally:
            config.DB_PATH = old_db
        try:
            for name in os.listdir(tmpdir):
                try:
                    os.unlink(os.path.join(tmpdir, name))
                except OSError:
                    pass
            os.rmdir(tmpdir)
        except OSError:
            pass
    return (not failures), failures, info


def main(argv=None):
    # Windows 控制台默认 GBK，回复里含 ¥、≈ 等字符时打印会抛 UnicodeEncodeError；
    # 统一按 UTF-8 输出（JSON 报告本身始终以 UTF-8 写文件，不受影响）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="ZeroStep 银行 Agent 评测运行器")
    parser.add_argument("--filter", default="", help="逗号分隔的任务 id，如 t01,t06")
    parser.add_argument("--category", default="", help="只跑某个类别：bills/transfer/wealth/card/subscription/cross/security")
    parser.add_argument("--json", default="", help="把完整报告写入指定 JSON 文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印异常堆栈与回复全文")
    args = parser.parse_args(argv)

    filters = set(x.strip() for x in args.filter.split(",") if x.strip())
    tasks = load_tasks(filters, args.category)
    if not tasks:
        print("没有匹配的任务。")
        return 2

    print("ZeroStep 评测：共 %d 条任务" % len(tasks))
    print("-" * 72)
    rows = []
    passed = 0
    for task in tasks:
        if task.get("stream"):
            ok, failures, info = _run_http_task(task, verbose=args.verbose)
        else:
            ok, failures, info = run_one(task, verbose=args.verbose)
        if ok:
            passed += 1
        rows.append({"id": task["id"], "title": task.get("title", ""), "category": task.get("category", ""),
                     "ok": ok, "failures": failures, "info": info})
        mark = "PASS" if ok else "FAIL"
        print("%s  %s  %-22s  %s" % (mark, task["id"], task.get("title", ""), (info.get("intent") or "-")))
        if not ok:
            for item in failures:
                print("       - " + item)
            if args.verbose:
                print("       回复：" + str(info.get("reply"))[:400])
    total = len(tasks)
    print("-" * 72)
    print("结果：%d/%d 通过，%d 失败" % (passed, total, total - passed))
    if args.json:
        report = {"total": total, "passed": passed, "failed": total - passed, "tasks": rows}
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("报告已写入 " + args.json)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
