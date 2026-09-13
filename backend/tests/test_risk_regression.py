"""风控回归契约（Phase 1 ②：拆分自 smoke2.test_risk_circuit_breaker）。

进程内 app fixture。熔断契约：
- trip → code=0 且 circuit_broken=True；
- 熔断期间 /signal/submit 被拦截（code=503 且提示含「熔断」）；
- reset → circuit_broken=False，恢复放行链路（无券商时回落到 503「未连接」而非熔断拦截）。
"""
_SIGNAL = {"source": "test", "code": "600519.SH", "side": "buy",
           "volume": 100, "price": 10.0, "price_type": "limit"}


def test_risk_circuit_breaker_blocks_and_recovers(app_client):
    # 干净状态
    r = app_client.post("/api/v1/config/risk/circuit", json={"action": "reset"})
    assert r.status_code == 200 and r.json().get("code") == 0, r.text[:200]

    # 触发熔断
    r = app_client.post("/api/v1/config/risk/circuit",
                        json={"action": "trip", "reason": "risk regression"})
    body = r.json()
    assert body.get("code") == 0, r.text[:200]
    assert (body.get("data") or {}).get("circuit_broken") is True, r.text[:200]

    # 熔断期间信号被拦（503 + 「熔断」提示）
    r = app_client.post("/api/v1/signal/submit", json=_SIGNAL)
    body = r.json()
    assert body.get("code") == 503, r.text[:200]
    assert "熔断" in str(body), r.text[:200]

    # 解除熔断
    r = app_client.post("/api/v1/config/risk/circuit", json={"action": "reset"})
    body = r.json()
    assert body.get("code") == 0, r.text[:200]
    assert (body.get("data") or {}).get("circuit_broken") is False, r.text[:200]


def test_signal_after_reset_reports_no_broker_not_circuit(app_client):
    """解除熔断后：拦截原因必须回到「未连接券商」503，而非残留熔断拦截。"""
    app_client.post("/api/v1/config/risk/circuit", json={"action": "reset"})
    r = app_client.post("/api/v1/signal/submit", json=_SIGNAL)
    body = r.json()
    assert body.get("code") == 503, r.text[:200]
    msg = str(body.get("message") or "")
    assert "熔断" not in msg, f"熔断残留：{msg}"
