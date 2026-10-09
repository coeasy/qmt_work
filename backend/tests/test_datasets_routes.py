"""R28 数据集 REST 端点测试。

只测**契约**不测真实取数（真实同步依赖外网/券商，留给 :mod:`test_dataset_syncer`
用注入方式验证）。这里钉死的是：

- 业务失败走 envelope（HTTP 200 + ``code != 0``），前端才能统一取 ``message``；
- 未知数据集 / 超限 limit 必须有明确的中文原因；
- 清单里每个数据集都带前端渲染所需的字段；
- 本地查询端点在空库时返回 0 行而不是 500。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.db import init_db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from datasource import local_store

    init_db(tmp_path / "app.db")
    monkeypatch.setattr(local_store, "_store", None)
    from app.main import app
    c = TestClient(app)
    c.headers.update({"X-API-Key": settings.api_key})
    return c


def test_list_datasets_grouped(client):
    r = client.get("/api/v1/datasets")
    assert r.status_code == 200
    b = r.json()
    assert b["code"] == 0
    data = b["data"]
    assert data["total"] >= 18
    cats = {c["id"] for c in data["categories"]}
    for want in ("bars", "reference", "fundamental", "tick", "calendar"):
        assert want in cats, f"缺少类别 {want}"


def test_list_items_have_status_fields(client):
    """每项必须带本地状态，否则界面无法回答「库里现在有多少」。"""
    r = client.get("/api/v1/datasets")
    items = [i for c in r.json()["data"]["categories"] for i in c["items"]]
    for it in items:
        for k in ("id", "label", "category", "chain", "store", "cursor",
                  "cron", "retention_days", "local", "last_sync_at"):
            assert k in it, f"{it.get('id')} 缺字段 {k}"
        assert set(it["local"]) >= {"rows", "codes", "first_dt", "last_dt"}


def test_detail_unknown_is_400_with_reason(client):
    r = client.get("/api/v1/datasets/not-a-dataset")
    assert r.status_code == 200          # envelope 约定：HTTP 恒 200
    b = r.json()
    assert b["code"] == 400
    assert "not-a-dataset" in b["message"]


def test_detail_known(client):
    r = client.get("/api/v1/datasets/bars_5m")
    assert r.status_code == 200
    b = r.json()
    assert b["code"] == 0
    assert b["data"]["id"] == "bars_5m"
    assert b["data"]["period"] == "5m"


def test_sources_endpoint_reveals_effective_chain(client):
    """回答「现在点同步，数据会从哪来」——声明链与实际链都要给。"""
    r = client.get("/api/v1/datasets/sources")
    assert r.status_code == 200
    d = r.json()["data"]
    assert "broker_available" in d and "providers" in d
    pd = d["per_dataset"]["bars_1d"]
    assert pd["declared"][0] == "broker"      # 声明：券商优先
    assert isinstance(pd["resolved"], list)   # 现实：按注册态过滤后的实际链


def test_sources_calendar_reports_builtin_fallback(client):
    """``calendar`` 的解析链恒为空（``local`` 不是注册 provider），但它**真的能同步**。

    若前端只看 ``effective_first`` 会显示「无可用源」——那是**假告警**（反向假绿
    家族）：日历每次都成功，靠的是 ``DatasetSyncer._fetch_calendar`` 的内置回退。
    这里钉死「解析为空但有内置实现」必须被单独报出来。
    """
    r = client.get("/api/v1/datasets/sources")
    assert r.status_code == 200
    pd = r.json()["data"]["per_dataset"]["calendar"]
    assert pd["effective_first"] == "", "calendar 不该有可解析的注册 provider"
    assert pd["builtin_fallback"] == "local"
    assert pd["builtin_note"], "内置源必须带可展示的说明文案"


def test_sources_no_builtin_fallback_when_chain_resolves(client):
    """有可解析 provider 的数据集不得谎报内置回退（避免 UI 显示两个源）。"""
    r = client.get("/api/v1/datasets/sources")
    pd = r.json()["data"]["per_dataset"]["bars_1d"]
    if pd["effective_first"]:
        assert pd["builtin_fallback"] == ""


def test_sync_unknown_dataset(client):
    r = client.post("/api/v1/datasets/not-a-dataset/sync", json={"limit": 1})
    assert r.status_code == 200
    b = r.json()
    assert b["code"] == 400
    assert "not-a-dataset" in b["message"]


def test_sync_limit_cap(client):
    """超上限必须拒绝 —— 全市场同步是调度的事，HTTP 请求不能挂死。"""
    r = client.post("/api/v1/datasets/bars_1d/sync", json={"limit": 99999})
    assert r.status_code == 200
    b = r.json()
    assert b["code"] == 400
    assert "定时调度" in b["message"]


def test_sync_with_explicit_codes_bypasses_cap(client):
    """显式指定 codes 时不按「全市场」论处——用户明确知道自己在跑多少只。"""
    r = client.post("/api/v1/datasets/bars_1d/sync",
                    json={"limit": 99999, "codes": ["600000.SH"]})
    # 不是 400（通过了上限校验）；真实取数可能失败，但那必须是 503 而非参数错误
    assert r.json()["code"] in (0, 503)


def test_data_endpoint_empty_store(client):
    """空库返回 0 行而不是 500 —— 表未迁移也要优雅。"""
    r = client.get("/api/v1/datasets/stock_list/data?limit=5")
    assert r.status_code == 200
    b = r.json()
    assert b["code"] == 0
    assert b["data"]["count"] == 0
    assert b["data"]["rows"] == []


def test_data_endpoint_calendar_uses_real_columns(client):
    """``exchange_calendar`` 的日期列是 ``trade_date`` 不是 ``date``。

    第一版按 ``(date, source)`` 写会撞 ``no such column``，而因为它被 try 包着，
    表现为「同步成功但日历永远空」——这里守住列名不回归。
    """
    r = client.get("/api/v1/datasets/calendar/data?limit=3")
    assert r.status_code == 200
    assert r.json()["code"] == 0, r.json().get("message")


def test_data_endpoint_requires_code_for_kline(client):
    """K 线类不给 code 时返回空（而不是把全市场塞进响应）。"""
    r = client.get("/api/v1/datasets/bars_1d/data?limit=5")
    assert r.json()["code"] == 0
    assert r.json()["data"]["count"] == 0


def test_data_limit_clamped(client):
    r = client.get("/api/v1/datasets/stock_list/data?limit=99999")
    assert r.json()["code"] == 0        # 被夹到上限，不报错
