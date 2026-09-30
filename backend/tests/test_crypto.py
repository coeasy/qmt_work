"""core/crypto.py 密钥加密测试（T7 补缺；原 app.crypto shim 已删除，直测真身）。

覆盖：roundtrip 加密解密 / tamper 检测 / 错误 key 失败 / 掩码 / env 主密钥优先级 /
主密钥文件路径跟随主库目录 / 旧位置升级迁移。
注意：模块导入即加载主密钥（可能落盘 master.key），测试用 monkeypatch 隔离，
不触碰真实密钥文件。
"""
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.crypto as crypto


@pytest.fixture(autouse=True)
def _fake_master(monkeypatch):
    """固定测试主密钥，隔离真实 master.key 文件。"""
    key = b"t" * 32
    monkeypatch.setattr(crypto, "_master", key)
    return key


def test_roundtrip():
    ct = crypto.encrypt_plain("sk-1234567890abcdef")
    assert ct != "sk-1234567890abcdef"          # 密文不透明
    assert crypto.decrypt_plain(ct) == "sk-1234567890abcdef"


def test_roundtrip_unicode():
    ct = crypto.encrypt_plain("密钥🔐测试/中文+特殊字符")
    assert crypto.decrypt_plain(ct) == "密钥🔐测试/中文+特殊字符"


def test_nonce_randomized():
    """每次加密 random nonce → 同一明文两次密文不同。"""
    a = crypto.encrypt_plain("same")
    b = crypto.encrypt_plain("same")
    assert a != b


def test_tamper_detected():
    """篡改密文任意字节 → decrypt 抛异常（GCM 认证失败）。"""
    ct = crypto.encrypt_plain("secret")
    raw = base64.b64decode(ct)
    raw = raw[:-1] + bytes([raw[-1] ^ 0xFF])
    tampered = base64.b64encode(raw).decode()
    with pytest.raises(Exception):
        crypto.decrypt_plain(tampered)


def test_wrong_key_fails(monkeypatch):
    ct = crypto.encrypt_plain("secret")
    monkeypatch.setattr(crypto, "_master", b"x" * 32)
    with pytest.raises(Exception):
        crypto.decrypt_plain(ct)


def test_garbage_token_fails():
    with pytest.raises(Exception):
        crypto.decrypt_plain("not-base64-!!")


def test_mask_secret():
    assert crypto.mask_secret("sk-1234567890abcdef") == "sk-" + "*" * 8 + "cdef"
    assert crypto.mask_secret("short") == "sho" + "*" * 8 + "hort"
    assert crypto.mask_secret("") == ""


def test_env_key_precedence(monkeypatch):
    """QMT_MASTER_KEY 存在时优先于本地密钥文件。"""
    env_key = base64.b64encode(b"e" * 32).decode()
    monkeypatch.setattr(crypto.os, "environ", {"QMT_MASTER_KEY": env_key})
    key = crypto._load_master_key()
    assert key == b"e" * 32


def test_key_file_generation(tmp_path, monkeypatch):
    """无 env / 无文件 → 生成并落盘；再读回一致。"""
    monkeypatch.setattr(crypto.os, "environ", {})
    key_file = tmp_path / "data" / "master.key"
    monkeypatch.setattr(crypto, "_KEY_FILE", key_file)
    # 同时隔离旧位置，否则会读到开发机上真实的 backend/data/master.key
    monkeypatch.setattr(crypto, "_LEGACY_KEY_FILE", tmp_path / "_none" / "master.key")
    key1 = crypto._load_master_key()
    assert len(key1) == 32 and key_file.exists()
    key2 = crypto._load_master_key()
    assert key1 == key2


def test_key_file_follows_db_dir(tmp_path, monkeypatch):
    """主密钥必须**跟随主库目录**（与 .qmt_work.lock / wal.jsonl / bars_cold.db 同口径）。

    回归护栏（2026-09-30）：曾固定在 exe_dir()/data —— 打包态指向**只读**的
    resources/，构建后自检在那里生成的 master.key 会被 electron-builder 打进安装包
    （所有安装共享一把密钥），而干净的只读安装反而写不进去直接崩在导入期。
    """
    monkeypatch.setattr(crypto, "settings", SimpleNamespace(db_path=tmp_path / "ud" / "app.db"))
    assert crypto._key_file() == tmp_path / "ud" / "master.key"


def test_key_file_memory_db_falls_back(monkeypatch):
    """:memory: / 相对路径没有可靠父目录 → 退回 exe 同目录（保持旧行为）。"""
    monkeypatch.setattr(crypto, "settings", SimpleNamespace(db_path=Path(":memory:")))
    assert crypto._key_file() == crypto.exe_dir() / "data" / "master.key"


def test_legacy_key_migrated(tmp_path, monkeypatch):
    """升级路径：旧位置（<exe>/data/master.key）的密钥要复制到新位置并继续生效。

    否则升级后老库里已加密的密文将永久无法解密。
    """
    monkeypatch.setattr(crypto.os, "environ", {})
    legacy = tmp_path / "legacy" / "master.key"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"L" * 32)
    new = tmp_path / "new" / "master.key"
    monkeypatch.setattr(crypto, "_LEGACY_KEY_FILE", legacy)
    monkeypatch.setattr(crypto, "_KEY_FILE", new)
    assert crypto._load_master_key() == b"L" * 32
    assert new.read_bytes() == b"L" * 32          # 已复制到新位置
    assert legacy.exists()                        # 旧文件保留（兼容降级）


def test_short_key_file_regenerated(tmp_path, monkeypatch):
    """密钥文件过短（写入中断）→ 不采信，重新生成 32 字节。"""
    monkeypatch.setattr(crypto.os, "environ", {})
    key_file = tmp_path / "data" / "master.key"
    key_file.parent.mkdir(parents=True)
    key_file.write_bytes(b"short")
    monkeypatch.setattr(crypto, "_KEY_FILE", key_file)
    monkeypatch.setattr(crypto, "_LEGACY_KEY_FILE", tmp_path / "_none" / "master.key")
    key = crypto._load_master_key()
    assert len(key) == 32
    assert key_file.read_bytes() == key


def test_field_encryption_roundtrip():
    """字段级加密：敏感字段加 enc:v1: 前缀，解密还原；历史明文原样透传。"""
    obj = {"secret": "sk-abc", "note": "plain", "url": "https://x"}
    enc = crypto.encrypt_fields(obj)
    assert enc["secret"].startswith(crypto._ENC_PREFIX)
    assert enc["note"] == "plain"                      # 非敏感字段不动
    dec = crypto.decrypt_fields(enc)
    assert dec["secret"] == "sk-abc" and dec["url"] == "https://x"
    # 历史明文（无前缀）解密时原样返回
    assert crypto.decrypt_fields({"secret": "raw-secret"})["secret"] == "raw-secret"
    # 二次加密不叠加
    twice = crypto.encrypt_fields(enc)
    assert twice["secret"] == enc["secret"]
