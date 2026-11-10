"""P0-2 回归：券商桥 ``auth_token`` 必须以密文落库，且可解密还原。

| 场景 | 期望 |
|------|------|
| 新保存连接 | ``auth_token`` 列为空、``auth_token_enc`` 以 ``enc:v1:`` 开头 |
| 重新加载 | 内存里拿到明文 token（连接才能用） |
| 旧库升级 | 启动时自动把明文搬进密文列，并清空明文（幂等） |
| 对外列表 | 只暴露 ``has_auth_token``，不返回 token 本身 |
"""
import pytest

from core import db as db_mod
from core.db import DB
from xtquant_client.manager import BrokerManager, ConnectionConfig, _decode_auth_token


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    # 测试环境无写权限放 master.key：注入固定主密钥（32 字节）。
    import core.crypto as crypto
    monkeypatch.setattr(crypto, "_master", b"0" * 32, raising=False)
    db = DB(tmp_path / "app.db")
    monkeypatch.setattr(db_mod, "_db", db)
    yield db
    db.close()


def _cfg(**kw) -> ConnectionConfig:
    base = dict(conn_id="c1", name="测试", broker_id="qmt.mini",
                client_path="", client_mode="auto", account_id="", session_id=0)
    base.update(kw)
    return ConnectionConfig(**base)


def test_persist_encrypts_and_loads(fresh_db):
    mgr = BrokerManager()
    mgr._persist(_cfg(auth_token="s3cret-token"))

    row = fresh_db.query_one(
        "SELECT auth_token, auth_token_enc FROM broker_connections WHERE conn_id='c1'")

    assert row["auth_token"] == "", "明文列必须为空"
    assert row["auth_token_enc"].startswith("enc:v1:")
    assert "s3cret" not in row["auth_token_enc"], "密文不得包含原文"

    mgr2 = BrokerManager()
    mgr2.load_persisted()
    conns = {c.cfg.conn_id: c for c in mgr2.all_connections()}
    assert conns["c1"].cfg.auth_token == "s3cret-token"


def test_list_exposes_only_flag(fresh_db):
    mgr = BrokerManager()
    cfg = _cfg(auth_token="s3cret-token")
    mgr._build(cfg, connect=False)     # 只装配，不连接
    mgr._persist(cfg)

    items = mgr.status_list()
    item = next(i for i in items if i["conn_id"] == "c1")

    assert item["has_auth_token"] is True
    assert "auth_token" not in item
    assert "s3cret" not in str(item)


def test_backfill_migrates_legacy_plaintext(tmp_path, monkeypatch):
    """模拟旧库：只有明文 → 新版本启动后应自动加密并清空明文。"""
    path = tmp_path / "old.db"
    old = DB(path)
    # 先保证列存在（旧库没有 auth_token_enc）
    old.execute(
        "INSERT INTO broker_connections (conn_id, name, broker_id, auth_token, created_at) "
        "VALUES ('legacy', '旧连接', 'qmt.mini', 'legacy-token', '2026-01-01')")
    old.close()

    new_db = DB(path)
    monkeypatch.setattr(db_mod, "_db", new_db)
    try:
        row = new_db.query_one(
            "SELECT auth_token, auth_token_enc FROM broker_connections WHERE conn_id='legacy'")
        assert row["auth_token"] == "", "回填后应清明文"
        assert row["auth_token_enc"].startswith("enc:v1:")
        assert _decode_auth_token(dict(row)) == "legacy-token"
    finally:
        new_db.close()


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "idem.db"
    db1 = DB(path)
    db1.execute(
        "INSERT INTO broker_connections (conn_id, name, broker_id, auth_token, created_at) "
        "VALUES ('legacy', '旧连接', 'qmt.mini', 'legacy-token', '2026-01-01')")
    db1.close()

    db2 = DB(path)
    first = db2.query_one(
        "SELECT auth_token_enc FROM broker_connections WHERE conn_id='legacy'")["auth_token_enc"]
    db2.close()

    db3 = DB(path)   # 第二次启动：不应再加密一次
    second = db3.query_one(
        "SELECT auth_token_enc FROM broker_connections WHERE conn_id='legacy'")["auth_token_enc"]
    db3.close()

    assert first == second, "重复启动不应改变密文"


def test_decode_falls_back_to_plaintext(fresh_db):
    """密文为空但明文存在（加密失败降级）时仍能读出 token。"""
    fresh_db.execute(
        "INSERT INTO broker_connections (conn_id, name, broker_id, auth_token, created_at) "
        "VALUES ('p1', '明文', 'qmt.mini', 'plain-token', '2026-01-01')")

    row = fresh_db.query_one(
        "SELECT * FROM broker_connections WHERE conn_id='p1'")
    assert _decode_auth_token(dict(row)) == "plain-token"
