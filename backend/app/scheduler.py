"""预约转账到期执行器：独立连接 + 可选后台线程。"""
import os
import threading

from . import audit, config, db, mock_core

_LOCK = threading.RLock()
_THREAD = None
_STOP = threading.Event()
_STATE = {
    "interval_s": 5,
    "last_tick_at": "",
    "last_executed": 0,
    "last_checked": 0,
    "last_error": "",
}


def _setting(name: str, default):
    """优先读 config 中的同名常量（便于测试 reload），否则读环境变量。"""
    value = getattr(config, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _enabled() -> bool:
    raw = str(_setting("SCHEDULER_ENABLED", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _interval(default: int = 5) -> int:
    try:
        value = int(float(_setting("SCHEDULER_INTERVAL", default)))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def tick(conn=None, now_iso: str = "") -> dict:
    """执行一次到期预约并写审计。

    入参 conn 为空时自建独立连接（调用方不传 conn 的典型场景是 server 的 /api/scheduler/tick）。
    每条 executed 写 scheduled_transfer_executed（risk_level=medium），
    每条 failed 写 scheduled_transfer_failed（risk_level=high）。
    """
    own = conn is None
    if own:
        conn = db.connect(config.DB_PATH)
    try:
        result = mock_core.execute_due_transfers(conn, now_iso)
        executed = result.get("executed") or []
        failed = result.get("failed") or []
        for item in executed:
            audit.append(
                conn, "scheduled_transfer_executed", session_id="", tool="create_transfer",
                args={"sched_id": item.get("sched_id"), "amount_cents": item.get("amount_cents")},
                result={"transfer_id": item.get("transfer_id")}, risk_level="medium",
            )
        for item in failed:
            audit.append(
                conn, "scheduled_transfer_failed", session_id="", tool="create_transfer",
                args={"sched_id": item.get("sched_id"), "amount_cents": item.get("amount_cents")},
                result={"code": item.get("code"), "message": item.get("message")}, risk_level="high",
            )
        with _LOCK:
            _STATE["last_tick_at"] = db.now_iso()
            _STATE["last_executed"] = len(executed)
            _STATE["last_checked"] = int(result.get("checked") or 0)
        return result
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def _loop(stop_event: threading.Event, interval: int) -> None:
    """后台循环：每 interval 秒自建连接跑一次 tick；异常吞掉只记 last_error。"""
    while not stop_event.is_set():
        try:
            conn = db.connect(config.DB_PATH)
            try:
                tick(conn)
                with _LOCK:
                    _STATE["last_error"] = ""
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:  # 调度线程绝不把异常抛到主线程
            with _LOCK:
                _STATE["last_error"] = str(exc)[:200]
        if stop_event.wait(max(1, int(interval))):
            break


def start_background(interval: int = 5) -> dict:
    """启动后台调度线程（同一进程只起一次）；SCHEDULER_ENABLED=0 时直接返回。"""
    global _THREAD, _STOP
    interval = _interval(interval)
    if not _enabled():
        return {"started": False, "interval": interval, "running": False}
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            _STATE["interval_s"] = interval
            return {"started": False, "interval": interval, "running": True}
        stop_event = threading.Event()
        _STOP = stop_event
        _STATE.update({"interval_s": interval, "last_error": ""})
        thread = threading.Thread(target=_loop, args=(stop_event, interval), name="zerostep-scheduler", daemon=True)
        _THREAD = thread
        thread.start()
    return {"started": True, "interval": interval, "running": True}


def stop_background() -> None:
    """停止后台调度线程（可重复调用，之后可再次 start_background）。"""
    global _THREAD
    with _LOCK:
        thread = _THREAD
        stop_event = _STOP
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=3)
    with _LOCK:
        if _THREAD is thread:
            _THREAD = None


def status() -> dict:
    """调度器状态：{running, interval_s, last_tick_at, last_executed, last_checked}。"""
    with _LOCK:
        running = bool(_THREAD is not None and _THREAD.is_alive())
        return {
            "running": running,
            "interval_s": int(_STATE.get("interval_s", 5)),
            "last_tick_at": str(_STATE.get("last_tick_at", "")),
            "last_executed": int(_STATE.get("last_executed", 0)),
            "last_checked": int(_STATE.get("last_checked", 0)),
            "last_error": str(_STATE.get("last_error", "")),
        }
