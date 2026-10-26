"""ZeroStep HTTP 服务：零第三方依赖（http.server + sqlite3 + SSE）。

启动：
    python backend/server.py            # 默认 http://127.0.0.1:8000
    PORT=8080 python backend/server.py

接口契约见 docs/api.md。结构化事件（tool_start/tool_end/approval_required/message）
通过 SSE 推给前端，同时持久化到 events 表，可回放、可审计。
"""
import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app import audit, config, db, engine, mock_core, totp  # noqa: E402

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")

CONN = db.connect()
db.init_schema(CONN)
db.seed_if_needed(CONN)


def json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ZeroStep/" + config.VERSION

    # ---------- 基础输出 ----------
    def _send(self, status: int, payload) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise ValueError("请求体不是合法 JSON")

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, fmt, *args):  # 安静一点，只保留错误
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("%s - %s " % (self.address_string(), fmt % args))

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                return self._send(200, {"status": "ok", "app": config.APP_NAME, "version": config.VERSION,
                                        "llm_mode": "deepseek" if config.LLM_ENABLED else "rule",
                                        "demo_mode": config.DEMO_MODE, "seed_version": config.SEED_VERSION})
            if path.startswith("/api/session/"):
                sid = path.rsplit("/", 1)[-1]
                return self._send(200, engine.session_snapshot(CONN, sid))
            if path == "/api/accounts":
                sid = (self._query().get("session_id") or [""])[0]
                snap = engine.session_snapshot(CONN, sid)
                return self._send(200, {"accounts": snap["accounts"], "cards": snap["cards"],
                                        "total_cents": mock_core.account_total(CONN, snap["user"]["user_id"])})
            if path == "/api/transactions":
                sid = (self._query().get("session_id") or [""])[0]
                snap = engine.session_snapshot(CONN, sid)
                limit = int((self._query().get("limit") or ["20"])[0])
                return self._send(200, {"transactions": mock_core.list_transactions(CONN, snap["user"]["user_id"], limit=limit)})
            if path == "/api/audit":
                q = self._query()
                sid = (q.get("session_id") or [None])[0]
                limit = int((q.get("limit") or ["50"])[0])
                return self._send(200, {"entries": audit.entries(CONN, sid, limit), "chain": audit.verify(CONN)})
            if path == "/api/events":
                q = self._query()
                sid = (q.get("session_id") or [""])[0]
                after = int((q.get("after") or ["0"])[0])
                rows = CONN.execute("SELECT seq, type, payload_json, ts FROM events WHERE session_id=? AND seq>? ORDER BY seq", (sid, after)).fetchall()
                events = [{"seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload_json"]), "ts": r["ts"]} for r in rows]
                return self._send(200, {"events": events})
            if path == "/api/tasks":
                return self._send(200, {"tasks": _list_tasks()})
            if path == "/api/demo/totp":
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启，不返回动态码")
                user_id = (self._query().get("user_id") or [config.DEFAULT_USER_ID])[0]
                return self._send(200, {"user_id": user_id, "code": totp.current_code(user_id),
                                        "seconds_left": totp.seconds_left()})
            if path.startswith("/api/"):
                return self._error(404, "NOT_FOUND", "接口不存在")
            return self._static(path)
        except engine.EngineError as exc:
            return self._error(exc.http_status, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "INTERNAL", str(exc))

    def do_POST(self):
        global CONN
        path = urlparse(self.path).path
        try:
            if path == "/api/session":
                created = engine.create_session(CONN)
                snap = engine.session_snapshot(CONN, created["session_id"])
                return self._send(200, snap)
            if path == "/api/admin/reset":
                if not config.DEMO_MODE:
                    return self._error(403, "DEMO_MODE_DISABLED", "演示模式未开启")
                # 同一连接内做表级重置，避免 Windows 下删除被占用的 bank.db 抛 WinError 32
                db.reset_demo(CONN)
                return self._send(200, {"reset": True, "seed_version": config.SEED_VERSION})
            if path == "/api/chat":
                body = self._body()
                sid = body.get("session_id") or ""
                text = (body.get("text") or "").strip()
                if not sid or not text:
                    return self._error(400, "BAD_REQUEST", "缺少 session_id 或 text")
                if body.get("stream"):
                    return self._stream_chat(sid, text)
                result = engine.run_turn(CONN, sid, text)
                return self._send(200, _public(result))
            if path == "/api/approval":
                body = self._body()
                result = engine.resume(CONN, body.get("session_id") or "", body.get("approval_id") or "",
                                       body.get("decision") or "", body.get("code") or "")
                return self._send(200, _public(result))
            if path == "/api/undo":
                body = self._body()
                result = engine.undo_action(CONN, body.get("session_id") or "", body.get("action_id") or "")
                return self._send(200, result)
            return self._error(404, "NOT_FOUND", "接口不存在")
        except engine.EngineError as exc:
            return self._error(exc.http_status, exc.code, exc.message)
        except ValueError as exc:
            return self._error(400, "BAD_REQUEST", str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "INTERNAL", str(exc))

    # ---------- SSE ----------
    def _stream_chat(self, sid: str, text: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def write_event(event_type: str, payload) -> None:
            frame = "event: %s\ndata: %s\n\n" % (event_type, json.dumps(payload, ensure_ascii=False))
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

        final = {}
        try:
            final = engine.run_turn(CONN, sid, text, sink=write_event)
            write_event("done", {"status": final.get("status"), "thread_id": final.get("thread_id"),
                                 "undo": final.get("undo"), "approval": final.get("approval")})
        except engine.EngineError as exc:
            write_event("error", {"code": exc.code, "message": exc.message})
            write_event("done", {"status": "error"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                write_event("error", {"code": "INTERNAL", "message": str(exc)})
                write_event("done", {"status": "error"})
            except Exception:
                pass

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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    httpd = Server((host, port), Handler)
    print("ZeroStep 已在 http://%s:%d 启动（LLM 模式：%s，演示模式：%s）" %
          (host, port, "deepseek" if config.LLM_ENABLED else "rule", "开" if config.DEMO_MODE else "关"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
