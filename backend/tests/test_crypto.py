"""app/crypto.py 密钥加密测试（T7 补缺）。

覆盖：roundtrip 加密解密 / tamper 检测 / 错误 key 失败 / 掩码 / env 主密钥优先级。
注意：模块导入即加载主密钥（可能落盘 master.key），测试用 monkeypatch 隔离，
不触碰真实密钥文件。
"""
import base64

import pytest

import app.crypto as crypto


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


def test_env_key_precedence(monkeypatch, tmp_path):
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
    key1 = crypto._load_master_key()
    assert len(key1) == 32 and key_file.exists()
    key2 = crypto._load_master_key()
    assert key1 == key2
