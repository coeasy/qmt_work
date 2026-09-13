"""账户网格 / 批量操作 / 重连 契约测试（Phase 1 ③）。

进程内 app fixture。**零 mock 铁律的可测形式（环境无关）**：
- 只读账户端点：无券商 → HTTP 200 + code=503 + 引导到「券商连接」；
  有券商（如本机 autoconnect）→ code=0 + 真实数据。**绝不出现假数据**；
- 写端点（trade/order）：券商不可用 → 503 + 引导；券商可用但被风控/柜台拒绝 →
  400 + 真实原因（如「风控/执行拒绝：下单失败：柜台返回 -1（…）」）。两者都不得
  伪造成功或伪造行情——400 分支已校验「原因非空且来自券商侧、不得携带 ok=True」；
- 批量下单/撤单（conn_id 不存在）：HTTP 200 + code=0 信封，但 **ok 必须为 0**
  （逐单如实失败，绝不因批量入口而假报成功）——任何环境下都成立；
- 批量重连未知连接 → 逐条 failed，绝不假报 connected。
"""
_GRID_ORDER = {"code": "600519.SH", "direction": "buy", "volume": 100,
               "price": 10.0, "price_type": "limit"}


def _assert_real_or_guide(body: dict, what: str):
    """零 mock 不变量的**可判定形式**（环境无关）：

    - 503：券商**不可用**（未连接 / SDK 缺失 / 桥接不可用）→ 必须携带「券商」引导；
    - 0  ：请求已受理 → data 必须非空（真实数据或真实执行结果）；
    - 400：请求已抵达券商但被**真实拒绝**（风控拦截 / 柜台拒单 / 非交易时段）
           → 必须携带非空且来自券商侧的原因，且不得伪报 ok=True。

    禁止任何「假数据 / 假成功」形态：既不伪造行情，也绝不把拒单包装成已受理。
    """
    code = body.get("code")
    if code == 503:
        assert "券商" in str(body.get("message") or ""), f"{what} 缺引导：{str(body)[:200]}"
        return
    if code == 0:
        assert body.get("data") is not None, f"{what} code=0 但 data 为空"
        return
    if code == 400:
        msg = str(body.get("message") or "")
        assert msg.strip(), f"{what} 400 但无原因：{str(body)[:200]}"
        assert ("风控/执行拒绝" in msg or "券商" in msg or "柜台" in msg), \
            f"{what} 400 原因不可信（疑似伪造）：{msg[:200]}"
        data = body.get("data")
        assert not (isinstance(data, dict) and data.get("ok") is True), \
            f"{what} 400 却携带 ok=True（假成功）"
        return
    raise AssertionError(f"{what} 非法业务码 {code}：{str(body)[:200]}")


def test_account_grid_never_fakes_data(app_client):
    r = app_client.get("/api/v1/account/grid")
    assert r.status_code == 200
    _assert_real_or_guide(r.json(), "account/grid")


def test_trade_order_never_fakes_data(app_client):
    r = app_client.post("/api/v1/trade/order", json=_GRID_ORDER)
    assert r.status_code == 200
    _assert_real_or_guide(r.json(), "trade/order")


def test_account_status_never_fakes_data(app_client):
    r = app_client.get("/api/v1/account/status")
    assert r.status_code == 200
    _assert_real_or_guide(r.json(), "account/status")


def test_batch_order_unknown_conn_zero_ok(app_client):
    r = app_client.post("/api/v1/account/batch/order",
                        json={"orders": [{"conn_id": "nope", **_GRID_ORDER}]})
    body = r.json()
    assert r.status_code == 200 and body.get("code") == 0, r.text[:200]
    summary = body.get("data") or {}
    assert summary.get("ok") == 0, f"连接不存在时批量下单不得假报成功：{r.text[:300]}"


def test_batch_cancel_unknown_conn_all_failed(app_client):
    r = app_client.post("/api/v1/account/batch/cancel",
                        json={"items": [{"conn_id": "nope", "order_id": "x1"}]})
    body = r.json()
    assert r.status_code == 200 and body.get("code") == 0, r.text[:200]
    data = body.get("data") or {}
    assert data.get("ok") == 0, r.text[:300]
    results = data.get("results") or []
    assert results and all(rec.get("status") == "failed" for rec in results), r.text[:300]


def test_batch_cancel_empty_items_is_400(app_client):
    r = app_client.post("/api/v1/account/batch/cancel", json={})
    assert r.json().get("code") == 400, r.text[:200]


def test_batch_order_empty_is_400(app_client):
    r = app_client.post("/api/v1/account/batch/order", json={})
    assert r.json().get("code") == 400, r.text[:200]


def test_batch_reconnect_unknown_conn_never_connected(app_client):
    r = app_client.post("/api/v1/account/batch/reconnect",
                        json={"conn_ids": ["no-such-conn"]})
    body = r.json()
    assert r.status_code == 200 and body.get("code") == 0, r.text[:200]
    data = body.get("data") or {}
    results = data.get("results") or ([data] if data else [])
    assert results, r.text[:300]
    assert all(rec.get("status") != "connected" for rec in results), \
        f"未知连接不得假报已重连：{r.text[:300]}"
