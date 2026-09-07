"""兼容 shim：密钥加密已下沉至 core.crypto（P1-2 / M1，2026-09-08）。

新代码请直接 `from core.crypto import ...`；本文件仅为存量引用保留。
注意：`_NONCE` 为加密版本戳（旧密文兼容），实现仍在 core/crypto.py。
"""
from core.crypto import (  # noqa: F401
    decrypt_fields,
    decrypt_plain,
    encrypt_fields,
    encrypt_plain,
    mask_secret,
)
