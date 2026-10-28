"""审批单：创建、查询、校验（确认 / 动态码）、过期处理。"""
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from . import config, db, second_factor, totp


def _now() -> datetime:
    return datetime.now().astimezone()


def create(conn, session_id: str, thread_id: str, action: str, params: Dict[str, Any], risk_level: str,
           reasons, challenge: str = "confirm") -> Dict[str, Any]:
    approval_id = "ap_" + secrets.token_hex(6)
    nonce = "n_" + secrets.token_hex(8)
    created = _now()
    expires = created + timedelta(seconds=config.APPROVAL_TTL_SECONDS)
    row = {
        "approval_id": approval_id, "session_id": session_id, "thread_id": thread_id, "action": action,
        "params": params, "risk_level": risk_level, "reasons": reasons, "challenge": challenge,
        "nonce": nonce, "status": "pending", "created_at": created.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"), "decided_at": None,
    }
    conn.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (approval_id, session_id, thread_id, action, json.dumps(params, ensure_ascii=False), risk_level,
         json.dumps(reasons, ensure_ascii=False), challenge, nonce, "pending", row["created_at"], row["expires_at"], None),
    )
    conn.commit()
    return row


def get(conn, approval_id: str) -> Optional[Dict[str, Any]]:
    r = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params_json"])
    d["reasons"] = json.loads(d["reasons_json"])
    return d


def pending_for_session(conn, session_id: str) -> Optional[Dict[str, Any]]:
    r = conn.execute("SELECT * FROM approvals WHERE session_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params_json"])
    d["reasons"] = json.loads(d["reasons_json"])
    return d


def mark(conn, approval_id: str, status: str) -> None:
    conn.execute("UPDATE approvals SET status=?, decided_at=? WHERE approval_id=?", (status, db.now_iso(), approval_id))
    conn.commit()


def check(conn, approval_id: str, decision: str, code: str, user_id: str,
          method: str = "") -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """返回 (ok, error_code, approval)。

    method 为可选的校验方式（v0.4.1 多因子）：approve 且审批要求动态码时，
    m = method or "totp"；totp 走原动态码校验（向后兼容），sms 校验短信验证码
    （code 即短信码），face 校验人脸令牌（code 即 fc_ 令牌）。
    """
    ap = get(conn, approval_id)
    if not ap:
        return False, "APPROVAL_NOT_FOUND", None
    if ap["status"] != "pending":
        return False, "APPROVAL_ALREADY_DECIDED", ap
    expires = datetime.fromisoformat(ap["expires_at"])
    if _now() > expires:
        mark(conn, approval_id, "expired")
        return False, "APPROVAL_EXPIRED", ap
    if decision not in ("approve", "reject"):
        return False, "INVALID_DECISION", ap
    if decision == "approve" and ap["challenge"] == "totp":
        factor = (method or "totp").strip().lower()
        if factor not in ("totp", "sms", "face"):
            return False, "INVALID_METHOD", ap
        if factor == "sms":
            if not second_factor.sms_verify(user_id, code or ""):
                return False, "INVALID_SMS", ap
        elif factor == "face":
            if not second_factor.face_verify(user_id, code or ""):
                return False, "INVALID_FACE", ap
        elif not totp.verify_code(user_id, code or "", window=config.TOTP_WINDOW):
            return False, "INVALID_OTP", ap
    return True, "", ap
