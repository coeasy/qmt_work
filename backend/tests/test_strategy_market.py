"""策略市场单元测试（pytest，不依赖券商连接）。

覆盖：内置模板目录 / 发布->列表->取单 roundtrip / .zip bundle 导出导入 / 单策略 JSON 导入导出 /
以及挂载 strategy_market 路由的端点测试。
运行：cd backend && python -m pytest tests/test_strategy_market.py -q
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from pathlib import Path

from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.state import state
from app.db import DB
from tools import strategy_market as sm

_BUILTIN = ["ma_cross", "macd", "rsi", "limitup"]


def _tmp_db():
    """返回 (db, tmpdir)。Windows 下避免 TemporaryDirectory 严格清理的文件锁问题。"""
    d = tempfile.mkdtemp()
    db = DB(Path(d) / "test.db")
    return db, d


@pytest.fixture
def db_env():
    db, d = _tmp_db()
    prev = state.db
    state.db = db
    yield db
    state.db = prev
    shutil.rmtree(d, ignore_errors=True)


# ---------------- 目录 ----------------
def test_catalog_nonempty_and_four_types():
    cat = sm.strategy_catalog()
    assert len(cat) >= 4
    types = {c["type"] for c in cat}
    for t in _BUILTIN:
        assert t in types
    for c in cat:
        assert "id" in c and "name" in c and "params_schema" in c
        assert isinstance(c["params_schema"], list)


# ---------------- 发布/列表/取单 roundtrip ----------------
def test_publish_list_get_roundtrip(db_env):
    rec = sm.publish_to_market(
        "s1", {"title": "我的均线", "author": "alice", "type": "ma_cross",
               "description": "测试", "tags": ["demo"]},
        "print('hello')")
    assert rec["id"] == "s1"
    assert rec["title"] == "我的均线"
    assert rec["downloads"] == 0

    rows = sm.list_market()
    assert len(rows) == 1
    assert rows[0]["id"] == "s1"

    # tag 过滤
    tagged = sm.list_market(tag="demo")
    assert len(tagged) == 1
    notag = sm.list_market(tag="nope")
    assert len(notag) == 0

    one = sm.get_market("s1")
    assert one is not None
    assert one["content"] == "print('hello')"
    assert one["tags"] == ["demo"]
    assert sm.get_market("missing") is None


# ---------------- .zip bundle 导出导入 ----------------
def test_export_import_bundle_roundtrip(db_env):
    sm.publish_to_market("a", {"title": "A", "type": "macd"}, "code_a")
    sm.publish_to_market("b", {"title": "B", "type": "rsi"}, "code_b")

    out = os.path.join(tempfile.gettempdir(), "bundle_test.zip")
    res = sm.export_bundle(["a", "b"], out)
    assert res["count"] == 2
    assert os.path.isfile(res["path"])

    # 清空后重新导入
    sm.state.db.execute("DELETE FROM strategy_market")
    assert sm.list_market() == []

    imp = sm.import_bundle(out)
    assert imp["imported"] == 2
    assert set(imp["ids"]) == {"a", "b"}
    assert len(sm.list_market()) == 2

    # 覆盖去重：再次导入同名 id，数量不变
    imp2 = sm.import_bundle(out)
    assert imp2["imported"] == 2
    assert len(sm.list_market()) == 2

    os.remove(out)


def test_import_bundle_invalid_missing_manifest(db_env):
    bad = os.path.join(tempfile.gettempdir(), "bad.zip")
    import zipfile
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("x.py", "code")
    try:
        sm.import_bundle(bad)
        assert False, "should raise"
    except ValueError as exc:
        assert "manifest" in str(exc)
    os.remove(bad)


# ---------------- 单策略 JSON 导出导入 ----------------
def test_export_import_json_roundtrip(db_env):
    sm.publish_to_market("j1", {"title": "J", "author": "bob", "type": "limitup",
                                "tags": ["x"]}, "code_j")
    out = os.path.join(tempfile.gettempdir(), "j1.json")
    r = sm.export_json("j1", out)
    assert r["id"] == "j1"

    sm.state.db.execute("DELETE FROM strategy_market")
    imp = sm.import_json(out)
    assert imp["imported"] == 1
    assert imp["ids"] == ["j1"]
    one = sm.get_market("j1")
    assert one["content"] == "code_j"
    assert one["tags"] == ["x"]
    os.remove(out)


# ---------------- 端点测试（仅挂载 strategy_market 路由） ----------------
@pytest.fixture
def client():
    db, d = _tmp_db()
    prev = state.db
    state.db = db

    app = FastAPI()
    from app.routes import strategy_market as sm_route
    app.include_router(sm_route.router)
    c = TestClient(app)
    yield c
    state.db = prev
    shutil.rmtree(d, ignore_errors=True)


def test_catalog_endpoint(client):
    r = client.get("/strategy-market/catalog")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) >= 4


def test_publish_install_export_import_endpoints(client):
    body = {"strategy_id": "e1", "title": "端点策略", "author": "u",
            "type": "ma_cross", "content": "x=1", "tags": ["t"]}
    r = client.post("/strategy-market/publish", json=body)
    assert r.status_code == 200
    assert r.json()["data"]["id"] == "e1"

    # list
    r = client.get("/strategy-market/market")
    assert r.status_code == 200 and len(r.json()["data"]) == 1

    # get
    r = client.get("/strategy-market/market/e1")
    assert r.status_code == 200
    # missing -> 404（约定：错误以 err(code) 返回，HTTP 仍 200）
    r = client.get("/strategy-market/market/nope")
    assert r.json()["code"] == 404

    # install with empty client_path -> 400
    r = client.post("/strategy-market/install", json={"id": "e1", "client_path": ""})
    assert r.json()["code"] == 400

    # export zip
    r = client.post("/strategy-market/export", json={"ids": ["e1"]})
    assert r.status_code == 200
    zpath = r.json()["data"]["path"]
    assert r.json()["data"]["count"] == 1
    assert os.path.isfile(zpath)

    # import zip
    r = client.post("/strategy-market/import", json={"path": zpath})
    assert r.status_code == 200
    assert r.json()["data"]["imported"] == 1

    # export-json / import-json
    r = client.post("/strategy-market/export-json", json={"id": "e1"})
    assert r.status_code == 200
    jpath = r.json()["data"]["path"]
    assert os.path.isfile(jpath)

    r = client.post("/strategy-market/import-json", json={"path": jpath})
    assert r.status_code == 200
    assert r.json()["data"]["imported"] == 1

    os.remove(zpath)
    os.remove(jpath)
