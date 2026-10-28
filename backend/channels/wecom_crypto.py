"""企业微信回调报文加密（纯标准库，零第三方依赖，v0.3.2）。

实现官方回调协议所需的全部密码学原语：
- AES-128 分组加解密（FIPS-197，自带 S 盒与密钥扩展，不依赖 cryptography）；
- AES-128-CBC（PKCS#7 补位，企微按 32 字节补位）；
- 企微消息封装/解封装：random(16) + msg_len(4，网络序) + msg + corp_id；
- 回调签名：sha1("".join(sorted([token, timestamp, nonce, encrypt])))。

说明：本文件只做协议与密码学，不做任何业务判断；密钥只来自环境变量/调用方，
不落盘、不写日志。自检见 backend/channels/wecom.py --selftest（FIPS-197 向量）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from typing import Optional, Tuple

# FIPS-197 AES-128 S 盒与逆 S 盒
SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
)
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i

_BLOCK = 16          # AES 分组长度（字节）
_WECOM_PAD = 32      # 企微 PKCS#7 补位到 32 字节的整数倍


# ---------------- 基础运算 ----------------
def _xtime(value: int) -> int:
    """GF(2^8) 上乘 2（不可约多项式 x^8+x^4+x^3+x+1）。"""
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法；用于 MixColumns/InvMixColumns。"""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result & 0xFF


def _expand_key(key: bytes) -> list:
    """AES-128 密钥扩展：16 字节主密钥 → 176 字节轮密钥。"""
    if len(key) != 16:
        raise ValueError("AES-128 密钥必须是 16 字节")
    words = list(key)
    rcon = 1
    for i in range(16, 176, 4):
        temp = words[i - 4:i]
        if i % 16 == 0:
            # RotWord + SubWord + Rcon
            temp = [SBOX[temp[1]], SBOX[temp[2]], SBOX[temp[3]], SBOX[temp[0]]]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        for j in range(4):
            words.append(words[i - 16 + j] ^ temp[j])
    return words


def _add_round_key(state: list, words: list, rnd: int) -> None:
    base = rnd * 16
    for i in range(16):
        state[i] ^= words[base + i]


def _sub_bytes(state: list, box= SBOX) -> None:
    for i in range(16):
        state[i] = box[state[i]]


def _shift_rows(state: list) -> None:
    # state 下标按列优先：state[4*c + r]；第 r 行循环左移 r
    state[1], state[5], state[9], state[13] = state[5], state[9], state[13], state[1]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[15], state[3], state[7], state[11]


def _inv_shift_rows(state: list) -> None:
    state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]


def _mix_columns(state: list) -> None:
    for c in range(4):
        i = c * 4
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
        state[i + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
        state[i + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
        state[i + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)


def _inv_mix_columns(state: list) -> None:
    for c in range(4):
        i = c * 4
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        state[i + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        state[i + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        state[i + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)


def encrypt_block(key: bytes, block: bytes) -> bytes:
    """AES-128 单分组加密（输入输出均 16 字节）。"""
    if len(block) != 16:
        raise ValueError("AES 分组必须是 16 字节")
    words = _expand_key(key)
    state = list(block)
    _add_round_key(state, words, 0)
    for rnd in range(1, 10):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, words, rnd)
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, words, 10)
    return bytes(state)


def decrypt_block(key: bytes, block: bytes) -> bytes:
    """AES-128 单分组解密。"""
    if len(block) != 16:
        raise ValueError("AES 分组必须是 16 字节")
    words = _expand_key(key)
    state = list(block)
    _add_round_key(state, words, 10)
    for rnd in range(9, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, INV_SBOX)
        _add_round_key(state, words, rnd)
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _sub_bytes(state, INV_SBOX)
    _add_round_key(state, words, 0)
    return bytes(state)


# ---------------- PKCS#7 补位 ----------------
def pkcs7_pad(data: bytes, block_size: int = _WECOM_PAD) -> bytes:
    """PKCS#7 补位；data 长度正好整除时补满一个 block（企微要求）。"""
    if block_size <= 0 or block_size > 255:
        raise ValueError("block_size 必须在 1..255")
    pad = block_size - len(data) % block_size
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block_size: int = _WECOM_PAD) -> bytes:
    """PKCS#7 去补位；补位非法时抛 ValueError。"""
    if not data or len(data) % block_size != 0:
        raise ValueError("密文长度不是补位块大小的整数倍")
    pad = data[-1]
    if pad < 1 or pad > block_size or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS#7 补位非法")
    return data[:-pad]


# ---------------- AES-CBC ----------------
def aes_cbc_encrypt(key: bytes, iv: bytes, plain: bytes) -> bytes:
    """AES-128-CBC 加密；plain 必须已按 16 字节补位。"""
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-128-CBC 的 key/iv 必须是 16 字节")
    if len(plain) % 16:
        raise ValueError("待加密数据长度必须是 16 的整数倍")
    out = bytearray()
    prev = iv
    for offset in range(0, len(plain), 16):
        block = bytes(a ^ b for a, b in zip(plain[offset:offset + 16], prev))
        prev = encrypt_block(key, block)
        out.extend(prev)
    return bytes(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, cipher: bytes) -> bytes:
    """AES-128-CBC 解密（不去补位）。"""
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-128-CBC 的 key/iv 必须是 16 字节")
    if not cipher or len(cipher) % 16:
        raise ValueError("密文长度必须是 16 的整数倍")
    out = bytearray()
    prev = iv
    for offset in range(0, len(cipher), 16):
        block = cipher[offset:offset + 16]
        plain = decrypt_block(key, block)
        out.extend(a ^ b for a, b in zip(plain, prev))
        prev = block
    return bytes(out)


# ---------------- 企微密钥与签名 ----------------
def aes_key_from_encoding_key(encoding_aes_key: str) -> bytes:
    """43 位 EncodingAESKey → 16 字节 AES 密钥（补 '=' 后 base64 解出 32 字节，取前 16）。"""
    value = (encoding_aes_key or "").strip()
    if len(value) != 43:
        raise ValueError("EncodingAESKey 必须是 43 位")
    try:
        raw = base64.b64decode(value + "=", validate=True)
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise ValueError("EncodingAESKey 不是合法 base64") from exc
    if len(raw) != 32:
        raise ValueError("EncodingAESKey 解码后必须是 32 字节")
    return raw[:16]


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """回调签名：sha1(sorted([token, timestamp, nonce, encrypt]) 拼接)。"""
    parts = sorted([str(token or ""), str(timestamp or ""), str(nonce or ""), str(encrypt or "")])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
    """常量时间比较回调签名。"""
    expected = wecom_signature(token, timestamp, nonce, encrypt)
    return hmac.compare_digest(expected, str(signature or ""))


# ---------------- 企微报文封装/解封装 ----------------
def wecom_encrypt(msg: bytes, corp_id: str, encoding_aes_key: str,
                  random16: Optional[bytes] = None) -> str:
    """封装并加密企微消息，返回 base64 字符串。

    明文结构：random(16) + msg_len(4，网络序) + msg + corp_id，PKCS#7 补到 32 字节。
    """
    key = aes_key_from_encoding_key(encoding_aes_key)
    iv = key
    prefix = random16 if random16 is not None else os.urandom(16)
    if len(prefix) != 16:
        raise ValueError("random16 必须是 16 字节")
    raw = bytes(prefix) + struct.pack(">I", len(msg)) + bytes(msg) + str(corp_id or "").encode("utf-8")
    cipher = aes_cbc_encrypt(key, iv, pkcs7_pad(raw, _WECOM_PAD))
    return base64.b64encode(cipher).decode("ascii")


def wecom_decrypt(encrypt_b64: str, corp_id: str, encoding_aes_key: str) -> bytes:
    """解密企微消息，返回 msgbuf 明文（校验 corp_id 尾部一致）。"""
    key = aes_key_from_encoding_key(encoding_aes_key)
    iv = key
    try:
        cipher = base64.b64decode(str(encrypt_b64 or ""), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("密文不是合法 base64") from exc
    if not cipher or len(cipher) % _WECOM_PAD:
        raise ValueError("密文长度非法")
    raw = pkcs7_unpad(aes_cbc_decrypt(key, iv, cipher), _WECOM_PAD)
    if len(raw) < 20:
        raise ValueError("解密后内容过短")
    msg_len = struct.unpack(">I", raw[16:20])[0]
    if msg_len < 0 or 20 + msg_len > len(raw):
        raise ValueError("消息长度字段非法")
    msg = raw[20:20 + msg_len]
    from_corp_id = raw[20 + msg_len:]
    if corp_id and from_corp_id.decode("utf-8", "ignore") != str(corp_id):
        raise ValueError("corp_id 校验失败")
    return msg


def decode_echostr(echostr: str, corp_id: str, encoding_aes_key: str) -> str:
    """URL 验证场景：解密 echostr 并返回明文字符串。"""
    return wecom_decrypt(echostr, corp_id, encoding_aes_key).decode("utf-8", "ignore")


def encode_reply_xml(encrypt: str, signature: str, timestamp: str, nonce: str) -> str:
    """被动回复 XML 外壳（Encrypt 已是 base64）。"""
    return (
        "<xml><Encrypt><![CDATA[%s]]></Encrypt>"
        "<MsgSignature><![CDATA[%s]]></MsgSignature>"
        "<TimeStamp>%s</TimeStamp>"
        "<Nonce><![CDATA[%s]]></Nonce></xml>"
    ) % (encrypt, signature, timestamp, nonce)


def wrap_reply(msg_xml: str, corp_id: str, encoding_aes_key: str,
               token: str, timestamp: str, nonce: str) -> Tuple[str, str]:
    """把明文回复 XML 加密并包成被动回复 XML，返回 (回复 XML, Encrypt)。"""
    encrypt = wecom_encrypt(str(msg_xml).encode("utf-8"), corp_id, encoding_aes_key)
    signature = wecom_signature(token, timestamp, nonce, encrypt)
    return encode_reply_xml(encrypt, signature, timestamp, nonce), encrypt


__all__ = [
    "SBOX", "INV_SBOX",
    "encrypt_block", "decrypt_block",
    "pkcs7_pad", "pkcs7_unpad",
    "aes_cbc_encrypt", "aes_cbc_decrypt",
    "aes_key_from_encoding_key",
    "wecom_signature", "verify_signature",
    "wecom_encrypt", "wecom_decrypt", "decode_echostr",
    "encode_reply_xml", "wrap_reply",
]
