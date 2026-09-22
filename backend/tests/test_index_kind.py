"""A7 回归护栏：eltdx 取 K 线时的 ``kind``（指数 / 个股）判定。

背景（2026-09-21 实测，用户报「从行情工作台打开时 K 线图无法正常展示」的**首要根因**）
----------------------------------------------------------------------------------------
``eltdx`` 的指数与个股走**两条不同的数据通道**：``cl.bars.get(..., kind="index")``
与 ``kind="stock"``（默认）。而通用 ``EltdxSource.get_kline`` 此前**从来没传过 kind**
⇒ 所有指数都按个股布局去解析字节流 ⇒ 抛
``ProtocolError: invalid kline date: <垃圾数字>``。

后果被放大成三层：
1. 指数 K 线**恒为空**（工作台默认标的正是 ``000001.SH`` 上证指数 ⇒ 一打开就是白板）；
2. 该异常冒到 ``registry._first_supported`` 被计成**数据源故障**，连续 3 次即
   **熔断 30s** ⇒ 一个指数的请求把整个 eltdx 源打成不可用，连个股 K 线/分时/资金流
   一起遭殃（实测日志：``数据源 eltdx 熔断（连续 3 次失败），冷却 30s``）；
3. 前端拿不到任何说明，只能显示一句通用猜测。

本文件锁住三件事：
- ``is_index_code`` 的判定表 —— **尤其「深市 000 段是个股」这条**（唯一防线）；
- ``get_kline`` 真的按判定结果选 kind；
- kind 猜错时 ``_bars_any_kind`` 会就地换另一边重试，且**两边都失败时异常必须抛出**
  （不得静默成空 —— 静默会把真故障藏起来，且熔断失去意义）。
"""
from __future__ import annotations

import asyncio

import pytest

from datasource.eltdx_utils import is_index_code


# ============================================================ 1. 判定表
# 每项：(代码, 期望, 说明)。说明写清「为什么」，避免后人误改。
_JUDGE_TABLE = [
    # ---- 沪市 000/999 段 = 指数 ----
    ("000001.SH", True, "上证指数"),
    ("000300.SH", True, "沪深300"),
    ("000905.SH", True, "中证500"),
    ("000852.SH", True, "中证1000"),
    ("999999.SH", True, "沪市 999 段为指数"),
    # ---- 沪市 880/881 段 = 通达信板块指数 ----
    ("880001.SH", True, "通达信行业板块指数"),
    ("881101.SH", True, "通达信概念板块指数"),
    # ---- ★★ 深市 000 段 = **个股**（本条是 A7 最关键的防线）----
    ("000001.SZ", False, "平安银行 —— 与 000001.SH 上证指数**只差交易所**"),
    ("000002.SZ", False, "万科A"),
    ("000063.SZ", False, "中兴通讯"),
    ("000333.SZ", False, "美的集团"),
    # ---- 深市只有 399 段是指数 ----
    ("399001.SZ", True, "深证成指"),
    ("399006.SZ", True, "创业板指"),
    ("399005.SZ", True, "中小100"),
    ("300999.SZ", False, "创业板个股"),
    ("301001.SZ", False, "创业板个股"),
    ("002594.SZ", False, "中小板个股（比亚迪）"),
    # ---- 北交所：只有 899 段是指数 ----
    ("899050.BJ", True, "北证50"),
    ("430047.BJ", False, "北交所个股"),
    ("830799.BJ", False, "北交所个股"),
    ("870204.BJ", False, "北交所个股"),
    ("920819.BJ", False, "北交所个股"),
    # ---- 常见个股 / 基金绝不可是指数 ----
    ("600519.SH", False, "贵州茅台"),
    ("601398.SH", False, "工商银行"),
    ("688111.SH", False, "科创板个股"),
    ("513090.SH", False, "ETF（5xx 段）"),
    ("510300.SH", False, "ETF（5xx 段）"),
    ("159915.SZ", False, "深市 ETF（1xx 段）"),
    ("113050.SH", False, "可转债"),
]


@pytest.mark.parametrize("code,expected,why", _JUDGE_TABLE,
                         ids=[f"{c}-{'指数' if e else '非指数'}" for c, e, _ in _JUDGE_TABLE])
def test_is_index_code_table(code, expected, why):
    assert is_index_code(code) is expected, (
        f"{code}（{why}）应判定为 {'指数' if expected else '非指数'}，"
        f"实得 {is_index_code(code)}")


def test_same_number_different_exchange_is_the_key_distinction():
    """★ 同一个数字、只差交易所，判定必须相反。

    这是本函数的**存在理由**：``000001.SH`` 是上证指数、``000001.SZ`` 是平安银行。
    任何「``code.startswith("000")`` 就是指数」式的实现都会在这里失败 ——
    而且失败方向是「把个股当指数取数」，属于**静默取到错数据**，比报错更难发现。
    """
    assert is_index_code("000001.SH") is True
    assert is_index_code("000001.SZ") is False


def test_lowercase_and_whitespace_tolerated():
    """大小写/空白不该改变判定（接口层常带空格）。"""
    assert is_index_code(" 000001.sh ") is True
    assert is_index_code("000001.sz") is False


def test_empty_or_garbage_is_not_index():
    for bad in ("", "   ", None, "ABC", "---"):
        assert is_index_code(bad) is False, f"{bad!r} 不应被判为指数"


def test_bare_code_defaults_to_sh():
    """不带交易所后缀时按沪市处理（项目既有约定：裸码视为沪市）。

    记录为**显式行为**而不是隐含假设：裸 ``000001`` → 沪市指数。
    若哪天改成「默认深市」，这条会立刻变红，提醒重新评估。
    """
    assert is_index_code("000001") is True
    assert is_index_code("600519") is False


# ============================================================ 2. get_kline 选 kind
class _FakeBar:
    def __init__(self, close=1.0):
        from datetime import datetime
        self.time = datetime(2026, 9, 21)
        self.open = close - 0.05
        self.high = close + 0.05
        self.low = close - 0.1
        self.close = close
        self.volume_lots = 100
        self.amount = 1000.0


class _FakeBarsAPI:
    """记录每次 ``get`` 的 kind；``fail_on`` 指定的 kind 会抛异常（模拟猜错）。"""

    def __init__(self, fail_on: str = "", msg: str = "ProtocolError: invalid kline date: 30869201"):
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.msg = msg

    def get(self, ec, period=None, count=None, adjust=None, kind=None):
        self.calls.append({"ec": ec, "period": period, "count": count,
                           "adjust": adjust, "kind": kind})
        if self.fail_on and kind == self.fail_on:
            raise RuntimeError(self.msg)
        return [_FakeBar(close=100.0)]


class _FakeClient:
    def __init__(self, bars: _FakeBarsAPI):
        self.bars = bars

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _source(monkeypatch, client):
    """造一个 EltdxSource，但把 ``_use_client`` 换成「直接把假 client 交给 fn」。"""
    from datasource.eltdx_source import EltdxSource

    src = EltdxSource()
    monkeypatch.setattr(src, "_use_client", lambda fn: fn(client))
    return src


def test_get_kline_passes_kind_index_for_sh_index(monkeypatch):
    """沪市指数必须传 ``kind="index"`` —— 改动前恒传默认 stock ⇒ 抛异常 + 熔断。"""
    api = _FakeBarsAPI()
    src = _source(monkeypatch, _FakeClient(api))
    out = asyncio.run(src.get_kline("000001.SH", "1d", 5))
    assert api.calls, "应当调用过 bars.get"
    assert api.calls[0]["kind"] == "index", (
        f"000001.SH 必须走指数通道，实得 kind={api.calls[0]['kind']!r}")
    assert out, "假 client 有数据，不应为空"


def test_get_kline_passes_kind_stock_for_sz_000(monkeypatch):
    """★ 关键防线：``000001.SZ``（平安银行）必须走 ``kind="stock"``。

    若判定写成「000 段即指数」，这里会把平安银行按指数字节流解析 ⇒
    要么报错、要么拿到一份**看起来正常但完全不相干**的指数 K 线。
    """
    api = _FakeBarsAPI()
    src = _source(monkeypatch, _FakeClient(api))
    asyncio.run(src.get_kline("000001.SZ", "1d", 5))
    assert api.calls[0]["kind"] == "stock", (
        f"000001.SZ 是个股，必须走个股通道，实得 kind={api.calls[0]['kind']!r}")


def test_get_kline_forwards_period_count_adjust(monkeypatch):
    """kind 之外，原有参数不得被顺手改掉（回归护栏）。"""
    api = _FakeBarsAPI()
    src = _source(monkeypatch, _FakeClient(api))
    asyncio.run(src.get_kline("600519.SH", "1d", 7, "qfq"))
    c = api.calls[0]
    assert c["count"] == 7
    assert c["kind"] == "stock"
    assert c["period"], "周期必须映射后传下去"


# ============================================== 3. kind 猜错时的就地重试（熔断保护）
def test_bars_any_kind_retries_other_kind_when_guessed_wrong():
    """猜错（这里假设成 stock，实际是指数）⇒ 自动换 index 重试一次并拿到数据。

    就地重试的价值不只是「拿到数据」，更是**不把自身可修复的错误上报成数据源故障**：
    异常若逃出 ``_inner``，会被 registry 计入熔断，连续 3 次就把整个 eltdx 源封 30s。
    """
    from datasource.eltdx_source import EltdxSource

    api = _FakeBarsAPI(fail_on="stock")
    out = EltdxSource._bars_any_kind(_FakeClient(api), "000001.SH", "1d", 5, "", "stock")
    assert [c["kind"] for c in api.calls] == ["stock", "index"], (
        f"应先按传入 kind 试、失败后换另一边，实得 {[c['kind'] for c in api.calls]}")
    assert out, "换边后应真的拿到数据"


def test_bars_any_kind_first_try_wins_when_kind_correct():
    """kind 正确时**只调一次**（不得无谓重试，避免翻倍打源）。"""
    from datasource.eltdx_source import EltdxSource

    api = _FakeBarsAPI()
    EltdxSource._bars_any_kind(_FakeClient(api), "000001.SH", "1d", 5, "", "index")
    assert [c["kind"] for c in api.calls] == ["index"]


def test_bars_any_kind_raises_when_both_kinds_fail():
    """两边都失败 ⇒ **异常必须抛出**，绝不静默成空。

    静默成空会同时毁掉两件事：① 用户看到白板且不知为什么（A8 修的就是这个）；
    ② 真故障不再计入熔断，坏掉的源会被反复重试而不是被及时隔离。
    """
    from datasource.eltdx_source import EltdxSource

    class _AlwaysFail(_FakeBarsAPI):
        def get(self, ec, period=None, count=None, adjust=None, kind=None):
            self.calls.append({"ec": ec, "kind": kind})
            raise RuntimeError("ProtocolError: invalid kline date: 30869201")

    api = _AlwaysFail()
    with pytest.raises(RuntimeError):
        EltdxSource._bars_any_kind(_FakeClient(api), "000001.SH", "1d", 5, "", "index")
    assert [c["kind"] for c in api.calls] == ["index", "stock"], "两边都该试过再抛"


# ============================================== 4. 判定表与实现的单一真源
def test_get_kline_uses_is_index_code_not_a_local_copy(monkeypatch):
    """``get_kline`` 必须复用 ``is_index_code``（单一真源），不得自己再抄一份前缀判断。

    做法：把 ``eltdx_source`` 模块里引用的 ``is_index_code`` 换成探针，
    若实现真的走它，探针必然被调用；抄一份本地逻辑则探针不会被调用。
    """
    import datasource.eltdx_source as mod

    seen: list[str] = []

    def _spy(code):
        seen.append(code)
        return True  # 一律当指数，便于观察 kind

    monkeypatch.setattr(mod, "is_index_code", _spy, raising=True)
    api = _FakeBarsAPI()
    src = _source(monkeypatch, _FakeClient(api))
    asyncio.run(src.get_kline("600519.SH", "1d", 5))
    assert seen == ["600519.SH"], "get_kline 未使用 is_index_code（可能抄了一份本地判断）"
    assert api.calls[0]["kind"] == "index", "探针返回 True ⇒ kind 必须跟着变 index"
