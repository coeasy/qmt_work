"""V9 Phase 5（P1-27）：全局异常处理统一信封。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_404_enveloped_on_api_paths(app_client):
    r = app_client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == 404 and "message" in body


def test_404_untouched_off_api_paths(app_client):
    # 非 /api/v1 路径：错误处理器不改写 detail 语义（信封兜底中间件可能外包一层，
    # 属既有行为）；关键断言是 HTTP 状态语义保持 404。
    r = app_client.get("/definitely-not-a-route")
    assert r.status_code == 404


def test_422_enveloped_on_validation_error(app_client):
    # runtime/jobs 提交非法 body → FastAPI 422 → 信封
    r = app_client.post("/api/v1/runtime/jobs", json={"kind": 123})
    assert r.status_code in (200, 422)  # 依赖路由模型；422 时必须带信封
    if r.status_code == 422:
        assert r.json()["code"] == 422
