"""密钥加密（§4.11）：AES-256-GCM。

主密钥来源优先级：env QMT_MASTER_KEY > 本地密钥文件（首次生成）。
LLM API Key 等敏感配置落库前加密，读取时内存解密。
"""
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import exe_dir

# 主密钥文件与数据库同目录（exe_dir()/data），保证打包运行(frozen)时密钥与
# app.db 共置、且不会落到 _internal/data 被打包进安装包。开发模式下 exe_dir()==BASE_DIR，
# 等价于原 backend/data/master.key，无回归。
_KEY_FILE = exe_dir() / "data" / "master.key"
_NONCE = b"qmt-agent-v1!!"  # 固定 12 字节前缀（每个密文再拼随机 nonce）


def _load_master_key() -> bytes:
    env_key = os.environ.get("QMT_MASTER_KEY", "")
    if env_key:
        return base64.b64decode(env_key) if len(env_key) > 32 else env_key.encode().ljust(32, b"0")[:32]
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        data = _KEY_FILE.read_bytes()
        if len(data) >= 32:
            return data[:32]
    key = os.urandom(32)
    _KEY_FILE.write_bytes(key)
    return key


_master = _load_master_key()


def encrypt_plain(text: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_master).encrypt(nonce, text.encode(), _NONCE)
    return base64.b64encode(nonce + ct).decode()


def decrypt_plain(token: str) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_master).decrypt(nonce, ct, _NONCE).decode()


def mask_secret(token: str, keep: int = 4) -> str:
    """脱敏展示：sk-****1234"""
    if len(token) <= keep:
        return "*" * len(token)
    return token[:3] + "*" * 8 + token[-keep:]
