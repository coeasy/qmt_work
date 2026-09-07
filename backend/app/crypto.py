"""密钥加密（§4.11）：AES-256-GCM。

主密钥来源优先级：env QMT_MASTER_KEY > 本地密钥文件（首次生成）。
LLM API Key 等敏感配置落库前加密，读取时内存解密。
"""
import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import exe_dir

log = logging.getLogger("qmt_work.crypto")

# 主密钥文件与数据库同目录（exe_dir()/data），保证打包运行(frozen)时密钥与
# app.db 共置、且不会落到 _internal/data 被打包进安装包。开发模式下 exe_dir()==BASE_DIR，
# 等价于原 backend/data/master.key，无回归。
_KEY_FILE = exe_dir() / "data" / "master.key"
_NONCE = b"qmt-agent-v1!!"  # 固定 12 字节前缀（每个密文再拼随机 nonce）


def _harden_key_file(path) -> None:
    """密钥文件最小权限（H3）：POSIX 0600；Windows 移除继承、仅保留当前用户与 SYSTEM。"""
    try:
        if os.name == "posix":
            os.chmod(path, 0o600)
        else:
            import subprocess
            me = os.environ.get("USERNAME", "")
            if me:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{me}:F", "SYSTEM:F"],
                    capture_output=True, timeout=5)
    except Exception as exc:  # noqa: BLE001 权限加固失败不阻断启动，仅告警
        log.warning("密钥文件权限加固失败 %s: %s", path, exc)


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
    _harden_key_file(_KEY_FILE)
    return key


_master = _load_master_key()
# 已存在的旧密钥文件补一次权限加固（幂等）
_harden_key_file(_KEY_FILE)


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


# ---------------- 字段级加密（H3：落库敏感参数静态加密） ----------------
# 通知渠道 params 里的 secret/password/url(含签名 key)/token/auth 落库前加密，
# 读取时解密。密文带 enc:v1: 前缀标记；无前缀的历史明文继续兼容（读取原样返回）。
_SENSITIVE_KEYS = frozenset({"secret", "password", "token", "url", "auth"})
_ENC_PREFIX = "enc:v1:"


def encrypt_fields(obj: dict, keys=None) -> dict:
    """对 dict 中指定敏感字段加密（str 且非空且未加密过），返回浅拷贝。"""
    keys = _SENSITIVE_KEYS if keys is None else set(keys)
    out = dict(obj or {})
    for k, v in out.items():
        if k in keys and isinstance(v, str) and v and not v.startswith(_ENC_PREFIX):
            out[k] = _ENC_PREFIX + encrypt_plain(v)
    return out


def decrypt_fields(obj: dict, keys=None) -> dict:
    """encrypt_fields 的逆操作：解密带 enc:v1: 前缀的字段，失败置空并告警（不外泄密文）。"""
    keys = _SENSITIVE_KEYS if keys is None else set(keys)
    out = dict(obj or {})
    for k, v in out.items():
        if isinstance(v, str) and v.startswith(_ENC_PREFIX):
            try:
                out[k] = decrypt_plain(v[len(_ENC_PREFIX):])
            except Exception as exc:  # noqa: BLE001
                log.warning("敏感字段解密失败 key=%s: %s", k, exc)
                out[k] = ""
    return out
