"""会话安全锁（v0.4.1）：连续可疑或被拦行为达到阈值后，临时锁定会话。

设计要点
--------
* 锁表 session_locks 由本模块兜底建表（db.py 里也有，IF NOT EXISTS 幂等），
  这样即使 db 迁移晚于引擎接线也不会 500。
* 安全事件计数不另存计数器，直接数审计表里本会话的 attack_blocked / turn_blocked 事件，
  与审计链同源：重启后依然成立，也不会出现"计数器和审计对不上"的口径问题。
* 所有函数永不抛异常（最坏返回未锁定），锁只影响是否继续办理，不影响已发生的资金动作。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from . import audit, config, db

# 计入"可疑行为"的审计事件：注入/越权被拦，以及被 policy 直接拦下的动作
SECURITY_EVENTS = ("attack_blocked", "turn_blocked")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS session_locks (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    locked_until TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _ensure_table(conn) -> None:
    try:
        conn.execute(_TABLE_SQL)
        conn.commit()
    except Exception:
        pass


def _now(now_iso: str = "") -> datetime:
    if now_iso:
        try:
            return datetime.fromisoformat(now_iso)
        except Exception:
            pass
    return datetime.now().astimezone()


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _threshold() -> int:
    try:
        return int(getattr(config, "SECURITY_LOCK_THRESHOLD", 3) or 3)
    except Exception:
        return 3


def _window_seconds() -> int:
    try:
        return int(getattr(config, "SECURITY_LOCK_WINDOW_SECONDS", 600) or 600)
    except Exception:
        return 600


def _lock_seconds() -> int:
    try:
        return int(getattr(config, "SECURITY_LOCK_SECONDS", 300) or 300)
    except Exception:
        return 300


def info(conn, session_id: str, now_iso: str = "") -> Dict[str, Any]:
    """当前会话锁状态：{locked, locked_until, reason, remaining_s}。永不抛。"""
    empty = {"locked": False, "locked_until": "", "reason": "", "remaining_s": 0}
    if not session_id:
        return empty
    _ensure_table(conn)
    try:
        row = conn.execute(
            "SELECT locked_until, reason FROM session_locks WHERE session_id=?", (session_id,)
        ).fetchone()
    except Exception:
        return empty
    if not row:
        return empty
    until = _parse(row["locked_until"])
    now = _now(now_iso)
    if until is None or until <= now:
        return empty
    remaining = int((until - now).total_seconds())
    return {"locked": True, "locked_until": row["locked_until"], "reason": row["reason"],
            "remaining_s": max(1, remaining)}


def lock(conn, session_id: str, user_id: str, reason: str, seconds: int = 0, now_iso: str = "") -> Dict[str, Any]:
    """锁定会话并写审计事件 security_locked。永不抛。"""
    if not session_id:
        return {"locked": False, "locked_until": "", "reason": "", "remaining_s": 0}
    _ensure_table(conn)
    secs = int(seconds or _lock_seconds())
    now = _now(now_iso)
    until = (now + timedelta(seconds=secs)).isoformat(timespec="seconds")
    try:
        conn.execute(
            "INSERT INTO session_locks (session_id, user_id, locked_until, reason, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET user_id=excluded.user_id, locked_until=excluded.locked_until, "
            "reason=excluded.reason, created_at=excluded.created_at",
            (session_id, user_id or "", until, reason or "", now.isoformat(timespec="seconds")),
        )
        conn.commit()
    except Exception:
        return {"locked": False, "locked_until": "", "reason": "", "remaining_s": 0}
    try:
        audit.append(conn, "security_locked", session_id=session_id, tool="locks",
                     args={"reason": reason or "", "seconds": secs},
                     result={"locked_until": until}, risk_level="high", actor="system")
    except Exception:
        pass
    return {"locked": True, "locked_until": until, "reason": reason or "", "remaining_s": secs}


def unlock(conn, session_id: str) -> bool:
    _ensure_table(conn)
    try:
        conn.execute("DELETE FROM session_locks WHERE session_id=?", (session_id,))
        conn.commit()
        audit.append(conn, "security_unlocked", session_id=session_id, tool="locks", risk_level="medium", actor="system")
        return True
    except Exception:
        return False


def recent_event_count(conn, session_id: str, window_seconds: int = 0, now_iso: str = "") -> int:
    """窗口内该会话的安全事件数（读审计表，与哈希链同源）。"""
    if not session_id:
        return 0
    secs = int(window_seconds or _window_seconds())
    now = _now(now_iso)
    since = now - timedelta(seconds=secs)
    try:
        rows = conn.execute(
            "SELECT ts, event_type FROM audit WHERE session_id=? AND event_type IN ('attack_blocked','turn_blocked') "
            "ORDER BY seq DESC LIMIT 200", (session_id,)
        ).fetchall()
    except Exception:
        return 0
    count = 0
    for row in rows:
        ts = _parse(row["ts"])
        if ts is not None and ts >= since:
            count += 1
    return count


def record_security_event(conn, session_id: str, user_id: str, kind: str, reason: str,
                          now_iso: str = "") -> Dict[str, Any]:
    """记一次安全事件（已由调用方写入审计），达到阈值则锁定会话。

    返回 {locked, locked_until, reason, remaining_s, count, threshold, window_s, just_locked}。
    """
    threshold = _threshold()
    count = recent_event_count(conn, session_id, now_iso=now_iso)
    state = info(conn, session_id, now_iso=now_iso)
    just_locked = False
    if count >= threshold and not state["locked"]:
        reason_text = "连续 %d 次可疑或被拦操作（最近一次：%s），按异常熔断策略临时锁定 %d 秒" % (
            count, reason or kind, _lock_seconds())
        state = lock(conn, session_id, user_id, reason_text, now_iso=now_iso)
        just_locked = bool(state.get("locked"))
    out = dict(state)
    out.update({"count": count, "threshold": threshold, "window_s": _window_seconds(), "just_locked": just_locked})
    return out


def status(conn, session_id: str = "") -> Dict[str, Any]:
    """给接口/文档用的一行状态。"""
    state = info(conn, session_id) if session_id else {"locked": False, "locked_until": "", "reason": "", "remaining_s": 0}
    state.update({"threshold": _threshold(), "window_s": _window_seconds(), "lock_seconds": _lock_seconds()})
    return state
