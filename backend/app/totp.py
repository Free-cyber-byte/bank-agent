"""标准 TOTP（RFC 6238）实现，标准库即可，无需 pyotp。演示模式可查询当前码。

安全修复点（P1）：
1. 旧实现用 sha256("zerostep:" + user_id) 确定性派生密钥——任何人拿到 user_id 就能算出
   该用户的 TOTP 码，等于没有第二因子。现在每个用户首次使用时用 secrets.token_bytes(20)
   生成随机密钥并持久化到 db.totp_secrets，任何情况下都不再可推导。
2. 校验改用 hmac.compare_digest 常量时间比较，避免时序侧信道。
3. 每个用户连续失败 5 次锁定 60 秒（db.totp_secrets.fails / locked_until），成功即清零，
   防止 6 位码在线爆破。

注意：last_counter 只做审计记录（记录最近一次成功的时间步），不做「一次性」拒绝——
历史接口（smoke18 / run_tasks / replay）会在同一 30 秒窗口内对多张审批单重复使用
当前码，若把 last_counter 当重放闸会破坏既有合法调用链。
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timedelta
from typing import Optional

from . import db

# 连续失败阈值与锁定时长（秒）
MAX_FAILS = 5
LOCK_SECONDS = 60

_CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS totp_secrets ("
    "user_id TEXT PRIMARY KEY, secret TEXT NOT NULL, fails INTEGER NOT NULL DEFAULT 0, "
    "locked_until TEXT NOT NULL DEFAULT '', last_counter INTEGER NOT NULL DEFAULT 0, "
    "created_at TEXT NOT NULL DEFAULT '')"
)


def _table(conn) -> None:
    """兼容未经 init_schema 的临时连接：确保凭证表存在（幂等，旧库自动补表）。"""
    conn.execute(_CREATE_SQL)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _get_row(conn, user_id: str):
    _table(conn)
    return conn.execute(
        "SELECT user_id, secret, fails, locked_until, last_counter FROM totp_secrets WHERE user_id=?",
        (user_id,),
    ).fetchone()


def _secret_b32(conn, user_id: str) -> bytes:
    """取用户随机密钥；首次访问自动生成并落库（不可由 user_id 推导）。"""
    row = _get_row(conn, user_id)
    if row is not None and row["secret"]:
        return str(row["secret"]).encode("ascii")
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    conn.execute(
        "INSERT OR IGNORE INTO totp_secrets (user_id, secret, fails, locked_until, last_counter, created_at) "
        "VALUES (?,?,0,'',0,?)",
        (user_id, secret, _now_iso()),
    )
    conn.commit()
    row = _get_row(conn, user_id)
    if row is None or not row["secret"]:
        raise RuntimeError("totp secret init failed for %r" % (user_id,))
    return str(row["secret"]).encode("ascii")


def _hotp(secret_b32: bytes, counter: int) -> str:
    key = base64.b32decode(secret_b32)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1000000).zfill(6)


def current_code(user_id: str, at: Optional[float] = None, step: int = 30, conn=None) -> str:
    """返回该用户当前真实 6 位码（/api/demo/totp 依赖此接口）。"""
    own = conn is None
    if own:
        conn = db.connect()
    try:
        t = time.time() if at is None else at
        return _hotp(_secret_b32(conn, user_id), int(t // step))
    finally:
        if own:
            conn.close()


def verify_code(user_id: str, code: str, at: Optional[float] = None, window: int = 1,
                step: int = 30, conn=None) -> bool:
    """校验 TOTP 码。保留旧签名（user_id, code, at, window, step），新增可选 conn。"""
    if not code or not str(code).strip().isdigit():
        return False
    code = str(code).strip()
    own = conn is None
    if own:
        conn = db.connect()
    try:
        t = time.time() if at is None else at
        row = _get_row(conn, user_id)
        secret = _secret_b32(conn, user_id)
        row = _get_row(conn, user_id) or row
        locked_until = _parse_iso(row["locked_until"]) if row is not None else None
        if locked_until is not None and datetime.now().astimezone() < locked_until:
            return False
        counter = int(t // step)
        ok = any(
            hmac.compare_digest(_hotp(secret, counter + d), code)
            for d in range(-window, window + 1)
        )
        if ok:
            conn.execute(
                "UPDATE totp_secrets SET fails=0, locked_until='', last_counter=? WHERE user_id=?",
                (counter, user_id),
            )
        else:
            fails = int(row["fails"] or 0) + 1 if row is not None else 1
            if fails >= MAX_FAILS:
                until = (datetime.now().astimezone() + timedelta(seconds=LOCK_SECONDS)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE totp_secrets SET fails=0, locked_until=? WHERE user_id=?",
                    (until, user_id),
                )
            else:
                conn.execute("UPDATE totp_secrets SET fails=? WHERE user_id=?", (fails, user_id))
        conn.commit()
        return ok
    finally:
        if own:
            conn.close()


def seconds_left(at: Optional[float] = None, step: int = 30) -> int:
    t = time.time() if at is None else at
    return step - int(t % step)
