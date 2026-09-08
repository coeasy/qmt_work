"""周期契约回归测试（方案 A5）。

存在的意义：拦截 P0-1 类问题 —— 前后端周期枚举漂移 + 未知周期静默降级为日线。
用人肉点界面永远不会发现「点月线看到的是日线」，只有自动化断言相邻 bar 间隔
才能捕获（日线间隔 1 天 vs 月线间隔 ~30 天）。

分层：
  - 纯逻辑层（无网络）：归一化、别名、未知周期报错、不支持周期报错、元数据端点结构。
    → 任何环境都能跑，CI 必跑。
  - 数据层（需要 eltdx 联网）：断言各周期相邻 bar 间隔符合契约。
    → 网络不可用时自动 skip，不制造假红。

运行（项目约定：pytest 须逐文件运行，同进程跑全量会硬崩溃）：
    cd backend
    python -m pytest tests/test_period_contract.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datasource.periods import (  # noqa: E402
    CANONICAL_PERIODS,
    TICK_PERIOD,
    UnknownPeriodError,
    UnsupportedPeriodError,
    adjust_allowed_periods,
    all_periods,
    is_supported,
    kline_periods,
    normalize_period,
    spec,
    to_eltdx_period,
)

# ======================== 纯逻辑层（无网络依赖） ========================

def test_canonical_map_to_eltdx():
    """每个受支持周期都必须有 eltdx 入参，且不得为空。"""
    for canonical, s in CANONICAL_PERIODS.items():
        if s.supported:
            assert s.eltdx, f"{canonical} 标记 supported 但缺 eltdx 入参"
        else:
            assert not s.eltdx, f"{canonical} 标记不支持却有 eltdx 入参"
            assert s.reason, f"{canonical} 标记不支持但没写 reason（前端 tooltip 要用）"


def test_normalize_aliases():
    """历史别名必须归一化到 canonical（入向兼容，老前端不报错）。"""
    cases = {
        "1d": "1d", "day": "1d", "d": "1d",
        "1w": "1w", "week": "1w",
        # 关键：实测只有 month/1mon/mo 返回真月线，1mo 必须归一到 canonical 再映射为 month
        "1mo": "1mo", "1mon": "1mo", "month": "1mo", "mo": "1mo",
        "1q": "1q", "quarter": "1q", "3mo": "1q",
        "1y": "1y", "year": "1y",
        "1m": "1m", "1min": "1m", "minute": "1m",
        "60m": "60m", "1h": "60m",
    }
    for raw, expect in cases.items():
        assert normalize_period(raw) == expect, f"{raw} 应归一化为 {expect}"


def test_uppercase_and_whitespace_tolerated():
    assert normalize_period(" 1D ") == "1d"
    assert normalize_period("MONTH") == "1mo"


# 注意：不带 "1mo "/" 1d" 这类带空白的写法 —— 空白应被容忍（见
# test_uppercase_and_whitespace_tolerated），此处只测真正的未知值。
@pytest.mark.parametrize("bad", ["bogus", "2m", "2h", "10d", "1minute", "", None, "季", "1mos"])
def test_unknown_period_raises_not_fallback(bad):
    """核心契约：未知周期必须抛错，绝不静默降级为日线（P0-1 根因）。"""
    with pytest.raises(UnknownPeriodError):
        normalize_period(bad)
    # 兜底断言：任何情况下都不能悄悄变成 "1d"
    try:
        got = normalize_period(bad)
    except UnknownPeriodError:
        return
    assert got != "1d", f"未知周期 {bad!r} 被静默降级为日线"


def test_unsupported_period_raises():
    """季线：契约存在但数据源不支持 → UnsupportedPeriodError，且带原因。"""
    for raw in ("1q", "quarter", "q", "3mo", "season"):
        with pytest.raises(UnsupportedPeriodError) as ei:
            to_eltdx_period(raw)
        assert "季线" in str(ei.value)
        assert "日线" in str(ei.value), "错误文案应点明会降级为日线的风险"


def test_no_period_maps_to_day_unintentionally():
    """回归护栏：不得存在「多个 canonical 映射到同一个 eltdx 入参」的歧义
    （除合法场景外）。季线若哪天支持了，必须映射到新值而非 day。"""
    mapping = {c: s.eltdx for c, s in CANONICAL_PERIODS.items() if s.supported}
    reverse = {}
    for c, e in mapping.items():
        assert e not in reverse, f"周期 {c} 与 {reverse[e]} 映射到同一 eltdx 入参 {e}"
        reverse[e] = c
    assert mapping["1mo"] == "month", "月线必须映射为 month（实测 1mo 会被当无效值退回日线）"
    assert mapping["1d"] == "day"


def test_all_periods_metadata_shape():
    """`GET /market/periods` 的结构契约：前端据此渲染周期条。"""
    items = all_periods()
    assert items and isinstance(items, list)
    vs = [it["v"] for it in items]
    # 分时必须在首位，且与 K 线周期同列渲染
    assert vs[0] == TICK_PERIOD
    for it in items:
        assert {"v", "label", "kind", "supported", "reason"} <= set(it), f"字段缺失: {it}"
        assert it["label"], f"{it['v']} 缺中文标签"
        assert it["kind"] in ("tick", "minute", "kline")
        assert isinstance(it["supported"], bool)
        if not it["supported"]:
            assert it["reason"], f"{it['v']} 不支持但无 reason，前端无法解释"
    # 季线存在但置灰（不是消失 —— 消失会让用户以为漏功能）
    q = next(it for it in items if it["v"] == "1q")
    assert q["supported"] is False and q["reason"]


def test_kline_periods_excludes_unsupported():
    """参与缓存/同步的周期不得包含不支持项。"""
    kp = kline_periods()
    assert "1q" not in kp
    assert "1mo" in kp
    assert TICK_PERIOD not in kp, "分时不进 K 线缓存"


def test_adjust_allowed_periods_uses_contract():
    """复权周期集合必须来自契约常量，且包含月线（旧硬编码漏了 canonical 1mo）。"""
    allowed = adjust_allowed_periods()
    assert "1mo" in allowed, "旧硬编码 adj_periods 漏了 canonical '1mo'，月线复权曾失效"
    assert "1d" in allowed and "1w" in allowed
    assert "1m" not in allowed, "分钟线复权口径不一致，不应允许"


def test_is_supported_safe_for_unknown():
    assert is_supported("1d") is True
    assert is_supported("1q") is False
    assert is_supported("bogus") is False  # 未知不抛错，供 UI 判断


# ======================== 数据层（需要 eltdx 联网） ========================

def _gap_days(bars):
    from datetime import datetime
    from statistics import median

    def parse(s):
        s = str(s)[:19].replace("/", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    ps = [parse(b.get("time") or b.get("dt")) for b in bars]
    ps = [p for p in ps if p]
    if len(ps) < 2:
        return None
    diffs = [(ps[i + 1] - ps[i]).total_seconds() / 86400.0 for i in range(len(ps) - 1)]
    diffs = [d for d in diffs if d > 0]
    return median(diffs) if diffs else None


def _try_fetch():
    from datasource.eltdx_source import EltdxSource
    return EltdxSource()


@pytest.mark.parametrize("period", ["1d", "1w", "1mo", "1y"])
def test_period_returns_expected_granularity(period):
    """数据层核心断言：请求某周期，返回的 bar 间隔必须符合该周期量级。

    这是唯一能捕获「静默降级为日线」的自动化手段：
    日线间隔 ~1 天，周线 ~7 天，月线 ~30 天，年线 ~365 天。
    """
    try:
        src = _try_fetch()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"eltdx 不可用，跳过数据层校验：{e}")

    import asyncio
    try:
        bars = asyncio.run(src.get_kline("600519.SH", period=period, count=30))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"行情源不可用，跳过：{type(e).__name__}: {e}")

    if not bars:
        pytest.skip("数据源返回空，跳过间隔校验")

    gap = _gap_days(bars)
    assert gap is not None, f"{period} 无法解析 bar 时间"
    expect = spec(period).expect_gap_days
    assert expect is not None
    # 月线按自然月，28~31 天浮动；年线 365 左右；周线恰 7 天。
    # 允许 ±35% 容差（节假日/停牌会拉大相邻间隔）。
    lo, hi = expect * 0.65, expect * 1.35
    assert lo <= gap <= hi, (
        f"周期 {period} 返回的相邻间隔 {gap:.2f} 天不在期望区间 [{lo:.2f}, {hi:.2f}] "
        f"—— 极可能被静默降级为其他周期（典型：降级为日线会得到 ~1 天）"
    )
