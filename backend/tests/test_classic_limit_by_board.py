"""经典选股：涨跌停阈值必须**按板块**取，不能写死 9.8%（V11 R13）。

为什么需要它（2026-09-19 实测）
------------------------------
``_st_limit_up_shakeout`` / ``_st_uptrend_limit_down`` 把阈值写死成 ±9.8%，
而 A 股涨跌停幅度**按板块不同**：主板 ±10%、创业板(300/301)/科创板(688)/
北交所 ±20%、ST ±5%。

后果是双向的，且都不报错：

- ``limit_up_shakeout``（涨 >= 9.8% 才算涨停）会把 20cm 股票的**普通上涨**
  误判成涨停（阈值偏低）；
- ``uptrend_limit_down``（跌 <= -9.8% 才算跌停）在 20cm 板块**永远不成立**
  —— 它们跌停是 -20%，跌 -9.8% 根本不是跌停。

实测 1500 只样本、6 个策略共 11 次命中，``uptrend_limit_down`` 是**唯一 0 命中**
的那个，即由此而来。

讽刺的是项目里 :func:`datasource.board.limit_ratio` 早就算得很好
（覆盖可转债无限幅 / ST 5% / 双创北交 20% / 主板 10%），选股引擎只是没用。

修复：``_limit_pct(code, default)`` 按板块算阈值（含 0.98 容差，兼容
除权与四舍五入导致的 9.97% 这类实际值）；默认参数改为 ``None``
表示「按板块自动」，用户显式传值则尊重用户。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _Bar:
    def __init__(self, close, high=0.0, low=0.0, open_=0.0, volume=0.0,
                 amount=0.0):
        self.close = close
        self.high = high if high else close * 1.01
        self.low = low if low else close * 0.99
        self.open = open_ if open_ else close
        self.volume = volume
        self.amount = amount


def test_limit_pct_differs_by_board():
    """同样是「涨停」，主板 9.8、双创/北交 19.6、ST 4.9。"""
    from app.screener.classic import _limit_pct

    main = _limit_pct("600519.SH", 9.8)
    gem = _limit_pct("300750.SZ", 9.8)
    star = _limit_pct("688981.SH", 9.8)
    # ST 需名称，仅代码时按代码段判定（创业板/科创板是 20cm）
    assert abs(main - 9.8) < 0.01, main
    assert gem > main * 1.9, f"创业板应约 19.6，实际 {gem}"
    assert star > main * 1.9, f"科创板应约 19.6，实际 {star}"


def test_gem_limit_down_is_reachable():
    """★ 关键：创业板跌 -15% 不算跌停，但对 9.8% 阈值来说「算」。

    写死 -9.8 时，创业板股票跌 -15% 会被**误判**成跌停；
    按板块取阈值后，-15% 达不到 -19.6，正确地不算跌停。
    """
    from app.screener.classic import _limit_pct

    gem_threshold = -_limit_pct("300750.SZ", 9.8)
    assert gem_threshold < -19.0, f"创业板跌停阈值应约 -19.6，实际 {gem_threshold}"
    assert -15.0 > gem_threshold, "-15% 不应被判为创业板的跌停"


def test_uptrend_limit_down_uses_board_threshold():
    """端到端：同一组 K 线，创业板因阈值更宽而不命中，主板可命中。

    构造：MA20 上行、价在均线上、昨日 -12%、今日反包。
    - 主板（600xxx）：-12% <= -9.8 ⇒ 判为跌停 ⇒ 命中；
    - 创业板（300xxx）：-12% > -19.6 ⇒ 不算跌停 ⇒ 不命中。
    """
    from app.screener.classic import evaluate_classic

    bars = []
    for i in range(30):
        bars.append(_Bar(close=10.0 + i * 0.1))     # 稳定上行，MA20 向上
    # 昨日 -12%（相对**前一根**算，不是相对自身），今日反包（收在昨日最高之上）
    base = bars[-3].close
    bars[-2] = _Bar(close=base * 0.88, high=base * 0.99)
    bars[-1] = _Bar(close=base * 1.02)

    ok_main, detail_main = evaluate_classic(bars, "uptrend_limit_down",
                                            code="600519.SH")
    ok_gem, detail_gem = evaluate_classic(bars, "uptrend_limit_down",
                                          code="300750.SZ")
    # 主板 -12% 已达跌停阈值
    assert detail_main.get("had_limit_down") is True, detail_main
    # 创业板 -12% 不是跌停
    assert detail_gem.get("had_limit_down") is False, detail_gem
    assert ok_gem is False


def test_user_can_override_board_threshold():
    """用户显式传阈值时尊重用户（自动判定只作用于默认值 None）。"""
    from app.screener.classic import evaluate_classic

    bars = [_Bar(close=10.0 + i * 0.1) for i in range(30)]
    base = bars[-3].close
    # -6%：明确越过用户给的 -5 阈值（避免 -5.0 <= -5.0 的浮点边界）
    bars[-2] = _Bar(close=base * 0.94, high=base * 0.99)
    bars[-1] = _Bar(close=base * 1.02)

    ok, detail = evaluate_classic(bars, "uptrend_limit_down",
                                  params={"limit_down_pct": -5.0},
                                  code="300750.SZ")
    assert detail.get("had_limit_down") is True, "显式传 -5 应生效（覆盖板块自动值）"
    # 同一组数据若走板块自动值（创业板 -19.6）则不判跌停
    _, detail_auto = evaluate_classic(bars, "uptrend_limit_down",
                                      code="300750.SZ")
    assert detail_auto.get("had_limit_down") is False, "未显式传时按板块取 -19.6"


def test_default_params_are_none_not_hardcoded():
    """默认参数必须是 None（按板块自动），不能是写死的 9.8。"""
    from app.screener.classic import CLASSIC_STRATEGIES

    assert CLASSIC_STRATEGIES["limit_up_shakeout"]["params"]["limit_up_pct"] is None
    assert CLASSIC_STRATEGIES["uptrend_limit_down"]["params"]["limit_down_pct"] is None
