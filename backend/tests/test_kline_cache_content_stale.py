"""K 线缓存：TTL 未到但**内容陈旧**时必须回源（V11 R13）。

为什么需要它（2026-09-18 实测）
------------------------------
``KlineCache.ais_fresh`` 只比 TTL（日线 6 小时），不比**缓存里存的是什么**。
于是：券商本地历史只到 ``20260825`` 时被写进缓存，随后 6 小时内 ``ais_fresh``
恒为 True ⇒ ``get_or_fetch`` 直接返回缓存 ⇒ 界面一直显示 24 天前的 K 线 ——
而此时主仓 ``local_bars`` 其实已经通过券商补下载拿到当天的数据了。

``force=1`` 能绕过（实测立即拿到 ``20260918``），但这要求调用方知道要 force，
普通浏览永远看不到新数据。

TTL 语义是「这份数据多久前取的」，它管不了「取到的本身就是陈的」——
这是**第四处**「有数据但不够新」的漏洞（前三处：同步空转、对账假空态、
同步陈旧降级）。

修复：``ais_fresh`` 在 TTL 之外再判一次**内容新鲜度**，日线及以上周期的最后一根
距今超过 ``CACHE_STALE_DAYS``（10 自然日，与同步同口径）即视为不新鲜、触发回源。
分钟线当日有效，不参与（否则周末/非交易时段会每次都回源）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _FakeDB:
    """最小 DB 替身：只支持 aquery 返回预置行。"""

    def __init__(self, rows):
        self._rows = rows
        self.queries: list = []

    async def aquery(self, sql: str, params=()):
        self.queries.append(sql)
        return list(self._rows)


def _mk_cache(last_dt: str, period: str = "1d", count_rows: int = 300):
    from gateway.kline_cache import KlineCache

    rows = [{"dt": last_dt, "open": 1, "high": 2, "low": 0.5, "close": 1.5,
             "volume": 10, "amount": 15, "adjust": "qfq"}]
    kc = KlineCache.__new__(KlineCache)
    kc.db = _FakeDB(rows)   # count=1 时 aget 只走热表，不访问冷仓 _arch
    kc.hits = kc.misses = kc.stale_serves = 0
    # __new__ 绕过 __init__，TTL 得自己给（否则 ttl_for 取不到属性）
    kc.ttl_daily = 6 * 3600.0
    kc.ttl_intraday = 60.0
    return kc


def _old(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def test_content_stale_detects_old_last_bar():
    """★ 缓存最后一根是 24 天前 ⇒ 判为内容陈旧（TTL 未到也不算新鲜）。"""
    kc = _mk_cache(_old(24))
    assert asyncio.run(kc.acontent_stale("600519.SH", "1d", "qfq")) is True


def test_fresh_content_is_not_stale():
    """最后一根就是今天 ⇒ 不陈旧。"""
    kc = _mk_cache(_today())
    assert asyncio.run(kc.acontent_stale("600519.SH", "1d", "qfq")) is False


def test_weekend_gap_is_not_stale():
    """周末 3 天不误判 —— 否则非交易日每次查询都白回源。"""
    kc = _mk_cache(_old(3))
    assert asyncio.run(kc.acontent_stale("600519.SH", "1d", "qfq")) is False


def test_minute_period_never_content_stale():
    """分钟线当日有效，不参与内容判定（避免每分钟都回源）。"""
    kc = _mk_cache(_old(24), period="1m")
    assert asyncio.run(kc.acontent_stale("600519.SH", "1m", "")) is False


def test_unparsable_date_is_not_treated_as_stale():
    """日期解析不出来时按「不陈旧」处理 —— 无从判断就交给回源逻辑，不武断。"""
    kc = _mk_cache("not-a-date")
    assert asyncio.run(kc.acontent_stale("600519.SH", "1d", "qfq")) is False


def test_ais_fresh_is_false_when_content_stale():
    """★ 端到端：TTL 未到 + 内容陈旧 ⇒ ais_fresh 必须返回 False（触发回源）。

    这是本修复的关键行为：没有它，``get_or_fetch`` 会一直返回那份 24 天前的缓存。
    """
    kc = _mk_cache(_old(24))
    # 让 TTL 判定通过：把 last_fetch 设为「刚刚」
    async def _recent(*a, **k):
        return time.time()

    kc.alast_fetch = _recent  # type: ignore[assignment]
    kc.acount = lambda *a, **k: asyncio.sleep(0, result=300)  # type: ignore[assignment]
    assert asyncio.run(kc.ais_fresh("600519.SH", "1d", 250, "qfq")) is False
