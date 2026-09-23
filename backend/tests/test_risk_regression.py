"""风控回归契约（Phase 1 ②：拆分自 smoke2.test_risk_circuit_breaker）。

进程内 app fixture。熔断契约：
- trip → code=0 且 circuit_broken=True；
- 熔断期间 /signal/submit 被拦截（**code=400** 且提示含「熔断」）；
- reset → circuit_broken=False，恢复放行链路（无券商时回落到 503「未连接」而非熔断拦截）。

★★ 2026-09-23（R25）语义修正：熔断拦截原本断言 503，与
`gateway/signal_router.py` 的设计相矛盾 —— 该模块刻意用 `broker_unavailable`
区分「券商不可用（503）」与「业务拒绝（400）」，但路由层从未读这个标志，
一律返 503。现路由已按该标志分流：
  - 熔断 / 风控拒绝 / 参数错 ⇒ **400**（请求被业务规则拒绝，不是服务挂了）
  - 券商没连上 / SDK 缺失 ⇒ **503**（服务能力缺失，给「去连接券商」引导）
两者都保留，判据是 `broker_unavailable`，不是「谁先返回」。
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

    # 熔断期间信号被拦（400 = 业务拒绝 + 「熔断」提示；**不是 503**，服务没挂）
    r = app_client.post("/api/v1/signal/submit", json=_SIGNAL)
    body = r.json()
    assert body.get("code") == 400, r.text[:200]
    assert "熔断" in str(body), r.text[:200]

    # 解除熔断
    r = app_client.post("/api/v1/config/risk/circuit", json={"action": "reset"})
    body = r.json()
    assert body.get("code") == 0, r.text[:200]
    assert (body.get("data") or {}).get("circuit_broken") is False, r.text[:200]


def test_signal_after_reset_reports_no_broker_not_circuit(app_client):
    """解除熔断后：拦截原因不得再是「熔断」（即熔断状态真的被 reset 清掉了）。

    ★★ 2026-09-23（R25）修正：原断言 `code == 503` 是**假绿** ——
      旧路由对**所有**失败一律返 503，所以这条用例在「熔断残留」时同样会通过，
      它从未验证过自己的名字。实测这条路径真实返回的是
      「可用资金不足：本单需 1000，可用 808」（风控闸门的业务拒绝）。

      现在改为断言两件事，都能被证伪：
        ① 原因里不得出现「熔断」（reset 若失效 ⇒ 立刻变红）；
        ② 必须是**业务拒绝 400**（带真实原因），而不是笼统的「服务不可用 503」。
      503 是留给「券商没连上 / SDK 缺失」的，两者混用会让用户去查错方向。
    """
    app_client.post("/api/v1/config/risk/circuit", json={"action": "reset"})
    r = app_client.post("/api/v1/signal/submit", json=_SIGNAL)
    body = r.json()
    msg = str(body.get("message") or "")
    assert "熔断" not in msg, f"熔断残留：{msg}"
    assert body.get("code") == 400, \
        f"解除熔断后应回到业务拒绝（400 + 真实原因），实得 {r.text[:200]}"
    assert msg, "业务拒绝必须带原因，否则用户无从排查"
