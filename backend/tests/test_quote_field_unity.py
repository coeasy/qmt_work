"""行情字段提取的唯一入口（V11 R14）。

被锁住的缺陷
------------
「最新价」此前有 **5 套各自为政的实现**，且键序不一致：

- ``app/main.py:55`` —— ``price → last → lastPrice → close``
- ``app/bootstrap/phase_engines.py:98`` —— ``preClose → lastClose → prevClose``（昨收）
- ``gateway/execution.py:38`` —— ``last → ask → bid``
- ``gateway/signal_router.py:268`` —— 同上（复制粘贴）
- ``engines/strategy_runtime.py:382`` —— ``lastPrice → last → price``

同一份行情在不同链路可能取到**不同价格**：某源同时给出 ``price`` 与
``lastPrice`` 时，风控按 ``price`` 判偏离、策略按 ``lastPrice`` 判涨跌停。

本文件既测语义（按用途的三个函数），也用**源码扫描**防止内联实现回潮。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.quote_fields import (
    BOOK_KEYS,
    LAST_PRICE_KEYS,
    PREV_CLOSE_KEYS,
    pick_last_price,
    pick_order_ref_price,
    pick_prev_close,
)

BACKEND = Path(__file__).resolve().parents[1]


# ------------------------------------------------------- 1. 最新价
def test_last_price_prefers_native_name():
    """原生名 lastPrice 优先于契约名 price（与项目「原生名优先」映射一致）。"""
    q = {"price": 10.0, "last": 10.5, "lastPrice": 11.0}
    assert pick_last_price(q) == 11.0


def test_last_price_falls_back_in_declared_order():
    assert pick_last_price({"last": 10.5, "price": 10.0}) == 10.5
    assert pick_last_price({"price": 10.0, "close": 9.9}) == 10.0
    assert pick_last_price({"close": 9.9}) == 9.9


def test_last_price_never_returns_book_price():
    """盘口价**不得**冒充最新价（那是 pick_order_ref_price 的职责）。"""
    assert pick_last_price({"ask": 11.0, "bid": 10.9}) is None
    assert pick_order_ref_price({"ask": 11.0, "bid": 10.9}) == 11.0


@pytest.mark.parametrize("bad", [None, "", 0, 0.0, -1, "abc", [], {}, "  "])
def test_last_price_rejects_invalid(bad):
    """空/非数/非正一律 None —— **绝不用 0 或负数冒充价格**。"""
    assert pick_last_price({"price": bad}) is None


def test_last_price_accepts_numeric_string():
    assert pick_last_price({"price": "10.50"}) == 10.5


def test_non_dict_input_is_safe():
    for bad in (None, [], "x", 3):
        assert pick_last_price(bad) is None
        assert pick_prev_close(bad) is None
        assert pick_order_ref_price(bad) is None


# ------------------------------------------------------- 2. 昨收
def test_prev_close_order():
    assert pick_prev_close({"preClose": 9.0, "lastClose": 9.5}) == 9.0
    assert pick_prev_close({"lastClose": 9.5, "prevClose": 9.4}) == 9.5
    assert pick_prev_close({"prevClose": 9.4}) == 9.4


def test_prev_close_does_not_fall_back_to_last():
    """昨收缺失必须返回 None —— 用最新价当昨收会算错涨跌停幅度。"""
    assert pick_prev_close({"last": 10.0, "close": 10.0}) is None


# ------------------------------------------------------- 3. 下单参考价
def test_order_ref_prefers_last_then_book():
    assert pick_order_ref_price({"last": 10.5, "ask": 11.0}) == 10.5
    assert pick_order_ref_price({"ask": 11.0, "bid": 10.9}) == 11.0
    assert pick_order_ref_price({"bid": 10.9}) == 10.9
    assert pick_order_ref_price({"open": 10.0}) is None


def test_key_tuples_are_declared_once():
    """键元组必须是模块级常量（供源码扫描与文档引用）。"""
    assert LAST_PRICE_KEYS[0] == "lastPrice"
    assert "close" in LAST_PRICE_KEYS
    assert PREV_CLOSE_KEYS[0] == "preClose"
    assert BOOK_KEYS == ("ask", "bid")


# ------------------------------------------------- 4. 源码扫描：防内联实现回潮
_OLD_PATTERNS: tuple[tuple[str, str], ...] = (
    # 内联的「最新价」键循环
    (r'for\s+k\s+in\s+\("price"\s*,\s*"last"', "内联最新价键循环（price 优先）"),
    (r'for\s+k\s+in\s+\("lastPrice"\s*,\s*"last"', "内联最新价键循环（lastPrice 优先）"),
    # 内联的「昨收」键循环
    (r'for\s+k\s+in\s+\("preClose"\s*,\s*"lastClose"', "内联昨收键循环"),
    (r'for\s+k\s+in\s+\("lastClose"\s*,\s*"preClose"', "内联昨收键循环"),
    # 内联的「下单参考价」表达式（曾复制粘贴两处）
    (r'\.get\("last"\)\s*or\s*\w+\.get\("ask"\)', "内联下单参考价（last or ask）"),
    (r'\.get\("lastPrice"\)\s*or\s*\w+\.get\("last"\)', "内联最新价 or 链"),
    (r'\.get\("lastClose"\)\s*or\s*\w+\.get\("preClose"\)', "内联昨收 or 链"),
)

_SKIP_DIRS = {"runtimes", "dist", "build", "__pycache__", "tests", ".venv", "node_modules"}


def _iter_backend_sources():
    for path in BACKEND.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name == "quote_fields.py":
            continue
        yield path


def test_no_inline_price_extraction_outside_quote_fields():
    """全 backend 不得再出现内联的行情字段提取（唯一入口 = core/quote_fields.py）。"""
    offenders: list[str] = []
    for path in _iter_backend_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover
            continue
        for pattern, label in _OLD_PATTERNS:
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(
                    f"{path.relative_to(BACKEND)}:{line} —— {label}")
    assert not offenders, (
        "发现内联的行情字段提取（应改调 core.quote_fields 的唯一入口）：\n  "
        + "\n  ".join(offenders))


def test_known_call_sites_use_the_shared_entry():
    """5 个历史位置必须已经改为调用共享入口。"""
    expectations = {
        "app/main.py": "pick_last_price",
        "app/bootstrap/phase_engines.py": "pick_prev_close",
        "gateway/execution.py": "pick_order_ref_price",
        "gateway/signal_router.py": "pick_order_ref_price",
        "engines/strategy_runtime.py": "pick_last_price",
    }
    for rel, symbol in expectations.items():
        text = (BACKEND / rel).read_text(encoding="utf-8", errors="ignore")
        assert f"from core.quote_fields import" in text, f"{rel} 未导入唯一入口"
        assert symbol in text, f"{rel} 未使用 {symbol}"
