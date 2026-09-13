"""V9 Phase 7 DoD：自研 cron 解析语义。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.runtime.cron import CronExpr, validate  # noqa: E402


def test_parse_and_matches_basic():
    e = CronExpr.parse("30 18 * * 1-5")
    assert e.matches(datetime(2026, 9, 11, 18, 30))       # 周五 18:30
    assert not e.matches(datetime(2026, 9, 12, 18, 30))   # 周六
    assert not e.matches(datetime(2026, 9, 11, 18, 31))


def test_step_and_list():
    e = CronExpr.parse("*/15 9-15 * * *")
    assert e.matches(datetime(2026, 9, 11, 9, 0))
    assert e.matches(datetime(2026, 9, 11, 15, 45))
    assert not e.matches(datetime(2026, 9, 11, 9, 10))
    e2 = CronExpr.parse("0 9,15 * * *")
    assert e2.matches(datetime(2026, 9, 11, 15, 0))
    assert not e2.matches(datetime(2026, 9, 11, 16, 0))


def test_day_dow_vixie_or_semantics():
    # 日=13 且 周一：vixie 语义 OR —— 9 月 13 日（周日）或任一周一都触发
    e = CronExpr.parse("0 12 13 * 1")
    assert e.matches(datetime(2026, 9, 13, 12, 0))   # 周日但 day=13
    assert e.matches(datetime(2026, 9, 7, 12, 0))    # 周一但 day≠13
    assert not e.matches(datetime(2026, 9, 8, 12, 0))


def test_next_after():
    e = CronExpr.parse("30 18 * * 1-5")
    nxt = e.next_after(datetime(2026, 9, 11, 18, 30))   # 周五 18:30 → 下周一
    assert nxt == datetime(2026, 9, 14, 18, 30)
    nxt2 = e.next_after(datetime(2026, 9, 11, 10, 0))   # 周五当天
    assert nxt2 == datetime(2026, 9, 11, 18, 30)


def test_invalid_expressions_raise():
    for bad in ("* * * *", "61 18 * * *", "* 25 * * *", "a b c d e",
                "0 12 32 * *", "*/0 * * * *"):
        with pytest.raises(ValueError):
            validate(bad)


def test_dow_7_is_sunday():
    e = CronExpr.parse("0 9 * * 7")
    assert e.matches(datetime(2026, 9, 13, 9, 0))   # 周日
