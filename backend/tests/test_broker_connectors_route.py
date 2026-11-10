"""P0-3 回归：连接器清单接口必须如实暴露退役状态。

背景：``/brokers/profiles`` 返回的是**券商类型**，不含传输方式；前端只能硬编码
「文件桥 / Redis / ZMQ / 直连」，于是后端早已判定失效的方式在界面上仍是「推荐」。
本接口把 ``connectors.registry`` 变成唯一真源。
"""
import asyncio

from app.routes.broker import list_connectors
from connectors.registry import RETIRED_PROFILES


def _items():
    res = asyncio.run(list_connectors())
    assert res["code"] == 0
    return res["data"]["items"]


def test_retired_profiles_are_flagged():
    items = {i["key"]: i for i in _items()}

    for key, notice in RETIRED_PROFILES.items():
        assert key in items, f"退役连接器 {key} 未出现在清单里"
        assert items[key]["retired"] is True
        assert items[key]["available"] is False
        assert items[key]["retirement_notice"] == notice
        assert items[key]["recommended"] is False


def test_available_profiles_are_marked():
    items = {i["key"]: i for i in _items()}

    assert items["qmt.big.closed"]["retired"] is False
    assert items["qmt.big.closed"]["available"] is True
    assert items["qmt.big.closed"]["recommended"] is True


def test_sorting_puts_available_first():
    items = _items()

    first_retired = next((n for n, i in enumerate(items) if i["retired"]), len(items))
    last_available = max((n for n, i in enumerate(items) if i["available"]), default=-1)
    assert last_available < first_retired, "退役项必须排在可用项之后"


def test_every_item_has_label_and_transport():
    for item in _items():
        assert item["label"], f"{item['key']} 缺 label"
        assert item["transport"], f"{item['key']} 缺 transport"
