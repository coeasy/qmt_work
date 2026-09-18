"""委托 / 成交字段契约归一（app/services/order_contract.py）回归。

★ 为什么单开这个文件、且刻意用**真实适配器**的字段结构写断言
------------------------------------------------------------
这个 bug 之所以能长期存活，是因为**测试用的是假网关**：后端单测里假网关返回
``side`` / ``filled``（``tests/test_e2e_flows.py``），与真实适配器
（``xtquant_client/xtp/trading.py`` 返回 ``direction`` / ``dealt``）正好一致于
**前端的旧预期**，于是全绿。真机上却是买入显示成卖出、已成交恒 0。

故本文件的输入一律照抄真实适配器的输出键（direction / dealt / order_id / seq），
**不许**用 side / filled 当输入 —— 否则又回到「测试替身掩盖真实差异」的老路。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.order_contract import (  # noqa: E402
    normalize_deal,
    normalize_deals,
    normalize_order,
    normalize_orders,
    status_label,
)
from xtquant_client.order_status import PARTIAL, CANCELLED, FILLED  # noqa: E402


def _real_order(**over):
    """照抄 xtquant_client/xtp/trading.py::get_orders 的真实输出键。"""
    row = {
        "order_id": "1001",
        "code": "600519.SH",
        "direction": "buy",
        "price": 1680.0,
        "volume": 100,
        "dealt": 30,
        "status": "part_deal",
    }
    row.update(over)
    return row


def _real_deal(**over):
    """照抄 xtquant_client/xtp/trading.py::get_deals 的真实输出键。"""
    row = {
        "order_id": "1001",
        "code": "600519.SH",
        "direction": "sell",
        "price": 1681.0,
        "volume": 100,
        "time": "2026-09-18 10:30:00",
        "seq": 7,
    }
    row.update(over)
    return row


# --------------------------------------------------------------- 字段名归一

def test_order_gets_contract_aliases():
    """真实适配器只给 direction/dealt；契约名 side/filled 必须被补上。"""
    row = normalize_order(_real_order())
    assert row["side"] == "buy"          # 方向不再恒显示「卖出」
    assert row["filled"] == 30           # 已成交不再恒为 0
    assert row["direction"] == "buy"     # 原生名保留（既有调用方/MCP 依赖）
    assert row["dealt"] == 30


def test_order_does_not_overwrite_existing_contract_name():
    """契约名已存在时绝不覆盖 —— 归一只是补缺，不是改写语义。"""
    row = normalize_order(_real_order(side="sell", filled=99))
    assert row["side"] == "sell"
    assert row["filled"] == 99


def test_order_status_normalized_to_platform_vocabulary():
    """status 一律收敛到平台标准词表，原始值留 status_raw。"""
    row = normalize_order(_real_order(status="part_deal"))
    assert row["status"] == PARTIAL
    assert row["status_raw"] == "part_deal"

    # 后端内部曾混写 part_filled / canceled，一并收敛
    assert normalize_order(_real_order(status="part_filled"))["status"] == PARTIAL
    assert normalize_order(_real_order(status="canceled"))["status"] == CANCELLED
    # xtp 整数码（53=部成）
    assert normalize_order(_real_order(status=53))["status"] == PARTIAL


def test_order_status_already_standard_keeps_no_raw():
    row = normalize_order(_real_order(status="filled"))
    assert row["status"] == FILLED
    assert "status_raw" not in row       # 没变就不必记，避免每行冗余键


def test_deal_gets_stable_deal_id():
    """成交无独立成交号：deal_id 由 order_id + seq 兜底，界面要用它做列表 key。"""
    row = normalize_deal(_real_deal())
    assert row["deal_id"] == "1001#7"
    assert row["side"] == "sell"
    assert row["order_id"] == "1001"


def test_deal_id_falls_back_without_seq():
    """旧 SDK 无 seq 时退化成 order_id，绝不返回空字符串（否则列表 key 全 undefined）。"""
    row = normalize_deal(_real_deal(seq=None))
    assert row["deal_id"] == "1001"


def test_deal_id_preserved_when_backend_provides_it():
    row = normalize_deal(_real_deal(deal_id="D-9"))
    assert row["deal_id"] == "D-9"


# ------------------------------------------------------------------- 批量/健壮

def test_batch_normalize_returns_same_list():
    orders = normalize_orders([_real_order(), _real_order(order_id="1002")])
    assert len(orders) == 2 and all(o["side"] == "buy" for o in orders)
    deals = normalize_deals([_real_deal()])
    assert deals[0]["deal_id"] == "1001#7"


def test_non_dict_rows_pass_through():
    """脏数据不炸整轮（与 run_classic 的宽容一致），原样返回。"""
    assert normalize_orders("not-a-list") == "not-a-list"
    assert normalize_deals([1, None, {}])[0] == 1


def test_status_label_covers_all_platform_states():
    for st, cn in [("pending", "待成交"), ("partial", "部分成交"), ("filled", "已成交"),
                   ("cancelled", "已撤单"), ("rejected", "废单"), ("unknown", "未知")]:
        assert status_label(st) == cn
    # 未知状态原样返回，绝不静默显示成「已成交」
    assert status_label("weird_state") == "weird_state"
    assert status_label(None) == "--"
