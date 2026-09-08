"""E4 敏感信息脱敏：日志过滤器 + 字典/文本掩码。

用途：
- 日志：`SensitiveFilter` 挂到 root logger，任何日志消息中出现的
  `api_key=...` / `Bearer xxx` / `"token": "xxx"` / `X-API-Key: xxx` 等一律掩码；
- 审计与 API 响应：`mask_dict()` 递归掩码敏感键（api_key/token/secret/password/…），
  资金账号用 `mask_account()` 保留头尾（便于人工核对但不泄露完整账号）。

掩码策略：长度 ≥ 8 保留前 3 后 2（`qmt-dev-key` → `qmt***ey`）；更短则全掩码，
既能在排障时区分不同密钥，又不足以还原原值。
"""
from __future__ import annotations

import logging
import re

# 需要整体掩码的键名（小写比较，子串匹配）
_SECRET_KEYS = (
    "api_key", "apikey", "api-key", "token", "secret", "password", "passwd",
    "pwd", "authorization", "auth_header", "private_key", "signature", "sign_key",
    "webhook_secret", "totp_secret", "access_key", "session_key", "credential",
)
# 需要保留头尾的键名（账号类）
_ACCOUNT_KEYS = ("account_id", "account", "acct", "fund_account", "qmt_account_id")
# 明确不脱敏的键（避免误伤业务字段）
_WHITELIST_KEYS = ("token_used", "api_key_id", "key_id", "has_api_key",
                   "requires_totp", "account_type", "account_count")

_MASK = "***"

_TEXT_PATTERNS = [
    # api_key=xxx / token: "xxx" / secret => xxx
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|pwd)\b"
        r"(\"?\s*[:=]\s*\"?)([^\s\"',;}&]{4,})"),
    # Bearer <token>
    re.compile(r"(?i)\b(Bearer)(\s+)([A-Za-z0-9._\-]{4,})"),
    # X-API-Key: xxx
    re.compile(r"(?i)\b(X-API-Key)(\s*:\s*)(\S{4,})"),
]


def mask_value(value: str, keep_head: int = 3, keep_tail: int = 2) -> str:
    """掩码单个字符串：长度 ≥ 8 保留前 keep_head 后 keep_tail，否则全掩码。"""
    s = "" if value is None else str(value)
    if not s:
        return s
    if len(s) < 8:
        return _MASK
    return f"{s[:keep_head]}{_MASK}{s[-keep_tail:]}"


def mask_account(value: str) -> str:
    """资金账号掩码：保留前 2 后 2（`8801234567` → `88***67`）。"""
    s = "" if value is None else str(value)
    if len(s) <= 4:
        return _MASK if s else s
    return f"{s[:2]}{_MASK}{s[-2:]}"


def _is_secret_key(key: str) -> bool:
    k = str(key).lower()
    if k in _WHITELIST_KEYS:
        return False
    return any(sk in k for sk in _SECRET_KEYS)


def _is_account_key(key: str) -> bool:
    k = str(key).lower()
    if k in _WHITELIST_KEYS:
        return False
    return k in _ACCOUNT_KEYS


def mask_dict(data, _depth: int = 0):
    """递归掩码字典/列表中的敏感字段（不修改原对象，返回副本）。"""
    if _depth > 8:
        return data
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if _is_secret_key(k):
                out[k] = mask_value(v) if isinstance(v, (str, int, float)) else _MASK
            elif _is_account_key(k) and isinstance(v, (str, int)):
                out[k] = mask_account(v)
            else:
                out[k] = mask_dict(v, _depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [mask_dict(v, _depth + 1) for v in data]
    if isinstance(data, str):
        return mask_text(data)
    return data


def mask_text(text: str) -> str:
    """对自由文本中的密钥模式做掩码（日志消息/异常文本）。"""
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat in _TEXT_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{mask_value(m.group(3))}", out)
    return out


class SensitiveFilter(logging.Filter):
    """日志脱敏过滤器：对 record.msg 与 args 应用文本掩码。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = mask_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (mask_text(v) if isinstance(v, str) else v)
                                   for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        mask_text(a) if isinstance(a, str) else a for a in record.args)
        except Exception:  # noqa: BLE001 - 日志链路绝不因脱敏失败而中断
            return True
        return True
