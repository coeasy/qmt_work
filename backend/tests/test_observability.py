"""观测性扩展（P2）单元测试：Metrics 扩展 + /observability 路由。

只挂载 observability router，不启动整个后端应用。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 将 backend 根目录加入 sys.path，保证 gateway/app 可被导入
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from gateway.metrics import Metrics, get_metrics  # noqa: E402
import app.state as st  # noqa: E402


@pytest.fixture
def metrics():
    m = get_metrics()
    # 测试前清空累计（避免与其它测试共享 _metrics 单例影响断言）
    with m._lock:
        m._backtests.clear()
        m._paper_orders.clear()
        m._api_latency.clear()
        m._api_latency_count = 0
        m._api_latency_sum = 0.0
        m._runtime_mode.clear()
        m._errors.clear()
        m._recent_traces.clear()
    return m


# ---------------- Metrics 扩展 ----------------

def test_record_methods_do_not_raise(metrics):
    metrics.record_backtest("ok")
    metrics.record_paper_order("buy")
    metrics.record_ws_message()
    metrics.record_ws_clients(3)
    metrics.record_request_duration_ms(30)
    metrics.record_request_duration_ms(600)
    metrics.record_runtime_mode("c1", "bridge")
    metrics.record_error("trade")
    metrics.record_trace("r1", "/x", 200, 12.5, "market")


def test_render_contains_new_and_old_metrics(metrics):
    metrics.record_backtest("ok")
    metrics.record_paper_order("sell")
    metrics.record_request_duration_ms(80)
    metrics.record_runtime_mode("c1", "in_process")
    metrics.record_error("ws")
    out = metrics.render()
    for name in ("qmt_backtests_total", "qmt_paper_orders_total",
                 "qmt_api_latency_ms_bucket", "qmt_runtime_mode",
                 "qmt_errors_total"):
        assert name in out, f"render 缺少新增指标 {name}"
    # 既有指标必须保留
    assert "qmt_uptime_seconds" in out
    assert "qmt_orders_total" in out
    assert "qmt_quotes_total" in out
    assert "qmt_api_requests_total" in out
    assert "qmt_ws_clients" in out
    assert "qmt_broker_connected" in out


def test_recent_traces_ring_buffer(metrics):
    for i in range(250):
        metrics.record_trace(f"r{i}", "/p", 200, float(i))
    traces = metrics.recent_traces()
    assert len(traces) <= 200
    assert traces[-1]["req_id"] == "r249"


# ---------------- 路由测试（仅挂载 observability router） ----------------

@pytest.fixture
def client(metrics):
    st.state.metrics = metrics
    from app.routes.observability import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_metrics_summary_route(client):
    metrics = st.state.metrics
    metrics.record_backtest("ok")
    metrics.record_paper_order("buy")
    metrics.record_ws_clients(4)
    metrics.record_error("auth")
    resp = client.get("/observability/metrics-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["backtests"] >= 1
    assert data["paper_orders"] >= 1
    assert data["ws_clients"] == 4
    assert data["errors"] >= 1
    assert "uptime" in data and "orders" in data and "quotes" in data


def test_traces_route(client):
    metrics = st.state.metrics
    metrics.record_trace("r1", "/api/x", 200, 15.0, "market")
    metrics.record_trace("r2", "/api/y", 500, 99.0, "trade")
    resp = client.get("/observability/traces?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[-1]["req_id"] == "r2"

    # limit 截断
    resp2 = client.get("/observability/traces?limit=1")
    assert len(resp2.json()["data"]) == 1
