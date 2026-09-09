"""兼容 shim：密钥加密已下沉至 core.crypto（P1-2 / M1，2026-09-08）。

新代码请直接 `from core.crypto import ...`；本文件仅为存量引用保留。
注意：`_NONCE` 为加密版本戳（旧密文兼容），实现仍在 core/crypto.py。
"""
import base64
import os

from core import crypto as _core

# Keep the historical private surface local to this compatibility shim. Older
# integrations and tests patch these names directly; that must not mutate the
# trusted core module's process-wide key.
_KEY_FILE = _core._KEY_FILE
_NONCE = _core._NONCE
_master = _core._master


def _load_master_key() -> bytes:
    old_key_file = _core._KEY_FILE
    _core._KEY_FILE = _KEY_FILE
    try:
        return _core._load_master_key()
    finally:
        _core._KEY_FILE = old_key_file


def encrypt_plain(text: str) -> str:
    nonce = _core.os.urandom(12)
    ciphertext = _core.AESGCM(_master).encrypt(nonce, text.encode(), _NONCE)
    return base64.b64encode(nonce + ciphertext).decode()


def decrypt_plain(token: str) -> str:
    raw = base64.b64decode(token)
    nonce, ciphertext = raw[:12], raw[12:]
    return _core.AESGCM(_master).decrypt(nonce, ciphertext, _NONCE).decode()


def mask_secret(token: str, keep: int = 4) -> str:
    return _core.mask_secret(token, keep)


def encrypt_fields(obj: dict, keys=None) -> dict:
    keys = _core._SENSITIVE_KEYS if keys is None else set(keys)
    out = dict(obj or {})
    for key, value in out.items():
        if key in keys and isinstance(value, str) and value and not value.startswith(_core._ENC_PREFIX):
            out[key] = _core._ENC_PREFIX + encrypt_plain(value)
    return out


def decrypt_fields(obj: dict, keys=None) -> dict:
    keys = _core._SENSITIVE_KEYS if keys is None else set(keys)
    out = dict(obj or {})
    for key, value in out.items():
        if isinstance(value, str) and value.startswith(_core._ENC_PREFIX):
            try:
                out[key] = decrypt_plain(value[len(_core._ENC_PREFIX):])
            except Exception:  # noqa: BLE001
                out[key] = ""
    return out
