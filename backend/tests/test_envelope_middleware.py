"""响应信封兜底中间件单元测试（R3 重构 + 2026-11-07 状态码分层）。

覆盖两类语义，**必须一起看**，否则容易把「只修 503」误解成「所有错误都变 4xx」：

* **body 兜底**：裸 list/dict 自动包成 ``{code:0, data}``（R3 起）。
* **状态码分层**：``code=503`` 提升为真实 HTTP 503；``400/404/409`` 仍是
  HTTP 200 + code（调用方语义，重试无意义）。见 ``middleware/envelope.py``
  的模块 docstring「为什么只提升 503」。
"""

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.middleware.envelope import EnvelopeMiddleware, _promote_status


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

    @app.get("/api/v1/envelope_err_400")
    def envelope_err_400():
        return {"code": 400, "message": "参数错误"}

    @app.get("/api/v1/envelope_err_404")
    def envelope_err_404():
        return {"code": 404, "message": "不存在"}

    @app.get("/api/v1/explicit_status")
    def explicit_status():
        """路由已显式给状态码：中间件不许覆盖（502 比 code 503 更具体）。"""
        return JSONResponse(status_code=502, content={"code": 503, "message": "上游拒绝"})

    @app.get("/mcp")
    def mcp():
        """非 /api/v1 前缀：JSON-RPC 也有 code 概念，不能套用 HTTP 语义。"""
        return {"code": 503, "message": "json-rpc-ish"}

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


# ---------------------------------------------------------------- body 兜底


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


# ------------------------------------------------------------ 状态码分层（503）


def test_envelope_503_becomes_real_http_503(app):
    """``code=503`` ⇒ 真实 HTTP 503，**body 一字不改**。

    ★ body 必须保持原样：前端 ``http.ts`` 的 ``!res.ok`` 分支靠 ``env.message``
      显示真因。若中间件在提升状态码时重写 body（例如只留 ``detail``），
      用户就会从「券商未连接，请到券商连接页」退化成「请求失败（HTTP 503）」。
    """
    c = TestClient(app)
    r = c.get("/api/v1/envelope_err")
    assert r.status_code == 503
    assert r.json() == {"code": 503, "message": "broker not connected"}


def test_promoted_503_keeps_body_length_consistent(app):
    """提升状态码后 content-length 必须与实际 body 一致（否则客户端截断/挂起）。"""
    c = TestClient(app)
    r = c.get("/api/v1/envelope_err")
    raw = r.content
    assert int(r.headers["content-length"]) == len(raw)


@pytest.mark.parametrize("path,code", [("/api/v1/envelope_err_400", 400),
                                       ("/api/v1/envelope_err_404", 404)])
def test_client_semantic_codes_stay_http_200(app, path, code):
    """400/404 是**调用方语义**，保持 HTTP 200 + code —— 本轮刻意不改。

    这不是遗漏：一起改会把「修 503 失真」变成「迁移全仓契约」，而全仓大量
    代码与用例按 ``code`` 分支判断。要改时扩 ``_PROMOTE_CODES`` 即可。
    """
    c = TestClient(app)
    r = c.get(path)
    assert r.status_code == 200
    assert r.json()["code"] == code


def test_explicit_status_code_is_not_overridden(app):
    """路由显式给的状态码优先级更高，中间件不许覆盖。"""
    c = TestClient(app)
    r = c.get("/api/v1/explicit_status")
    assert r.status_code == 502
    assert r.json()["code"] == 503


def test_non_api_path_503_not_promoted(app):
    """``/mcp`` 等非 ``/api/v1/`` 路径不提升：JSON-RPC 的 code 不是 HTTP 语义。"""
    c = TestClient(app)
    r = c.get("/mcp")
    assert r.status_code == 200
    assert r.json()["code"] == 503


# ----------------------------------------------------- _promote_status 纯函数


def test_promote_status_gates():
    """三重闸门逐条钉死，避免以后有人「顺手」放宽。"""
    env503 = {"code": 503, "message": "x"}
    # 闸门 1：非 /api/v1 不提升
    assert _promote_status(200, env503, "/mcp") == 200
    # 闸门 2：已显式状态码不覆盖
    assert _promote_status(502, env503, "/api/v1/x") == 502
    # 闸门 3：非 _PROMOTE_CODES 保持 200
    assert _promote_status(200, {"code": 400, "message": "x"}, "/api/v1/x") == 200
    # 命中
    assert _promote_status(200, env503, "/api/v1/x") == 503
    # 非信封（无 int code）不动
    assert _promote_status(200, {"x": 1}, "/api/v1/x") == 200
    assert _promote_status(200, [1, 2], "/api/v1/x") == 200
    # code=0 成功不动
    assert _promote_status(200, {"code": 0, "data": 1}, "/api/v1/x") == 200
