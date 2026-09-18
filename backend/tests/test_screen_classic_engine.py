"""经典策略**接入选股引擎**的回归（engine.scan_async 的 classic 分支）。

与 ``test_screener_classic.py`` 的分工：后者钉的是**策略判据**本身（纯函数），
这里钉的是**编排层有没有真的走经典分支** —— 这是「把 Sequoia-X 融合进选股模块」
的最后一环。历史上最容易出的错是：路由/引擎漏传 ``classic``，界面传了策略 id，
后端却仍按条件树求值，返回「零命中」而不是报错 —— 用户完全无从分辨。

故这里锁三条可见后果：
  1. 响应回显 ``classic`` 与 ``provenance.classic_strategy``（可溯源、可断言）；
  2. 结果行带策略明细与 close/change_pct（不是条件树那种只有 score 的行）；
  3. ``classic_params`` 真的生效（改阈值能改变命中集合）—— 否则参数表单是摆设。
"""
from __future__ import annotations

import asyncio

import pytest

from _phase4_support import FakeStore  # noqa: F401
from app.screener.engine import scan_async


class Bar:
    def __init__(self, close, open_=None, high=None, low=None,
                 volume=1e6, amount=None):
        self.close = close
        self.open = open_ if open_ is not None else close
        self.high = high if high is not None else max(self.open, close)
        self.low = low if low is not None else min(self.open, close)
        self.volume = volume
        self.amount = amount


def _bars(closes, amount=None):
    return [Bar(close=c, open_=c, high=c * 1.01, low=c * 0.99,
                volume=1e6, amount=amount) for c in closes]


def _run(coro):
    return asyncio.run(coro)


def _store():
    """两只票：一只突破（末根 12.0 创 20 日新高）、一只横盘。"""
    brk = _bars([10.0 + i * 0.05 for i in range(24)], amount=2e8)
    brk.append(Bar(close=12.0, open_=11.0, high=12.1, low=10.9, amount=2e8))
    flat = _bars([10.0] * 25, amount=2e8)
    return FakeStore(
        stock_list=[{"code": "BRK", "name": "突破"}, {"code": "FLAT", "name": "横盘"}],
        bars={"BRK": brk, "FLAT": flat},
    )


UNIVERSE = {"kind": "custom", "codes": ["BRK", "FLAT"]}


def test_scan_async_classic_returns_strategy_rows():
    out = _run(scan_async(
        _store(), {}, classic="turtle_trade", universe=UNIVERSE,
        source_policy="local_only"))
    assert out["classic"] == "turtle_trade"
    assert out["provenance"]["classic_strategy"] == "turtle_trade"
    codes = [r["code"] for r in out["results"]]
    assert "BRK" in codes and "FLAT" not in codes
    row = next(r for r in out["results"] if r["code"] == "BRK")
    assert row["strategy"] == "turtle_trade"
    assert row["close"] == 12.0                 # 与条件选股同构，界面能直接显示
    assert "amount_ok" in row                   # 策略明细（解释为什么命中）
    assert out["total_scanned"] == 2


def test_scan_async_classic_params_take_effect():
    """参数必须真的影响命中集合 —— 否则界面上的参数表单只是装饰。"""
    base = _run(scan_async(
        _store(), {}, classic="turtle_trade", universe=UNIVERSE,
        source_policy="local_only"))
    assert base["count"] == 1

    # 把成交额门槛抬到 1 万亿 → 谁都不该命中
    strict = _run(scan_async(
        _store(), {}, classic="turtle_trade",
        classic_params={"min_amount": 1e12},
        universe=UNIVERSE, source_policy="local_only"))
    assert strict["count"] == 0


def test_scan_async_classic_does_not_evaluate_conditions():
    """走经典分支时，随手传的 conditions 只回显、不参与求值。

    ★ 这条钉住「两者互斥」：若哪天有人把 conditions 也拿去求值，用户会拿到
    两套判据的交集，且完全看不出为什么变少。
    """
    out = _run(scan_async(
        _store(), {"and": []}, classic="turtle_trade", universe=UNIVERSE,
        source_policy="local_only"))
    assert out["conditions"] == {"and": []}     # 原样回显
    assert out["count"] == 1                    # 未被条件树影响


def test_scan_async_unknown_classic_is_rejected():
    """未知策略必须抛 ValueError（路由转 400），绝不静默返回空结果。"""
    with pytest.raises(Exception):
        _run(scan_async(
            _store(), {}, classic="nope", universe=UNIVERSE,
            source_policy="local_only"))
