"""ZeroStep HTTP 服务：零第三方依赖（http.server + sqlite3 + SSE）。

启动：
    python backend/server.py            # 默认 http://127.0.0.1:8000
    PORT=8080 python backend/server.py

接口契约见 docs/api.md。结构化事件（tool_start/tool_end/approval_required/message）
通过 SSE 推给前端，同时持久化到 events 表，可回放、可审计。
"""
import hmac
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app import analytics, audit, biz, config, db, engine, llm, mcp_client, mock_core, plugins, push, second_factor, skills, totp  # noqa: E402

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("image/png", ".png")

_BOOT_CONN = db.connect()
db.init_schema(_BOOT_CONN)
db.seed_if_needed(_BOOT_CONN)
_BOOT_CONN.close()


class _ThreadConn:
    """把「当前线程的连接」伪装成连接对象：历史代码里的 CONN.xxx 全部照旧可用。

    v0.6.1：真正执行 SQL 的是 db.thread_conn()，每个请求线程一条连接，
    避免共享连接下的事务互相 commit/rollback（v0.6.1 并发实测的根因）。
    """

    __slots__ = ()

    def __getattr__(self, name):
        return getattr(db.thread_conn(), name)

    def __enter__(self):
        return db.thread_conn().__enter__()

    def __exit__(self, *exc_info):
        return db.thread_conn().__exit__(*exc_info)


CONN = _ThreadConn()
plugins.bind_conn(lambda: CONN)  # 插件开关的审计写入复用当前线程连接

# v0.4.1 多因子审批：engine 统一抛 EngineError，这里补齐新增错误码的可读中文文案（均 400）
_APPROVAL_ERROR_MESSAGES = {
    "INVALID_SMS": "短信验证码不正确或已过期，请重新获取",
    "INVALID_FACE": "人脸确认已过期，请重新发起",
    "INVALID_METHOD": "不支持的校验方式",
}

# v0.5.0 流式输出注册表：thread_id -> 当前流状态（进程内存；只存「停止输出」信号，不撤销已执行工具）
_STREAMS: dict = {}
_STREAMS_LOCK = threading.Lock()
_STREAM_CHUNK_CHARS = 12
_STREAM_DELAY_DEFAULT_MS = 18

# ---------------- v0.6.1 HTTP 边界（浏览器可达面收窄；默认演示形态不变） ----------------
# CORS：默认只允许本机 8000 端口的前端 Origin；同源请求（无 Origin）不带任何 ACAO 头。
DEFAULT_CORS_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"
# Host：防 DNS rebinding——只认回环主机名，可用 HOST_ALLOW 追加。
DEFAULT_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCAL_CLIENT_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# body：512 KiB 上限；413 先回包再限量丢弃请求体，避免客户端因 RST 收不到错误。
MAX_BODY_BYTES = 512 * 1024
MAX_DRAIN_BYTES = 4 * 1024 * 1024
# limit 参数统一 clamp 到 1..200（非法整数才是 400）。
LIMIT_MIN = 1
LIMIT_MAX = 200


class _HttpError(Exception):
    """已知的客户端错误：按契约回给调用方，堆栈只留给真正的未知异常。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _BadJSON(_HttpError, ValueError):
    """请求体不是合法 JSON → 400 BAD_JSON（插件/推送等既有接口仍按 INVALID_BODY 兜底）。"""

    def __init__(self, message: str = "请求体不是合法 JSON"):
        _HttpError.__init__(self, 400, "BAD_JSON", message)


class _PayloadTooLarge(_HttpError):
    """请求体超过 512 KiB → 413 PAYLOAD_TOO_LARGE。"""

    def __init__(self, length: int):
        _HttpError.__init__(self, 413, "PAYLOAD_TOO_LARGE", "请求体超过 512 KiB 上限")
        self.length = int(length or 0)


def _env_list(name: str, default: str = "") -> list:
    """读取逗号分隔的环境变量列表（空值回落 default）。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        raw = default
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _host_name(raw) -> str:
    """Host 头 → 纯主机名：允许端口、IPv6 方括号、结尾点，统一小写。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        name = urlparse("//" + text).hostname or ""
    except ValueError:
        return ""
    return name.rstrip(".").lower()


CORS_ALLOW_ORIGINS = set(_env_list("CORS_ALLOW_ORIGINS", DEFAULT_CORS_ORIGINS))
ALLOWED_HOSTS = DEFAULT_ALLOWED_HOSTS | {_host_name(h) for h in _env_list("HOST_ALLOW") if _host_name(h)}
# 可选令牌：非空时才启用鉴权（默认空=关闭，保持纯本机演示与既有测试行为）。
API_TOKEN = os.environ.get("API_TOKEN", "").strip()


def _clamp_int(query, name: str, default: int, lo=None, hi=None) -> int:
    """查询参数整数化：非整数 400 BAD_REQUEST，越界 clamp 到 [lo, hi]。"""
    raw = (query.get(name) or [str(default)])[0]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _HttpError(400, "BAD_REQUEST", "%s 必须是整数" % name)
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def _accounts_with_cards(accounts, cards) -> list:
    """账户与卡片按掩码关联，补出卡片状态与限额（只读；v0.7.1 账户页用）。

    账户与卡片没有外键，唯一可靠的对应关系是掩码后四位（**** 8821 → card_001）。
    """
    by_tail = {}
    for card in cards or []:
        masked = str(card.get("number_masked") or "")
        tail = masked.replace("*", "").strip()
        if tail:
            by_tail[tail] = card
    out = []
    for acc in accounts or []:
        row = dict(acc)
        masked = str(row.get("number_masked") or "")
        tail = masked.replace("*", "").strip()
        card = by_tail.get(tail)
        row["card_id"] = card.get("card_id", "") if card else ""
        row["card_kind"] = card.get("kind", "") if card else ""
        row["card_status"] = card.get("status", "") if card else ""
        row["single_limit_cents"] = int(card.get("single_limit_cents") or 0) if card else 0
        row["daily_limit_cents"] = int(card.get("daily_limit_cents") or 0) if card else 0
        out.append(row)
    return out


def _stream_delay_ms() -> int:
    """块间延迟（毫秒）：默认 18，环境变量 ZS_STREAM_DELAY_MS 覆盖（测试传 0 可秒回）。"""
    raw = os.environ.get("ZS_STREAM_DELAY_MS")
    if raw is None or str(raw).strip() == "":
        return _STREAM_DELAY_DEFAULT_MS
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return _STREAM_DELAY_DEFAULT_MS


def json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ZeroStep/" + config.VERSION

    # ---------- v0.6.1 边界校验 ----------
    def _request_origin(self) -> str:
        return str(self.headers.get("Origin") or "").strip()

    def _cors_headers(self) -> None:
        """仅当 Origin 命中白名单时回显该 Origin；同源/无 Origin 请求不带任何 ACAO。"""
        origin = self._request_origin()
        if origin and origin in CORS_ALLOW_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _client_is_local(self) -> bool:
        addr = str(self.client_address[0]) if self.client_address else ""
        return addr.split("%", 1)[0] in LOCAL_CLIENT_ADDRS

    def _demo_guard(self) -> bool:
        """演示因子/重置接口只接受本机回环连接（否则 403 DEMO_LOCAL_ONLY）。"""
        if self._client_is_local():
            return True
        self._error(403, "DEMO_LOCAL_ONLY", "演示接口仅允许本机访问")
        return False

    def _guard(self, method: str, path: str) -> bool:
        """Host 校验（防 DNS rebinding）+ 可选 API_TOKEN + 写请求 CSRF 来源校验。"""
        host = self.headers.get("Host")
        if host is not None and _host_name(host) not in ALLOWED_HOSTS:
            self._error(403, "HOST_DENIED", "Host 不在允许列表内")
            return False
        if API_TOKEN and method != "OPTIONS" and path != "/api/health":
            token = str(self.headers.get("X-ZeroStep-Token") or "")
            if not hmac.compare_digest(token, API_TOKEN):
                self._error(401, "UNAUTHORIZED", "缺少或错误的 X-ZeroStep-Token")
                return False
        if method in ("POST", "DELETE"):
            origin = self._request_origin()
            if origin and origin not in CORS_ALLOW_ORIGINS:
                self._error(403, "CORS_ORIGIN_DENIED", "请求来源不在 CORS 白名单内")
                return False
        return True

    def _discard_body(self, length: int) -> None:
        """413 已回包后限量丢弃请求体，避免客户端还没读完响应就吃到 TCP RST。"""
        left = min(int(length or 0), MAX_DRAIN_BYTES)
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)

    # ---------- 基础输出 ----------
    def _send(self, status: int, payload) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def _body(self):
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(str(raw_length).strip()) if raw_length not in (None, "") else 0
        except (TypeError, ValueError):
            raise _HttpError(400, "BAD_REQUEST", "Content-Length 不合法")
        if length < 0:
            raise _HttpError(400, "BAD_REQUEST", "Content-Length 不合法")
        if length > MAX_BODY_BYTES:
            raise _PayloadTooLarge(length)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _BadJSON()

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, fmt, *args):  # 安静一点，只保留错误
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("%s - %s " % (self.address_string(), fmt % args))

    def finish(self):  # v0.6.1：请求线程结束时回收它自己的 sqlite 连接
        try:
            super().finish()
        finally:
            db.close_thread_conn()

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        path = urlparse(self.path).path
        if not self._guard("OPTIONS", path):
            return
        origin = self._request_origin()
        if not origin or origin not in CORS_ALLOW_ORIGINS:
            return self._error(403, "CORS_ORIGIN_DENIED", "请求来源不在 CORS 白名单内")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-ZeroStep-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._guard("GET", path):
            return
        try:
            if path == "/api/health":
                return self._send(200, {"status": "ok", "app": config.APP_NAME, "version": config.VERSION,
                                        "llm_mode": llm.effective(),
                                        "demo_mode": config.DEMO_MODE, "seed_version": config.SEED_VERSION})
            if path.startswith("/api/session/"):
                sid = path.rsplit("/", 1)[-1]
                return self._send(200, engine.session_snapshot(CONN, sid))
            if path == "/api/accounts":
                sid = (self._query().get("session_id") or [""])[0]
                snap = engine.session_snapshot(CONN, sid)
                return self._send(200, {"accounts": _accounts_with_cards(snap["accounts"], snap["cards"]),
                                        "cards": snap["cards"],
                                        "total_cents": mock_core.account_total(CONN, snap["user"]["user_id"])})
            if path == "/api/transactions":
                q = self._query()
                sid = (q.get("session_id") or [""])[0]
                snap = engine.session_snapshot(CONN, sid)
                limit = _clamp_int(q, "limit", 20, LIMIT_MIN, LIMIT_MAX)
                account_id = (q.get("account_id") or [""])[0]
                user_id = snap["user"]["user_id"]
                if account_id:
                    owns = [a for a in snap["accounts"] if a.get("account_id") == account_id]
                    if not owns:
                        return self._error(400, "BAD_ACCOUNT", "没有这个账户，请检查 account_id")
                return self._send(200, {"transactions": mock_core.list_transactions(
                    CONN, user_id, account_id=account_id or None, limit=limit),
                    "account_id": account_id or None})
            # ---------- v0.7.0 新增：专区（仪表盘 / 分析），全部只读 ----------
            if path == "/api/dashboard":
                q = self._query()
                sid = (q.get("session_id") or [""])[0]
                snap = engine.session_snapshot(CONN, sid)
                window = (q.get("window") or ["30d"])[0]
                try:
                    return self._send(200, analytics.dashboard(CONN, snap["user"]["user_id"], window))
                except analytics.RangeError as exc:
                    return self._error(400, "BAD_RANGE", str(exc))
            if path == "/api/biz":
                q = self._query()
                sid = (q.get("session_id") or [""])[0]
                try:
                    snap = biz.overview(CONN, engine.session_snapshot(CONN, sid)["user"]["user_id"], sid)
                except biz.BizError as exc:
                    return self._error(400, "BAD_USER", str(exc))
                return self._send(200, snap)
            if path == "/api/analytics":
                q = self._query()
                sid = (q.get("session_id") or [""])[0]
                snap = engine.session_snapshot(CONN, sid)
                try:
                    return self._send(200, analytics.analytics(
                        CONN, snap["user"]["user_id"],
                        (q.get("from") or [""])[0], (q.get("to") or [""])[0],
                        (q.get("granularity") or ["month"])[0]))
                except analytics.RangeError as exc:
                    return self._error(400, "BAD_RANGE", str(exc))
            if path == "/api/audit":
                q = self._query()
                sid = (q.get("session_id") or [None])[0]
                limit = _clamp_int(q, "limit", 50, LIMIT_MIN, LIMIT_MAX)
                return self._send(200, {"entries": audit.entries(CONN, sid, limit), "chain": audit.verify(CONN)})
            if path == "/api/events":
                q = self._query()
                sid = (q.get("session_id") or [""])[0]
                after = _clamp_int(q, "after", 0, 0)
                rows = CONN.execute("SELECT seq, type, payload_json, ts FROM events WHERE session_id=? AND seq>? ORDER BY seq", (sid, after)).fetchall()
                events = [{"seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload_json"]), "ts": r["ts"]} for r in rows]
                return self._send(200, {"events": events})
            if path == "/api/llm/status":
                return self._send(200, llm.status())
            if path == "/api/tasks":
                return self._send(200, _tasks_page(self._query()))
            # ---------- v0.3.0 新增：插件注册表（仿 DSH 能力开关） ----------
            if path == "/api/plugins":
                try:
                    return self._send(200, plugins.status())
                except Exception:  # noqa: BLE001 - 插件表异常也要给出可读结果，不泄堆栈
                    return self._send(500, {"ok": False, "error": "服务不可用"})
            # ---------- v0.2.0 新增：MCP / 技能卡 / 渠道状态 ----------
            if path == "/api/mcp/status":
                try:
                    return self._send(200, _mcp_status())
                except Exception:  # noqa: BLE001 - 外部服务探测失败也要给出可读结果，不泄堆栈
                    return self._send(500, {"ok": False, "error": "服务不可用"})
            if path == "/api/skills":
                try:
                    return self._send(200, _skills_status())
                except Exception:  # noqa: BLE001
                    return self._send(500, {"ok": False, "error": "服务不可用"})
            if path.startswith("/api/skills/"):
                card_id = unquote(path.rsplit("/", 1)[-1])
                try:
                    card = skills.get(card_id)
                except Exception:  # noqa: BLE001
                    return self._send(500, {"ok": False, "error": "服务不可用"})
                if not card:
                    return self._error(404, "SKILL_NOT_FOUND", "技能卡不存在")
                return self._send(200, {"skill": card})
            if path == "/api/channel/status":
                try:
                    return self._send(200, _channel_status())
                except Exception:  # noqa: BLE001
                    return self._send(500, {"ok": False, "error": "服务不可用"})
            if path == "/api/scheduled":
                # 预约转账列表（只读）：数据层未就绪时给出空列表，接口形状保持稳定
                q = self._query()
                status = (q.get("status") or [""])[0]
                if status and status not in ("scheduled", "pending", "executed", "failed", "cancelled"):
                    return self._send(400, {"ok": False, "error": "status 只能是 scheduled/executed/failed/cancelled（pending 同 scheduled）"})
                if status == "scheduled":
                    status = "pending"
                try:
                    limit = max(1, min(int((q.get("limit") or ["6"])[0]), 50))
                except (TypeError, ValueError):
                    limit = 6
                try:
                    offset = max(0, int((q.get("offset") or ["0"])[0]))
                except (TypeError, ValueError):
                    offset = 0
                try:
                    data = mock_core.list_scheduled_transfers(CONN, config.DEFAULT_USER_ID, status)
                    all_items = (data or {}).get("items") or []
                except Exception:  # noqa: BLE001 - 数据层未就绪/查询失败时返回空页，不泄堆栈
                    all_items = []
                total = len(all_items)
                page = all_items[offset:offset + limit]
                items = [{
                    "sched_id": it.get("sched_id", ""),
                    "payee_name": it.get("payee_name", ""),
                    "payee_account_masked": it.get("payee_account_masked") or it.get("payee_account", ""),
                    "amount_cents": int(it.get("amount_cents") or 0),
                    "amount_display": mock_core.yuan(int(it.get("amount_cents") or 0)),
                    "run_at": it.get("run_at", ""),
                    "status": it.get("status", ""),
                } for it in page]
                return self._send(200, {"ok": True, "scheduled": items, "total": total,
                                        "offset": offset, "limit": limit,
                                        "has_more": offset + len(items) < total})
            if path == "/api/demo/totp":
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启，不返回动态码")
                if not self._demo_guard():
                    return
                user_id = (self._query().get("user_id") or [config.DEFAULT_USER_ID])[0]
                return self._send(200, {"user_id": user_id, "code": totp.current_code(user_id),
                                        "seconds_left": totp.seconds_left()})
            if path == "/api/demo/sms":
                # 演示专用：短信因子为内存模拟，直接回显验证码与有效期（不接真实短信网关）
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启，不返回短信验证码")
                if not self._demo_guard():
                    return
                user_id = (self._query().get("user_id") or [config.DEFAULT_USER_ID])[0]
                info = second_factor.sms_send(user_id)
                return self._send(200, {"ok": True, "user_id": user_id, "code": info.get("code") or "",
                                        "seconds_left": int(info.get("seconds_left") or 0),
                                        "ttl": int(info.get("ttl") or 0)})
            if path.startswith("/api/"):
                return self._error(404, "NOT_FOUND", "接口不存在")
            return self._static(path)
        except engine.EngineError as exc:
            return self._error(exc.http_status, exc.code, exc.message)
        except _HttpError as exc:
            return self._error(exc.status, exc.code, exc.message)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "INTERNAL_ERROR", "服务器内部错误")

    def do_POST(self):
        global CONN
        path = urlparse(self.path).path
        if not self._guard("POST", path):
            return
        # v0.6.1 边界：即使接口不读 body（如 /api/session），也先按 Content-Length 拦超大请求体，
        # 否则「512 KiB 上限」只对调用 _body() 的接口生效。
        declared = self.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_len = int(declared)
            except (TypeError, ValueError):
                declared_len = -1
            if declared_len < 0:
                self._error(400, "BAD_REQUEST", "Content-Length 不合法")
                return
            if declared_len > MAX_BODY_BYTES:
                self._error(413, "PAYLOAD_TOO_LARGE", "请求体超过 512 KiB 上限")
                if declared_len <= MAX_DRAIN_BYTES:
                    self._discard_body(declared_len)
                self.close_connection = True
                return
        if self.headers.get("Transfer-Encoding", "").lower().strip() == "chunked":
            # 分块传输无法预知大小：直接拒绝，避免绕过 Content-Length 上限。
            self._error(411, "LENGTH_REQUIRED", "不支持分块传输，请提供 Content-Length")
            self.close_connection = True
            return
        try:
            if path == "/api/demo/identity":
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启")
                body = self._body()
                target = str(body.get("user_id") or "").strip()
                if not target:
                    return self._error(400, "BAD_USER", "缺少 user_id")
                row = CONN.execute("SELECT user_id FROM users WHERE user_id=?", (target,)).fetchone()
                if not row:
                    return self._error(400, "BAD_USER", "没有这个用户：%s" % target)
                created = engine.create_session(CONN, target)
                return self._send(200, engine.session_snapshot(CONN, created["session_id"]))
            if path == "/api/session":
                created = engine.create_session(CONN)
                snap = engine.session_snapshot(CONN, created["session_id"])
                return self._send(200, snap)
            if path == "/api/admin/reset":
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启")
                if not self._demo_guard():
                    return
                # 同一连接内做表级重置，避免 Windows 下删除被占用的 bank.db 抛 WinError 32
                db.reset_demo(CONN)
                plugins.reset()  # 插件状态一并回到默认「全部启用」
                return self._send(200, {"reset": True, "seed_version": config.SEED_VERSION})
            if path == "/api/turn/cancel":
                # v0.5.0：只停止「输出」，不撤销已执行工具；不在流式中则如实返回 cancelled=false
                body = self._body()
                if not isinstance(body, dict):
                    return self._error(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                thread_id = str(body.get("thread_id") or "").strip()
                state = None
                emitted = 0
                with _STREAMS_LOCK:
                    state = _STREAMS.get(thread_id) if thread_id else None
                    if state is not None:
                        state["stop"] = True
                        emitted = int(state.get("emitted_chunks") or 0)
                if state is None:
                    return self._send(200, {"cancelled": False, "reason": "not_streaming"})
                return self._send(200, {"cancelled": True, "emitted_chunks": emitted})
            if path == "/api/chat":
                body = self._body()
                if not isinstance(body, dict):
                    return self._error(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                sid = body.get("session_id") or ""
                raw_text = body.get("text") or ""
                if not isinstance(sid, str) or not isinstance(raw_text, str):
                    return self._error(400, "BAD_REQUEST", "session_id 与 text 必须是字符串")
                text = raw_text.strip()
                if not sid or not text:
                    return self._error(400, "BAD_REQUEST", "缺少 session_id 或 text")
                if body.get("stream"):
                    # v0.5.0：stream_text=false 时保持旧「整段 message」行为（老测试兼容）
                    return self._stream_chat(sid, text, body.get("stream_text") is not False)
                result = engine.run_turn(CONN, sid, text)
                return self._send(200, _public(result))
            if path == "/api/demo/face":
                # 演示专用：人脸因子为内存模拟，签发 60 秒有效的一次性令牌（不接真实人脸 SDK）
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启，不返回人脸令牌")
                if not self._demo_guard():
                    return
                body = self._body()
                if not isinstance(body, dict):
                    return self._error(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                user_id = body.get("user_id") or config.DEFAULT_USER_ID
                if not isinstance(user_id, str):
                    return self._error(400, "BAD_REQUEST", "user_id 必须是字符串")
                info = second_factor.face_scan(user_id)
                return self._send(200, {"ok": True, "user_id": user_id, "token": info.get("token") or "",
                                        "seconds_left": int(info.get("seconds_left") or 0),
                                        "ttl": int(info.get("ttl") or 0)})
            if path == "/api/approval":
                body = self._body()
                if not isinstance(body, dict):
                    return self._error(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                method = body.get("method") or ""
                if not isinstance(method, str):
                    return self._error(400, "INVALID_METHOD", _APPROVAL_ERROR_MESSAGES["INVALID_METHOD"])
                try:
                    result = engine.resume(CONN, body.get("session_id") or "", body.get("approval_id") or "",
                                           body.get("decision") or "", body.get("code") or "", method=method)
                except engine.EngineError as exc:
                    code = str(getattr(exc, "code", "") or "")
                    if code in _APPROVAL_ERROR_MESSAGES:
                        return self._error(400, code, _APPROVAL_ERROR_MESSAGES[code])
                    raise
                return self._send(200, _public(result))
            if path == "/api/llm/mode":
                body = self._body()
                if not isinstance(body, dict):
                    return self._error(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                mode = body.get("mode") or ""
                if not isinstance(mode, str) or not llm.set_mode(mode):
                    return self._error(400, "BAD_MODE", "mode 只能是 rule / llm / auto")
                status = llm.status()
                status["switched"] = True
                return self._send(200, status)
            if path == "/api/llm/test":
                # 一键诊断：无论成功或失败一律 200，返回可读原因与修复建议；不写审计链
                return self._send(200, llm.test_connection())
            if path.startswith("/api/plugins/") and path.endswith("/toggle"):
                plugin_id = unquote(path[len("/api/plugins/"):-len("/toggle")].strip("/"))
                try:
                    body = self._body()
                except (ValueError, TypeError):
                    return self._error(400, "INVALID_BODY", "参数不合法")
                if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
                    return self._error(400, "INVALID_BODY", "参数不合法")
                try:
                    plugin = plugins.set_enabled(plugin_id, bool(body["enabled"]), conn=CONN)
                except plugins.PluginNotFound:
                    return self._error(404, "PLUGIN_NOT_FOUND", "插件不存在")
                except plugins.PluginLocked:
                    return self._error(403, "PLUGIN_CORE_LOCKED", "核心能力不可停用")
                except Exception:  # noqa: BLE001 - 插件开关失败要给可读错误，不泄堆栈
                    return self._send(500, {"ok": False, "error": "服务不可用"})
                return self._send(200, {"ok": True, "plugin": plugin, "counts": plugins.counts()})
            if path == "/api/push":
                # 主动触达：只读数据库生成文案、写审计、可选转发渠道；失败一律 200 带可读原因
                try:
                    body = self._body()
                except (ValueError, TypeError):
                    return self._error(400, "INVALID_BODY", "请求体不是合法 JSON")
                if not isinstance(body, dict):
                    return self._error(400, "INVALID_BODY", "请求体必须是 JSON 对象")
                kind = body.get("kind") or push.DEFAULT_KIND
                if not isinstance(kind, str) or kind.strip().lower() not in push.KINDS:
                    return self._error(400, "BAD_KIND", "kind 只能是 %s" % " / ".join(push.KINDS))
                user_id = body.get("user_id") or config.DEFAULT_USER_ID
                if not isinstance(user_id, str):
                    return self._error(400, "BAD_REQUEST", "user_id 必须是字符串")
                return self._send(200, push.push(CONN, kind, user_id))
            if path == "/api/scheduler/tick":
                # 手动触发一次到期检查（演示/测试用）；后台线程也调用同一 tick
                mod = _scheduler_mod()
                if mod is None:
                    return self._send(200, {"ok": True, "checked": 0, "executed": [], "failed": [],
                                            "ts": db.now_iso(), "note": "调度器未启用"})
                try:
                    result = mod.tick(CONN) or {}
                except Exception:  # noqa: BLE001 - 单次调度失败不能影响服务
                    return self._send(500, {"ok": False, "error": "调度执行失败"})
                return self._send(200, {"ok": True, "checked": int(result.get("checked") or 0),
                                        "executed": result.get("executed") or [], "failed": result.get("failed") or [],
                                        "ts": result.get("ts") or db.now_iso()})
            if path == "/api/undo":
                body = self._body()
                if not isinstance(body, dict):
                    return self._error(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                result = engine.undo_action(CONN, body.get("session_id") or "", body.get("action_id") or "")
                return self._send(200, result)
            return self._error(404, "NOT_FOUND", "接口不存在")
        except engine.EngineError as exc:
            return self._error(exc.http_status, exc.code, exc.message)
        except _HttpError as exc:
            self._error(exc.status, exc.code, exc.message)
            if exc.status == 413:
                self._discard_body(getattr(exc, "length", 0))
                self.close_connection = True
            return
        except ValueError as exc:
            return self._error(400, "BAD_REQUEST", str(exc))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "INTERNAL_ERROR", "服务器内部错误")

    # ---------- SSE ----------
    def _stream_chat(self, sid: str, text: str, stream_text: bool = True) -> None:
        """v0.5.0：结构化事件照旧实时下发；最终回答按 contract 逐字流式。

        事件序（冻结）：answer_start → answer_delta ×n → answer_end → [insight_card] → done。
        stream_text=False 时退回旧行为：message 事件整段下发（老测试兼容）。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors_headers()
        self.end_headers()

        def write_event(event_type: str, payload) -> None:
            frame = "event: %s\ndata: %s\n\n" % (event_type, json.dumps(payload, ensure_ascii=False))
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

        if stream_text:
            def sink(event_type: str, payload) -> None:
                # 最终回答改由 answer_* 下发：过滤 message，避免前端先整段渲染再重复建气泡
                if event_type == "message":
                    return
                write_event(event_type, payload)
        else:
            sink = write_event

        turn_id = "turn_" + secrets.token_hex(4)
        stream_key = ""
        stream_state = None
        try:
            started = time.time()
            final = engine.run_turn(CONN, sid, text, sink=sink)
            reply = str(final.get("reply") or "")
            thread_id = str(final.get("thread_id") or "")
            cancelled = False
            chunks = 0
            if stream_text and reply:
                stream_state = {"thread_id": thread_id, "turn_id": turn_id, "stop": False,
                                "emitted_chunks": 0, "chars_total": len(reply)}
                if thread_id:
                    stream_key = thread_id
                    with _STREAMS_LOCK:
                        _STREAMS[thread_id] = stream_state
                write_event("answer_start", {"turn_id": turn_id, "thread_id": thread_id, "chars": len(reply)})
                delay = _stream_delay_ms()
                for i, offset in enumerate(range(0, len(reply), _STREAM_CHUNK_CHARS)):
                    with _STREAMS_LOCK:
                        stopped = bool(stream_state.get("stop"))
                    if stopped:
                        cancelled = True
                        break
                    piece = reply[offset:offset + _STREAM_CHUNK_CHARS]
                    stream_state["emitted_chunks"] = i + 1
                    write_event("answer_delta", {"turn_id": turn_id, "i": i, "text": piece})
                    chunks = i + 1
                    if delay > 0 and offset + _STREAM_CHUNK_CHARS < len(reply):
                        time.sleep(delay / 1000.0)
                if not cancelled:
                    with _STREAMS_LOCK:
                        cancelled = bool(stream_state.get("stop"))
                write_event("answer_end", {"turn_id": turn_id, "chunks": chunks,
                                           "elapsed_ms": int((time.time() - started) * 1000),
                                           "cancelled": cancelled})
                if cancelled:
                    # 只停输出：已执行的工具不回滚；补 note 让前端显示「已停止输出」
                    write_event("note", {"text": "已停止输出"})
                    try:
                        audit.append(CONN, "answer_cancelled", session_id=sid, tool="stream",
                                     args={"thread_id": thread_id, "emitted_chunks": chunks,
                                           "chars_total": len(reply)},
                                     result={"cancelled": True}, risk_level="low")
                    except Exception:  # noqa: BLE001 - 审计失败不能反向影响停止输出
                        traceback.print_exc()
            if final.get("insight"):
                write_event("insight_card", {"turn_id": turn_id, "thread_id": thread_id,
                                             "insight": final.get("insight")})
            write_event("done", {"status": final.get("status"), "thread_id": thread_id,
                                 "undo": final.get("undo"), "approval": final.get("approval"),
                                 "turn_id": turn_id})
        except engine.EngineError as exc:
            write_event("error", {"code": exc.code, "message": exc.message})
            write_event("done", {"status": "error"})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                write_event("error", {"code": "INTERNAL_ERROR", "message": "服务器内部错误"})
                write_event("done", {"status": "error"})
            except Exception:
                pass
        finally:
            if stream_key:
                with _STREAMS_LOCK:
                    _STREAMS.pop(stream_key, None)

    # ---------- 静态文件 ----------
    def _static(self, path: str) -> None:
        root = Path(config.FRONTEND_DIR).resolve()
        rel = path.lstrip("/")
        target = (root / rel).resolve() if rel else root / "index.html"
        if not str(target).startswith(str(root)):
            return self._error(403, "FORBIDDEN", "非法路径")
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            if rel and "." in Path(rel).name:
                return self._error(404, "NOT_FOUND", "文件不存在")
            target = root / "index.html"
            if not target.exists():
                return self._error(503, "FRONTEND_MISSING", "前端文件尚未就绪，请先构建/放置 frontend/index.html")
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)


def _public(result: dict) -> dict:
    """裁剪掉不需要回传的大字段。"""
    out = dict(result)
    out.pop("events", None)
    return out


def _list_tasks():
    tasks = []
    d = Path(config.TASKS_DIR)
    if not d.exists():
        return tasks
    for f in sorted(d.glob("t*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        tasks.append({"id": data.get("id"), "title": data.get("title"), "category": data.get("category"),
                      "user_input": (data.get("turns") or [""])[0], "expect": data.get("expect")})
    return tasks


def _to_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _tasks_page(query) -> dict:
    """快捷指令分页：只有传 limit 才分页；无参数时返回与旧版完全一致的全量。

    limit 合法区间 1..50，非法/<=0/非数字回落 6；offset 非法/负数按 0 处理，
    越界返回空列表（200，不报错）。has_more = offset + len(tasks) < total。
    """
    tasks = _list_tasks()
    if "limit" not in query:
        return {"tasks": tasks}
    limit = _to_int((query.get("limit") or ["6"])[0], 6)
    if limit < 1 or limit > 50:
        limit = 6
    offset = _to_int((query.get("offset") or ["0"])[0], 0)
    if offset < 0:
        offset = 0
    total = len(tasks)
    page = tasks[offset:offset + limit] if offset < total else []
    return {"tasks": page, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(page) < total}


def _mask_token(token: str) -> str:
    """令牌脱敏展示：只保留前 4 位与末 2 位，绝不回传明文。"""
    token = str(token or "")
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "***" + token[-2:]


_MCP_PLUGIN_IDS = {"fx": "mcp-fx", "risk": "mcp-risk"}


def _mcp_plugin_id(name: str) -> str:
    """服务名 -> 插件 id：内置 fx/risk 用固定映射，其余 MCP_SERVERS 服务统一 mcp-<name>。"""
    name = str(name or "")
    if name in _MCP_PLUGIN_IDS:
        return _MCP_PLUGIN_IDS[name]
    return "mcp-" + name if name else ""


def _mcp_status() -> dict:
    """MCP 状态超集：保留 mcp_client.status() 既有字段，只增不改。

    - 每个 server 补 status（ok -> "ok"，否则 "degraded"）；
    - 对应插件被停用时 status="disabled"、ok=false、degraded=true、last_error="插件已停用"
      （插件停用优先于运行态展示；MCP_ENABLED=0 的「整体关闭」语义保持不变）；
    - 顶层补 ok=true（接口本身可用）。前端已按这些字段渲染，故既有字段一个不少。
    - v0.6.0 起服务列表可来自 MCP_SERVERS（银行开放平台等），插件 id 统一 mcp-<name>；
      label/transport/auth 由 mcp_client.status() 提供，前端按缺省回退，不影响旧渲染。
    """
    body = mcp_client.status()
    servers = []
    for item in body.get("servers") or []:
        row = dict(item)
        plugin_id = _mcp_plugin_id(row.get("name"))
        if plugin_id and not plugins.is_enabled(plugin_id):
            row["status"] = "disabled"
            row["ok"] = False
            row["degraded"] = True
            row["last_error"] = "插件已停用"
        else:
            row["status"] = "ok" if row.get("ok") else "degraded"
        servers.append(row)
    body["servers"] = servers
    body["ok"] = True
    return body


def _skills_status() -> dict:
    """技能卡状态超集：保留 skills.status() 既有字段，只增不改。

    每张卡补 risk（按工具动钱程度静态分级）与 tool（首个工具名，缺省 read_only），
    前端已按这两个字段着色/展示，故不动既有字段；未知 id 兜底 medium。
    skill-pack 插件停用时整体返回空列表，并带 disabled=true。
    """
    if not plugins.is_enabled("skill-pack"):
        return {"count": 0, "skills": [], "disabled": True}
    body = skills.status()
    risk_map = {
        "bills": "low",
        "subscriptions": "low",
        "cards": "medium",
        "wealth": "medium",
        "transfer": "high",
        "cross": "high",
    }
    cards = []
    for item in body.get("skills") or []:
        row = dict(item)
        row["risk"] = risk_map.get(str(row.get("id") or ""), "medium")
        tools = row.get("tools") or []
        row["tool"] = str(tools[0]) if tools else "read_only"
        cards.append(row)
    body["skills"] = cards
    return body


def _channel_status() -> dict:
    """渠道服务状态：配置、webhook 地址与最近几条投递记录（不含令牌明文）。

    每次调用都从 config 现读，便于演示时改环境变量后热切换；日志按行 JSON，
    损坏行直接跳过，不影响接口可用性。会话数每次现读 sessions.json，失败按 0。
    """
    enabled = bool(getattr(config, "CHANNEL_ENABLED", False)) and plugins.is_enabled("channel-im")
    port = int(getattr(config, "CHANNEL_PORT", 8400) or 8400)
    token = str(getattr(config, "CHANNEL_TOKEN", "") or "")
    log_path = Path(getattr(config, "CHANNEL_MESSAGE_LOG", BASE / "channels" / "messages.jsonl"))
    recent = []
    try:
        if log_path.exists():
            lines = [line for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
            for line in lines[-5:]:
                try:
                    recent.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        recent = []
    sessions = 0
    sessions_path = Path(getattr(config, "CHANNEL_SESSIONS_FILE", Path(log_path).parent / "sessions.json"))
    if enabled:  # 渠道插件停用时按 0 展示，其余字段保留
        try:
            if sessions_path.exists():
                session_data = json.loads(sessions_path.read_text(encoding="utf-8"))
                if isinstance(session_data, dict):
                    sessions = len(session_data)
        except (OSError, ValueError):
            sessions = 0
    return {
        "enabled": enabled,
        "port": port,
        "webhook_url": "http://127.0.0.1:%d/message" % port,
        "token_set": bool(token),
        "token_configured": bool(token),
        "token_masked": _mask_token(token),
        "sessions": sessions,
        "message_log": str(log_path),
        "recent": recent,
    }


def _scheduler_mod():
    """预约转账调度器：模块缺失时返回 None，服务照常启动（手动 tick 也返回空结果）。"""
    try:
        from app import scheduler
        return scheduler
    except Exception:  # noqa: BLE001
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    httpd = Server((host, port), Handler)
    mod = _scheduler_mod()
    if mod is not None:
        try:
            mod.start_background()
        except Exception:  # noqa: BLE001 - 调度器起不来也要能演示
            print("调度器启动失败，预约转账只能手动触发")
    print("ZeroStep 已在 http://%s:%d 启动（LLM 模式：%s，演示模式：%s）" %
          (host, port, llm.effective(), "开" if config.DEMO_MODE else "关"))
    if config.DEMO_MODE:
        print("演示模式：二次因子接口仅本机可用（/api/demo/* 与 /api/admin/reset 仅接受 127.0.0.1 / ::1）")
    print("CORS 白名单：%s；Host 白名单：%s" %
          (", ".join(sorted(CORS_ALLOW_ORIGINS)) or "（空）", ", ".join(sorted(ALLOWED_HOSTS))))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if mod is not None:
            try:
                mod.stop_background()
            except Exception:  # noqa: BLE001
                pass
        httpd.server_close()


if __name__ == "__main__":
    main()
