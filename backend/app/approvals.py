"""审批单：创建、查询、校验（确认 / 动态码）、过期处理。

安全修复点（P1）：
1. IDOR：check() 新增 session_id，审批单只能由创建它的会话提交（跨会话 → APPROVAL_SESSION_MISMATCH）。
2. 二次决策：mark() 改为 CAS（WHERE status='pending'），返回是否命中；调用方必须检查，
   同一 approval_id 第二次 approve 会得到 APPROVAL_ALREADY_DECIDED，而不是再次执行工具。
3. 参数绑定：create() 把 params 的规范化摘要写入 approvals.params_digest；check() 复核
   数据库里的 params_json 未被篡改，engine.resume() 再比对「审批时的 steps」与
   「checkpoint 里即将执行的 steps」摘要一致，防止换单执行未审批的动作。
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from . import config, db, second_factor, totp


def _now() -> datetime:
    return datetime.now().astimezone()


def params_digest(params) -> str:
    """审批参数规范化摘要（SHA-256 hex）：键排序 + UTF-8，跨进程稳定。"""
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create(conn, session_id: str, thread_id: str, action: str, params: Dict[str, Any], risk_level: str,
           reasons, challenge: str = "confirm") -> Dict[str, Any]:
    approval_id = "ap_" + secrets.token_hex(6)
    nonce = "n_" + secrets.token_hex(8)
    created = _now()
    expires = created + timedelta(seconds=config.APPROVAL_TTL_SECONDS)
    digest = params_digest(params)
    row = {
        "approval_id": approval_id, "session_id": session_id, "thread_id": thread_id, "action": action,
        "params": params, "risk_level": risk_level, "reasons": reasons, "challenge": challenge,
        "nonce": nonce, "status": "pending", "created_at": created.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"), "decided_at": None,
        "params_digest": digest,
    }
    conn.execute(
        "INSERT INTO approvals (approval_id, session_id, thread_id, action, params_json, risk_level,"
        " reasons_json, challenge, nonce, status, created_at, expires_at, decided_at, params_digest)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (approval_id, session_id, thread_id, action, json.dumps(params, ensure_ascii=False), risk_level,
         json.dumps(reasons, ensure_ascii=False), challenge, nonce, "pending", row["created_at"],
         row["expires_at"], None, digest),
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


def mark(conn, approval_id: str, status: str, commit: bool = True) -> bool:
    """CAS 状态迁移：仅当审批单仍为 pending 时生效，返回是否命中。

    commit=False 用于让调用方把「决策落地」和「写审计」放进同一个事务：
    两者要么都生效，要么都不生效，避免出现「已 approved 但审计没写、执行没跑」的卡死单。
    """
    cur = conn.execute(
        "UPDATE approvals SET status=?, decided_at=? WHERE approval_id=? AND status='pending'",
        (status, db.now_iso(), approval_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def check(conn, approval_id: str, decision: str, code: str, user_id: str,
          method: str = "", session_id: str = "") -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """返回 (ok, error_code, approval)。

    method 为可选的校验方式（v0.4.1 多因子）：approve 且审批要求动态码时，
    m = method or "totp"；totp 走原动态码校验（向后兼容），sms 校验短信验证码
    （code 即短信码），face 校验人脸令牌（code 即 fc_ 令牌）。
    session_id 非空时必须与审批单归属会话一致（防 IDOR）。
    """
    ap = get(conn, approval_id)
    if not ap:
        return False, "APPROVAL_NOT_FOUND", None
    if session_id and ap["session_id"] != session_id:
        return False, "APPROVAL_SESSION_MISMATCH", ap
    if ap["status"] != "pending":
        return False, "APPROVAL_ALREADY_DECIDED", ap
    stored = str(ap.get("params_digest") or "")
    if stored and params_digest(ap["params"]) != stored:
        return False, "APPROVAL_PARAMS_MISMATCH", ap
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
        elif not totp.verify_code(user_id, code or "", window=config.TOTP_WINDOW, conn=conn):
            return False, "INVALID_OTP", ap
    return True, "", ap
