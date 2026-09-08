"""兼容 shim：脱敏工具已下沉至 core/masking.py（P1-2 延续 / M1，2026-09-08）。

脱敏是通用基础设施（core/db.py 审计落库与 logging_setup 都用到），与网关无关，
故归入 core 层。新代码请直接 `from core.masking import ...`。
"""
from core.masking import *  # noqa: F401,F403
from core.masking import (  # noqa: F401
    SensitiveFilter,
    _is_account_key,
    _is_secret_key,
    mask_account,
    mask_dict,
    mask_text,
    mask_value,
)
