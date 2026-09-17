"""行情字段契约测试：`_norm_quote` 必须同时满足两个消费方。

背景（真实缺陷）：迅投原生字段是 `lastPrice` / `lastClose`，归一化后是
`last` / `lastClose`；而界面契约（frontend-next/src/shared/types.ts::Quote）
读的是 `price` / `pre_close` / `change` / `change_pct`。
两边名字对不上时**既不报错也不兜底**，表现只是「行情通道已连接，但界面上
价格和涨跌幅永远是 --」—— 极易被误判成「没连上券商」。

因此这里锁死：
1. 原生名（sync 层 schema guard 与各引擎依赖）**不能删**；
2. 界面契约名必须齐全且数值正确。
"""
from __future__ import annotations

import asyncio

from sync import SyncEngine
from xtquant_client.xtp.quotes import QuotesMixin

#: 界面契约要求的字段（shared/types.ts::Quote）
UI_FIELDS = ("price", "pre_close", "change", "change_pct", "time")
#: 原生字段（sync._QUOTE_REQUIRED 与引擎在用）
NATIVE_FIELDS = ("code", "last", "lastClose", "ts")


def norm(tick: dict, code: str = "600519.SH") -> dict:
    """直接调 _norm_quote —— 它是纯函数（不读 self 属性），无需构造完整适配器。"""
    return QuotesMixin._norm_quote(QuotesMixin.__new__(QuotesMixin), code, tick)


def test_norm_quote_keeps_native_fields():
    out = norm({"lastPrice": 1500.0, "lastClose": 1490.0})
    for k in NATIVE_FIELDS:
        assert out.get(k) not in (None, ""), f"原生字段 {k} 丢失"


def test_norm_quote_exposes_ui_contract_fields():
    out = norm({"lastPrice": 1500.0, "lastClose": 1490.0})
    for k in UI_FIELDS:
        assert k in out, f"界面契约字段 {k} 缺失"


def test_change_and_pct_are_computed():
    out = norm({"lastPrice": 1500.0, "lastClose": 1490.0})
    assert out["price"] == 1500.0
    assert out["pre_close"] == 1490.0
    assert out["change"] == 10.0
    assert abs(out["change_pct"] - 0.6711) < 1e-3


def test_change_pct_is_negative_when_price_drops():
    out = norm({"lastPrice": 9.5, "lastClose": 10.0})
    assert out["change"] == -0.5
    assert out["change_pct"] == -5.0


def test_no_division_by_zero_when_pre_close_missing_or_zero():
    """★不变量：lastClose 缺失/为 0 时不给涨跌幅，而不是算出 inf/NaN。"""
    for tick in (
        {"lastPrice": 10.0},
        {"lastPrice": 10.0, "lastClose": 0},
        {"lastPrice": 10.0, "lastClose": None},
        {"lastPrice": 10.0, "lastClose": ""},
    ):
        out = norm(tick)
        assert out["change"] is None, tick
        assert out["change_pct"] is None, tick
        assert out["price"] == 10.0, tick


def test_change_is_none_when_last_missing():
    out = norm({"lastClose": 10.0})
    assert out["change"] is None
    assert out["change_pct"] is None


def test_kline_bar_key_names_still_work():
    """K 线 bar 用小写键名（close/preClose），归一化路径必须同样兼容。"""
    out = norm({"close": 20.0, "preClose": 19.0})
    assert out["last"] == 20.0
    assert out["price"] == 20.0
    assert out["pre_close"] == 19.0
    assert out["change_pct"] is not None


def test_norm_quote_survives_garbage_values():
    out = norm({"lastPrice": "abc", "lastClose": "def"})
    assert out["change"] is None and out["change_pct"] is None


def test_quote_reaches_sync_cache_with_ui_fields():
    """端到端：归一化结果喂给 SyncEngine.on_event 后，缓存里界面字段可用。"""
    class _Mgr:
        def active_bridge(self):
            return None

    eng = SyncEngine(_Mgr(), None)
    asyncio.run(eng.on_event({"type": "quote",
                              "data": norm({"lastPrice": 11.7, "lastClose": 11.5})}))
    q = eng.latest_quotes.get("600519.SH")
    assert q is not None
    assert q["price"] == 11.7
    assert q["change_pct"] is not None
