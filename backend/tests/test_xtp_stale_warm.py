"""券商 K 线「结果陈旧也要补下载」（V11 R13）。

为什么需要它（2026-09-19 实测）
------------------------------
``_warm_kline_cache`` 只在 ``get_market_data`` **返回空**时预热。但 QMT 客户端
本地历史可能只下载到 ``20250418`` —— 此时它**照样返回非空**（一年多前的 320 根），
于是「空才预热」**永不触发**，券商源永远吐一年前的行情。

后果链：

1. 同步的新鲜度门槛发现数据陈旧 ⇒ 降级到在线源；
2. 而在线源（腾讯 / 新浪）在全市场量级下**必然被限流**
   （实测腾讯 ``501``、新浪 ``456``）；
3. ⇒ 全市场 5224 只里 5010 只陈旧，数据一年多没更新。

**券商渠道本身不限流**，只要让它把缺口补上，整条链就活了。所以预热条件必须
从「空」扩展到「陈旧」，且下载窗口要从**最后一根**起算 —— 默认的「回看
count 天」够不着一年的缺口（本地停在 20250418、count=120 只覆盖到 2026-05）。

本测试用替身 xtdata 锁住三条：空结果仍预热（旧行为不退化）、陈旧结果要预热、
下载窗口按最后日期算；以及「同一状态只试一次」的 memo（否则全市场同步会
放大成数千次 RPC）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _FakeXtdata:
    """替身 xtdata：记录 download 调用，get_market_data 返回可控数据。"""

    def __init__(self, last_date: str = "", empty_first: bool = False):
        self.last_date = last_date
        self.empty_first = empty_first
        self.calls: list = []
        self.downloads: list = []
        self._served = False

    def download_history_data(self, *args, **kwargs):
        self.downloads.append(args)
        return None

    def get_market_data(self, **kwargs):
        self.calls.append(kwargs)
        if self.empty_first and not self._served:
            self._served = True
            return {}
        if not self.last_date:
            return {}
        import pandas as pd
        dates = [self.last_date]
        return {"close": pd.DataFrame(
            [[1.0]], index=["600519.SH"], columns=[self.last_date])}


@pytest.fixture(autouse=True)
def _clean_memo():
    """``_WARM_MEMO`` 是**类级**的，不清会跨用例泄漏，让后续用例误判「已预热过」。"""
    from xtquant_client.xtp.quotes import QuotesMixin

    QuotesMixin._WARM_MEMO.clear()
    yield
    QuotesMixin._WARM_MEMO.clear()


@pytest.fixture
def adapter():
    """构造一个 xtdata 可替换的 XTP quotes 适配器。"""
    from xtquant_client.xtp.quotes import QuotesMixin

    q = QuotesMixin.__new__(QuotesMixin)
    return q


def _old_date(days: int = 500) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def test_empty_result_still_warms(adapter):
    """旧行为不退化：空结果照样预热（本地从未下载过的标的）。"""
    fake = _FakeXtdata(last_date="20260918", empty_first=True)
    adapter._xtdata = fake
    adapter.get_kline("600519.SH", "1d", 20)
    assert fake.downloads, "空结果必须预热"


def test_stale_result_triggers_warm(adapter):
    """★ 结果非空但陈旧 ⇒ 必须预热（旧逻辑会漏掉这一整类）。"""
    fake = _FakeXtdata(last_date=_old_date(500))
    adapter._xtdata = fake
    adapter.get_kline("600519.SH", "1d", 20)
    assert fake.downloads, "陈旧数据必须触发补下载"


def test_fresh_result_does_not_warm(adapter):
    """数据够新时不得无谓打 RPC（全市场同步会放大成数千次）。"""
    today = datetime.now().strftime("%Y%m%d")
    fake = _FakeXtdata(last_date=today)
    adapter._xtdata = fake
    adapter.get_kline("600519.SH", "1d", 20)
    assert not fake.downloads, "数据新鲜时不该预热"


def test_warm_window_starts_from_last_bar(adapter):
    """★ 下载窗口从**最后一根**起算，不能用默认的「回看 count 天」。

    本地停在 500 天前、count=120 时，默认窗口只覆盖 122 天 —— 够不着缺口，
    补了也白补。
    """
    old = _old_date(500)
    fake = _FakeXtdata(last_date=old)
    adapter._xtdata = fake
    adapter.get_kline("600519.SH", "1d", 120)
    assert fake.downloads, "应触发预热"
    args = fake.downloads[0]
    start = args[2] if len(args) > 2 else None
    assert start == old, f"下载起点应是最后一根 {old}，实际 {start}"


def test_same_stale_state_warms_only_once(adapter):
    """同一状态只试一次：否则全市场同步会把 RPC 放大数千倍。"""
    old = _old_date(500)
    fake = _FakeXtdata(last_date=old)
    adapter._xtdata = fake
    type(adapter)._WARM_MEMO.clear()
    adapter.get_kline("600519.SH", "1d", 20)
    n1 = len(fake.downloads)
    adapter.get_kline("600519.SH", "1d", 20)
    assert len(fake.downloads) == n1, "同一陈旧状态不得反复预热"
    type(adapter)._WARM_MEMO.clear()


def test_minute_period_not_warmed_for_staleness(adapter):
    """分钟线数据量极大，不做「陈旧补下载」（只保留空结果预热）。"""
    fake = _FakeXtdata(last_date=_old_date(500))
    adapter._xtdata = fake
    adapter.get_kline("600519.SH", "1m", 20)
    assert not fake.downloads
