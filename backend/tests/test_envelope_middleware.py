"""响应信封兜底中间件单元测试（R3 重构）。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.envelope import EnvelopeMiddleware


@pytest.fixture
def app():
    app = FastAPI()

    @app.get("/api/v1/raw_list")
    def raw_list():
        return [{"code": "600519", "name": "贵州茅台"}]

    @app.get("/api/v1/raw_dict")
    def raw_dict():
        return {"x": 1, "y": 2}

    @app.get("/api/v1/envelope_ok")
    def envelope_ok():
        return {"code": 0, "data": [1, 2, 3]}

    @app.get("/api/v1/envelope_err")
    def envelope_err():
        return {"code": 503, "message": "broker not connected"}

    @app.get("/static/anything")
    def static_path():
        return [1, 2, 3]  # 也返 list 但路径不在 /api/v1

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}  # 走 skip

    @app.get("/api/v1/string_data")
    def string_data():
        return "raw string"

    app.add_middleware(EnvelopeMiddleware)
    return app


def test_raw_list_gets_envelope(app):
    c = TestClient(app)
    r = c.get("/api/v1/raw_list")
    assert r.status_code == 200
    body = r.json()
    assert body == {"code": 0, "data": [{"code": "600519", "name": "贵州茅台"}]}


def test_raw_dict_gets_envelope(app):
    c = TestClient(app)
    r = c.get("/api/v1/raw_dict")
    assert r.status_code == 200
    body = r.json()
    assert body == {"code": 0, "data": {"x": 1, "y": 2}}


def test_envelope_ok_passthrough(app):
    c = TestClient(app)
    r = c.get("/api/v1/envelope_ok")
    assert r.status_code == 200
    assert r.json() == {"code": 0, "data": [1, 2, 3]}  # 原样


def test_envelope_err_passthrough(app):
    c = TestClient(app)
    r = c.get("/api/v1/envelope_err")
    assert r.status_code == 200
    assert r.json() == {"code": 503, "message": "broker not connected"}


def test_static_path_skipped(app):
    c = TestClient(app)
    r = c.get("/static/anything")
    # 静态路径不在 /api/v1 范围内，envelope 跳过；但 FastAPI TestClient 会因 [1,2,3] 转 json
    # 我们的中间件检查路径不是 /api/v1 开头就放行，所以原始 [1,2,3]
    assert r.status_code == 200
    assert r.json() == [1, 2, 3]


def test_health_path_skipped(app):
    c = TestClient(app)
    r = c.get("/api/v1/health")
    # /api/v1/health 在 _SKIP_PREFIXES 里
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_string_data_passthrough(app):
    """非 list/dict 也不原样兜底（应保持原始字符串）。"""
    c = TestClient(app)
    r = c.get("/api/v1/string_data")
    # "raw string" 走 fastapi 自动 JSON 序列化 -> 字符串
    # 我们的中间件：data 是 str，既不是 list 也不是 dict，不在兜底条件内 → 原样返回
    assert r.status_code == 200
    assert r.json() == "raw string"
