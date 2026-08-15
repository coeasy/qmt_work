"""零依赖 TOTP（RFC 6238）实现，用于交易二次确认。

不引入 pyotp 依赖：仅用标准库（hmac / hashlib / struct）生成/校验基于时间的动态码。
默认 30s 时间步长、6 位十进制码，支持 ±1 步窗口容错。
"""
import hashlib
import hmac
import struct
import time


def totp_at(secret: str, t: int | None = None, digits: int = 6, period: int = 30) -> str:
    """返回 secret 在时刻 t（秒）对应的 TOTP 码。"""
    if t is None:
        t = int(time.time())
    counter = t // period
    msg = struct.pack(">Q", counter)
    key = secret.encode("utf-8")
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret: str, code: str, digits: int = 6, period: int = 30,
                window: int = 1) -> bool:
    """校验 code 是否在允许的时间窗口内（默认 ±period 一步）。"""
    if not secret or not code:
        return False
    code = code.strip()
    t = int(time.time())
    for w in range(-window, window + 1):
        if totp_at(secret, t + w * period, digits, period) == code:
            return True
    return False


def current_totp(secret: str, digits: int = 6, period: int = 30) -> str:
    """调试/演示用：返回当前码（生产环境应由用户独立设备生成）。"""
    return totp_at(secret, digits=digits, period=period)
