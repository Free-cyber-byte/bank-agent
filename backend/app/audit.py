"""SHA-256 哈希链审计。每条记录链接上一条，任何篡改都会让链校验失败。"""
import hashlib
import json
from typing import Any, Dict, List, Optional

from . import db

GENESIS = "0" * 64


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
    conn.commit()
    return payload


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
