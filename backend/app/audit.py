"""SHA-256 哈希链审计。每条记录链接上一条，任何篡改都会让链校验失败。"""
import hashlib
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from . import db

GENESIS = "0" * 64
_BUSY_RETRIES = 6


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_of(payload: Dict[str, Any]) -> str:
    return sha256_hex(canonical(payload))


def last_hash(conn) -> str:
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS


def append(conn, event_type: str, session_id: str = "", tool: str = "", args: Any = None, result: Any = None,
           risk_level: str = "low", approval_id: str = "", actor: str = "agent") -> Dict[str, Any]:
    """追加一条审计记录。

    seq 与 prev_hash 都是「读-算-写」：并发写必须整体串行，否则会出现两条记录拿到同一个
    seq（UNIQUE audit.seq 冲突 → 500）或两条记录接同一个 prev_hash（哈希链分叉）。因此这里
    用 BEGIN IMMEDIATE 包住「取末尾 hash + 取下一个 seq + 插入」，被别的写事务占用时退避重试。
    已经在事务里（例如资金动作调用方）则沿用调用方事务，不再提交，交由调用方统一提交。
    """
    own = not conn.in_transaction
    last_exc: Optional[Exception] = None
    for attempt in range(_BUSY_RETRIES):
        try:
            if own:
                conn.execute("BEGIN IMMEDIATE")
            prev = last_hash(conn)
            ts = db.now_iso()
            args_hash = sha256_hex(canonical(args)) if args is not None else ""
            result_hash = sha256_hex(canonical(result)) if result is not None else ""
            seq = (conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM audit").fetchone()["n"])
            payload = {
                "seq": seq, "ts": ts, "session_id": session_id, "actor": actor, "event_type": event_type,
                "tool": tool, "args_hash": args_hash, "result_hash": result_hash, "risk_level": risk_level,
                "approval_id": approval_id, "prev_hash": prev,
            }
            h = _hash_of(payload)
            payload["hash"] = h
            conn.execute(
                "INSERT INTO audit (seq, ts, session_id, actor, event_type, tool, args_hash, result_hash, risk_level, approval_id, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (seq, ts, session_id, actor, event_type, tool, args_hash, result_hash, risk_level, approval_id, prev, h),
            )
            if own:
                conn.commit()
            return payload
        except sqlite3.Error as exc:  # busy / locked / seq 冲突：回滚后重试
            last_exc = exc
            if own and conn.in_transaction:
                conn.rollback()
            if attempt == _BUSY_RETRIES - 1:
                raise
            time.sleep(0.005 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("audit append failed")


def entries(conn, session_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    if session_id:
        rows = conn.execute("SELECT * FROM audit WHERE session_id=? ORDER BY seq DESC LIMIT ?", (session_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def verify(conn) -> Dict[str, Any]:
    rows = conn.execute("SELECT * FROM audit ORDER BY seq ASC").fetchall()
    prev = GENESIS
    checked = 0
    for r in rows:
        payload = {
            "seq": r["seq"], "ts": r["ts"], "session_id": r["session_id"], "actor": r["actor"],
            "event_type": r["event_type"], "tool": r["tool"], "args_hash": r["args_hash"],
            "result_hash": r["result_hash"], "risk_level": r["risk_level"], "approval_id": r["approval_id"],
            "prev_hash": r["prev_hash"],
        }
        expect = _hash_of(payload)
        if r["prev_hash"] != prev or r["hash"] != expect:
            return {"valid": False, "checked": checked, "broken_at": r["seq"], "reason": "哈希链断裂或记录被篡改"}
        prev = r["hash"]
        checked += 1
    return {"valid": True, "checked": checked, "broken_at": None, "reason": ""}
