"""经典策略选股的 REST 契约（routes/screen.py 的 classic 分支）。

为什么钉这些用例
----------------
经典策略在引擎里早就跑通了（``tests/test_screener_classic.py``），但**路由层不
透传 ``classic`` 就等于没接** —— 界面传了参数、后端仍走条件树，用户看到的是
「条件树的空结果」而不是报错。这类静默错配最难发现，所以这里逐条钉死入参校验。

★ 校验的语义：**未知策略/坏参数必须 400 + 原因**，绝不返回空列表冒充「无命中」。
选股结果为空有两种完全不同的含义（数据源不可用 vs 真的没票），界面必须能区分；
参数写错被静默忽略会让用户误判为后者。
"""
import json

import pytest

from app.screener.classic import STRATEGY_IDS, list_strategies


def _data(resp):
    return resp.json().get("data")


def test_strategies_endpoint_lists_all(app_client):
    r = app_client.get("/api/v1/market/screen/strategies")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["count"] == len(STRATEGY_IDS)
    ids = [x["id"] for x in data["items"]]
    assert ids == list(STRATEGY_IDS)
    for it in data["items"]:
        assert it["label"] and it["desc"]
        assert isinstance(it["params"], dict)
    # 元信息里绝不能带可调用对象（JSON 序列化不了，且会泄漏实现）
    assert "fn" not in json.dumps(data)


def test_screen_requires_conditions_or_classic(app_client):
    """两者都不传 → 400 并指明可选路径，而不是拿空条件树跑出「零命中」。"""
    r = app_client.get("/api/v1/market/screen")
    body = r.json()
    assert body["code"] == 400
    assert "conditions" in body["message"]
    assert "classic" in body["message"]


def test_unknown_classic_strategy_is_400_with_options(app_client):
    r = app_client.get("/api/v1/market/screen", params={"classic": "not_a_strategy"})
    body = r.json()
    assert body["code"] == 400
    assert "未知经典策略" in body["message"]
    # 报错要带可用清单 —— 用户不必回代码里找
    for sid in STRATEGY_IDS:
        assert sid in body["message"]


def test_bad_classic_params_is_400_not_silently_ignored(app_client):
    r = app_client.get("/api/v1/market/screen",
                       params={"classic": "turtle_trade", "classic_params": "{oops"})
    body = r.json()
    assert body["code"] == 400
    assert "classic_params" in body["message"]


def test_classic_params_must_be_object(app_client):
    r = app_client.get("/api/v1/market/screen",
                       params={"classic": "turtle_trade", "classic_params": "[1,2]"})
    body = r.json()
    assert body["code"] == 400
    assert "classic_params" in body["message"]


@pytest.mark.parametrize("body,why", [
    ({}, "缺 strategies"),
    ({"strategies": []}, "空数组"),
    ({"strategies": "not_a_strategy"}, "未知策略 id"),
    ({"strategies": ["turtle_trade", "nope"]}, "混了一个未知 id"),
])
def test_classic_batch_rejects_bad_input(app_client, body, why):
    r = app_client.post("/api/v1/market/screen/classic", json=body)
    assert r.status_code == 200
    got = r.json()
    assert got["code"] == 400, f"应 400：{why}；实际 {got}"
    assert got["message"]


def test_classic_batch_rejects_bad_params_shape(app_client):
    r = app_client.post("/api/v1/market/screen/classic",
                        json={"strategies": ["turtle_trade"], "params": [1, 2]})
    assert r.json()["code"] == 400


def test_list_strategies_matches_registry():
    """接口列举的元信息必须与注册表逐字段一致（界面按它生成参数表单）。"""
    items = list_strategies()
    assert len(items) == len(STRATEGY_IDS)
    for it in items:
        assert set(it) == {"id", "label", "desc", "params"}
        assert it["params"], f"{it['id']} 无默认参数 → 表单会空白"
