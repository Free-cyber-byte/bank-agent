"""标准 TOTP（RFC 6238）实现，标准库即可，无需 pyotp。演示模式可查询当前码。"""
import base64
import hashlib
import hmac
import struct
import time
from typing import Optional

from . import config


def _secret(user_id: str) -> bytes:
    digest = hashlib.sha256(("zerostep:" + user_id).encode("utf-8")).digest()[:20]
    return base64.b32encode(digest)


def _hotp(secret_b32: bytes, counter: int) -> str:
    key = base64.b32decode(secret_b32)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1000000).zfill(6)


def current_code(user_id: str, at: Optional[float] = None, step: int = 30) -> str:
    t = time.time() if at is None else at
    return _hotp(_secret(user_id), int(t // step))


def verify_code(user_id: str, code: str, at: Optional[float] = None, window: int = 1, step: int = 30) -> bool:
    if not code or not code.strip().isdigit():
        return False
    code = code.strip()
    t = time.time() if at is None else at
    counter = int(t // step)
    return any(_hotp(_secret(user_id), counter + d) == code for d in range(-window, window + 1))


def seconds_left(at: Optional[float] = None, step: int = 30) -> int:
    t = time.time() if at is None else at
    return step - int(t % step)
