"""HTTP 冒烟测试：启动真实服务器并走一遍 REST/SSE/审批/撤销/审计。"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8011"


def call(method, path, payload=None, raw=False):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            if raw:
                return resp.status, body
            return resp.status, json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body


def wait_server(proc):
    for _ in range(60):
        try:
            code, body = call("GET", "/api/health")
            if code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is not None:
            return False
        time.sleep(0.4)
    return False


def main():
    for suffix in ("", "-wal", "-shm"):
        p = ROOT / "backend" / "data" / ("bank.db" + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    proc = subprocess.Popen(
        [sys.executable, "backend/server.py"],
        cwd=str(ROOT),
        env={**__import__("os").environ, "PORT": "8011", "DEMO_MODE": "1", "ZEROSTEP_DISABLE_LLM": "1", "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    failures = []
    try:
        if not wait_server(proc):
            print("服务器未启动")
            return 1
        print("[OK] /api/health")
        status, session = call("POST", "/api/session", {"user_id": "u001"})
        assert status == 200, session
        sid = session["session_id"]
        print("[OK] /api/session", sid, "accounts:", len(session["accounts"]))
        status, tasks = call("GET", "/api/tasks")
        print("[OK] /api/tasks", len(tasks.get("tasks", [])))
        status, chat = call("POST", "/api/chat", {"session_id": sid, "text": "看看我这个月的账单"})
        assert status == 200 and chat["status"] == "ok", chat
        print("[OK] /api/chat 账单:", chat["reply"][:60])
        status, chat = call("POST", "/api/chat", {"session_id": sid, "text": "给妈妈转 800 元"})
        assert chat["status"] == "awaiting_approval", chat
        ap = chat["approval"]
        print("[OK] 审批请求:", ap["risk_level"], ap["challenge"]["type"], ap["approval_id"])
        status, totp = call("GET", "/api/demo/totp")
        code = totp["code"]
        status, done = call("POST", "/api/approval", {"session_id": sid, "approval_id": ap["approval_id"], "decision": "approve", "code": code})
        assert done["status"] == "ok", done
        print("[OK] 审批执行:", done["reply"][:60])
        action_id = (done.get("undo") or {}).get("action_id")
        assert action_id, done
        status, undone = call("POST", "/api/undo", {"session_id": sid, "action_id": action_id})
        print("[OK] 撤销:", str(undone.get("reply") if isinstance(undone, dict) else undone)[:80])
        status, audit = call("GET", "/api/audit?session_id=" + sid)
        assert audit["chain"]["valid"], audit["chain"]
        print("[OK] 审计链:", audit["chain"]["checked"], "条事件，valid=True")
        status, html = call("GET", "/", raw=True)
        assert status == 200 and b"ZeroStep" in html, status
        print("[OK] 首页 index.html", len(html), "字节")
        status, css = call("GET", "/styles.css", raw=True)
        assert status == 200, status
        status, js = call("GET", "/app.js", raw=True)
        assert status == 200, status
        print("[OK] 静态资源 styles.css / app.js")
        status, reset = call("POST", "/api/admin/reset", {})
        assert status == 200, reset
        status, session2 = call("POST", "/api/session", {"user_id": "u001"})
        assert session2["accounts"][0]["balance_cents"] == 1286050, session2["accounts"]
        print("[OK] /api/admin/reset 后余额回到种子数据")
        stream_req = urllib.request.Request(
            BASE + "/api/chat",
            data=json.dumps({"session_id": session2["session_id"], "text": "查一下我工资卡余额", "stream": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(stream_req, timeout=20) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8")
        assert "text/event-stream" in ctype, ctype
        events = [line for line in raw.splitlines() if line.startswith("event: ")]
        assert any("done" in line for line in events), raw[:300]
        print("[OK] SSE 流式对话，事件数:", len(events))
    except AssertionError as exc:
        failures.append(str(exc))
        print("[FAIL]", exc)
    finally:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        if failures:
            print("服务器 stderr 尾部:")
            print(err.decode("utf-8", "replace")[-2000:])
        for line in err.decode("utf-8", "replace").splitlines()[-5:]:
            if "Traceback" in line or "Error" in line:
                print("[server]", line)
    if failures:
        print("HTTP 冒烟测试失败")
        return 1
    print("HTTP 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
