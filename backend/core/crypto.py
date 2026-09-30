"""密钥加密（§4.11）：AES-256-GCM。

主密钥来源优先级：env QMT_MASTER_KEY > 本地密钥文件（首次生成）。
LLM API Key 等敏感配置落库前加密，读取时内存解密。
"""
import base64
import logging
import os
import shutil
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import exe_dir, settings

log = logging.getLogger("qmt_work.crypto")


def _key_file() -> Path:
    """主密钥文件路径：**跟随主库目录** ``<db_path 的父目录>/master.key``。

    与 ``run.py`` 的 ``.qmt_work.lock`` / ``.qmt_work.port``、``wal.jsonl``、
    ``bars_cold.db`` 保持同一口径（它们都是 ``settings.db_path.parent``）——
    密钥此前是**唯一**的例外，所以踩了下面这个坑。

    ★ 为什么不能用 ``exe_dir()/data``（2026-09-30 修的真缺陷）：
      打包态桌面壳通过 ``QMT_DB_PATH`` 把主库指到 userData（用户可写），而 exe 同目录的
      ``resources/`` 是**只读**的。密钥落在那里有两个后果：
        ① 构建流水线的「构建后自检」会在原地跑一次后端 ⇒ 生成 ``data/master.key``；
           若之后（或下一轮）再跑一次 electron-builder，这个文件就被**打进安装包** ⇒
           所有安装共享同一把密钥，而它正是构建者本机加密数据所用的密钥。
           2026-08-14 曾手工清理过一次，但因为没有在流水线里设防而复发。
        ② 真正干净的安装（Program Files，resources 只读）首次启动**写不进去** ⇒
           ``OSError`` 直接崩在后端导入期。此前之所以没暴露，恰恰是因为 ① 顺带把密钥
           「带」进了包、掩盖了写入失败 —— 绿灯是另一个 bug 遮出来的。

    开发态 ``exe_dir() == BASE_DIR`` 且 ``db_path == backend/data/app.db`` ⇒
    路径仍为 ``backend/data/master.key``，无回归。
    """
    db = Path(settings.db_path)
    # ``QMT_DB_PATH=:memory:``（测试/冒烟）与相对路径没有可靠的父目录，退回旧位置。
    if db.name == ":memory:" or not db.is_absolute():
        return exe_dir() / "data" / "master.key"
    return db.parent / "master.key"


# 旧版本位置（``<exe 同目录>/data/master.key``）。**只用于升级迁移**：
# 老库里已加密的密文必须还能解开，故读到旧文件时复制到新位置而不是丢弃。
_LEGACY_KEY_FILE = exe_dir() / "data" / "master.key"
_KEY_FILE = _key_file()
_NONCE = b"qmt-agent-v1!!"  # 固定 12 字节前缀（每个密文再拼随机 nonce）


def _harden_key_file(path) -> None:
    """密钥文件最小权限（H3）：POSIX 0600；Windows 移除继承、仅保留当前用户与 SYSTEM。"""
    try:
        if os.name == "posix":
            os.chmod(path, 0o600)
        else:
            import subprocess
            me = os.environ.get("USERNAME", "")
            icacls = shutil.which("icacls")
            if me and icacls:
                subprocess.run(  # noqa: S603
                    [icacls, str(path), "/inheritance:r",
                     "/grant:r", f"{me}:F", "SYSTEM:F"],  # noqa: S607
                    capture_output=True, timeout=5)
    except Exception as exc:  # noqa: BLE001 权限加固失败不阻断启动，仅告警
        log.warning("密钥文件权限加固失败 %s: %s", path, exc)


def _read_key_file(path: Path) -> bytes | None:
    """读密钥文件；不存在 / 太短 / 读不动都返回 None（由调用方决定下一步）。"""
    try:
        if not path.exists():
            return None
        data = path.read_bytes()
    except OSError as exc:  # 权限/句柄异常不阻断启动流程
        log.warning("读取主密钥文件失败 %s: %s", path, exc)
        return None
    return data[:32] if len(data) >= 32 else None


def _load_master_key() -> bytes:
    env_key = os.environ.get("QMT_MASTER_KEY", "")
    if env_key:
        return base64.b64decode(env_key) if len(env_key) > 32 else env_key.encode().ljust(32, b"0")[:32]
    key = _read_key_file(_KEY_FILE)
    if key is not None:
        return key
    # 升级迁移：老版本的密钥在 exe 同目录（打包态是只读的 resources/，而且会被打进
    # 安装包）。**复制**而非移动 —— 老位置可能只读，且要保证降级回旧版本仍能解密。
    if _LEGACY_KEY_FILE != _KEY_FILE:
        legacy = _read_key_file(_LEGACY_KEY_FILE)
        if legacy is not None:
            try:
                _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                _KEY_FILE.write_bytes(legacy)
                _harden_key_file(_KEY_FILE)
                log.info("主密钥已迁移到新位置：%s（源 %s）", _KEY_FILE, _LEGACY_KEY_FILE)
            except OSError as exc:
                # 新位置不可写时继续沿用旧文件，避免因迁移失败而整个后端起不来。
                log.warning("主密钥迁移失败（继续使用旧文件 %s）：%s", _LEGACY_KEY_FILE, exc)
            return legacy
    key = os.urandom(32)
    try:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_bytes(key)
    except OSError as exc:
        log.error("无法写入主密钥文件 %s（目录不可写）：%s", _KEY_FILE, exc)
        raise
    _harden_key_file(_KEY_FILE)
    return key


_master = _load_master_key()
# 已存在的主密钥文件补一次权限加固（幂等）。用 QMT_MASTER_KEY 注入密钥时该文件
# 可能不存在，此时跳过 —— 否则每次启动都会白跑一次 icacls 并留下告警噪声。
if _KEY_FILE.exists():
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
