"""第二因子模拟实现（短信验证码 / 人脸确认）：纯标准库、内存态、永不抛异常。

演示环境不接真实短信网关与人脸 SDK：验证码与令牌都保存在进程内存字典里，
服务重启即失效；演示模式下前端可直接领取验证码/令牌（见 server.py 的
/api/demo/sms、/api/demo/face）。有效期与失败次数阈值全部在函数内从 config 读取，
因此 importlib.reload(config) 后可立即生效。
"""
import hmac
import secrets
import threading
import time
from typing import Any, Dict

from . import config

_LOCK = threading.RLock()
_SMS: Dict[str, Dict[str, Any]] = {}   # user_id -> {code, expires_at, attempts}
_FACE: Dict[str, Dict[str, Any]] = {}  # user_id -> {token, expires_at}


def _cfg_int(name: str, default: int) -> int:
    """读取正整数配置：异常或非正数时回退默认值（配置损坏也不能让因子不可用）。"""
    try:
        value = int(getattr(config, name, default))
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


def sms_send(user_id: str) -> Dict[str, Any]:
    """生成并缓存一条 6 位短信验证码（演示：直接返回明文，便于前端展示与自动化测试）。"""
    ttl = _cfg_int("SMS_TTL_SECONDS", 120)
    try:
        code = str(secrets.randbelow(1000000)).zfill(6)
        with _LOCK:
            _SMS[str(user_id)] = {"code": code, "expires_at": time.time() + ttl, "attempts": 0}
        return {"ok": True, "code": code, "seconds_left": ttl, "ttl": ttl}
    except Exception:  # noqa: BLE001 - 演示因子永不抛异常，失败也返回可读结构
        return {"ok": False, "code": "", "seconds_left": 0, "ttl": ttl}


def sms_verify(user_id: str, code: str) -> bool:
    """校验短信验证码：单次使用（成功即作废）、过期作废、连续失败达到上限即作废。"""
    try:
        key = str(user_id)
        max_attempts = _cfg_int("SMS_MAX_ATTEMPTS", 5)
        guess = str(code or "").strip()
        with _LOCK:
            record = _SMS.get(key)
            if not record:
                return False
            if time.time() > float(record["expires_at"]):
                _SMS.pop(key, None)
                return False
            if not guess or not hmac.compare_digest(str(record["code"]), guess):
                record["attempts"] = int(record.get("attempts") or 0) + 1
                if int(record["attempts"]) >= max_attempts:
                    _SMS.pop(key, None)  # 失败次数用尽，验证码作废
                return False
            _SMS.pop(key, None)  # 单次使用：成功即作废
            return True
    except Exception:  # noqa: BLE001 - 永不抛异常，校验失败一律 False
        return False


def face_scan(user_id: str) -> Dict[str, Any]:
    """模拟一次人脸扫描，签发一次性令牌（演示：不接真实 SDK，直接返回令牌）。"""
    ttl = _cfg_int("FACE_TTL_SECONDS", 60)
    try:
        token = "fc_" + secrets.token_hex(16)
        with _LOCK:
            _FACE[str(user_id)] = {"token": token, "expires_at": time.time() + ttl}
        return {"ok": True, "token": token, "seconds_left": ttl, "ttl": ttl}
    except Exception:  # noqa: BLE001
        return {"ok": False, "token": "", "seconds_left": 0, "ttl": ttl}


def face_verify(user_id: str, token: str) -> bool:
    """校验人脸令牌：单次使用、过期作废；错误令牌不消耗（允许重新扫描）。"""
    try:
        key = str(user_id)
        guess = str(token or "").strip()
        with _LOCK:
            record = _FACE.get(key)
            if not record:
                return False
            if time.time() > float(record["expires_at"]):
                _FACE.pop(key, None)
                return False
            if not guess or not hmac.compare_digest(str(record["token"]), guess):
                return False
            _FACE.pop(key, None)  # 单次使用
            return True
    except Exception:  # noqa: BLE001 - 永不抛异常
        return False
