"""腾讯公共源 K 线：复权键名必须解析对（V11 R13）。

为什么需要它（2026-09-18 实测）
------------------------------
全市场日线同步写入 163 万根却报「已完成」，而 5093 只股票的**最后一根停在
20250418**（一年多前）。追查链路后发现最后一环：

- 券商（QMT）本地历史只下载到 2025-04-18，但**照样非空** ⇒ 源链停在 broker；
- 本该降级到在线源，而腾讯源对这 5093 只**恒返回空**；
- 原因不是网络，是**解析取错了键**：接口对有复权历史的标的返回
  ``{"data": {"sh600519": {"qfqday": [...]}}}``，键名是 ``"<adj>day"``；
  而代码按 ``data.get(adj)`` 取 —— 键根本不叫 ``qfq``，于是恒为 0 行。

只有返回裸 ``day`` 键的标的（无除权历史的次新股 / 科创板新股）才侥幸成功，
实测全市场 5153 只里**恰好 49 只**属于这一类 —— 与库里 tencent provider 的
股票数完全吻合，互相印证。

这个 bug 的隐蔽性在于：它不会报错、不会让接口失败，只是**静默返回空列表**，
在源链里表现为「该源无数据」而继续往后走，最终退回那份陈旧数据。

锁住的修复：``data.get(f"{adj}day") or data.get(adj) or data.get("day")``，
且取不到时**写日志**而不是静默返回空。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _payload(key: str, rows=None) -> str:
    """构造腾讯返回：``data.<vendor>.<key>`` 里放 K 线行。"""
    rows = rows if rows is not None else [["2026-09-18", "1", "2", "3", "0.5", "10"]]
    return json.dumps({"code": 0, "data": {"sh600519": {key: rows}}})


@pytest.fixture
def src(monkeypatch):
    """腾讯源，``_get`` 被打桩成可控返回。"""
    from datasource.public_sources import TencentSource

    s = TencentSource()
    holder: dict = {}

    async def _get(url: str) -> str:
        return holder["body"]

    monkeypatch.setattr(s, "_get", _get)
    s._holder = holder  # type: ignore[attr-defined]
    return s


def _run(s, body: str, adjust: str = "qfq"):
    s._holder["body"] = body
    return asyncio.run(s.get_kline("600519.SH", "1d", 20, adjust))


def test_qfq_reads_qfqday_key(src):
    """★ qfq 的真实键名是 ``qfqday`` —— 此前按 ``qfq`` 取，恒 0 行。"""
    bars = _run(src, _payload("qfqday"), "qfq")
    assert len(bars) == 1, f"应解析出 1 根，实际 {len(bars)}"
    assert bars[0]["time"] == "2026-09-18"
    assert bars[0]["close"] == 2.0


def test_hfq_reads_hfqday_key(src):
    """hfq 对应 ``hfqday``。"""
    bars = _run(src, _payload("hfqday"), "hfq")
    assert len(bars) == 1


def test_unadjusted_reads_day_key(src):
    """不复权（或科创板无除权历史）返回裸 ``day`` —— 这是此前唯一能走通的路径。"""
    bars = _run(src, _payload("day"), "qfq")
    assert len(bars) == 1


def test_legacy_plain_adj_key_still_works(src):
    """万一接口改回裸 ``qfq`` 键，仍要能取到（向后兼容）。"""
    bars = _run(src, _payload("qfq"), "qfq")
    assert len(bars) == 1


def test_prefers_adjusted_over_raw(src):
    """同时有 ``qfqday`` 和 ``day`` 时优先复权序列（不能退化成不复权）。"""
    body = json.dumps({"code": 0, "data": {"sh600519": {
        "qfqday": [["2026-09-18", "1", "111", "3", "0.5", "10"]],
        "day": [["2026-09-18", "1", "999", "3", "0.5", "10"]],
    }}})
    bars = _run(src, body, "qfq")
    assert bars[0]["close"] == 111.0, "应取复权序列，而非裸 day"


def test_empty_payload_returns_empty_and_warns(src, caplog):
    """取不到行时不静默 —— 必须留日志，否则又是一次查不到原因的空。"""
    import logging

    with caplog.at_level(logging.WARNING):
        bars = _run(src, json.dumps({"code": 0, "data": {"sh600519": {}}}))
    assert bars == []
    assert "腾讯 K 线无数据" in caplog.text, "空结果必须留痕"


def test_all_six_fields_are_parsed(src):
    """行是 [time, open, close, high, low, volume] —— 注意第 3 位是 close 不是 high。"""
    bars = _run(src, _payload("qfqday", [["2026-09-18", "10", "12", "13", "9", "100"]]))
    b = bars[0]
    assert (b["open"], b["close"], b["high"], b["low"], b["volume"]) == (
        10.0, 12.0, 13.0, 9.0, 100.0)
