"""行情字段提取的唯一入口（V11 R14）。

为什么必须有这个模块
--------------------
此前「最新价」在不同链路有 **5 套各自为政的实现**，而且**键序不一致**：

| 位置 | 用途 | 键序 |
|---|---|---|
| ``app/main.py:55`` | 风控价格偏离校验 | ``price → last → lastPrice → close`` |
| ``app/bootstrap/phase_engines.py:98`` | 昨收（涨跌停判定） | ``preClose → lastClose → prevClose`` |
| ``gateway/execution.py:38`` | 市价单风控参考价 | ``last → ask → bid`` |
| ``gateway/signal_router.py:268`` | 同上（复制粘贴） | ``last → ask → bid`` |
| ``engines/strategy_runtime.py:382`` | 策略 tick 判定 | ``lastPrice → last → price`` |

后果：同一份行情在不同链路可能取到**不同的价格**。某数据源同时给出
``price`` 与 ``lastPrice``（两者在除权/撮合瞬间可能不同）时，风控按 ``price``
判断、策略按 ``lastPrice`` 判断 —— 同一笔委托被两套价格口径审视。

本模块按**用途**（而非按文件）收敛为三个语义，各有唯一实现：

- :func:`pick_last_price` —— 最新价（展示、风控偏离校验）
- :func:`pick_prev_close` —— 昨收（涨跌停幅度判定）
- :func:`pick_order_ref_price` —— 下单参考价（市价单风控；可退到盘口可成交价）

键序遵循项目既有的「原生名优先」契约映射（迅投 ``lastPrice`` → ``last`` →
``price``），并统一要求**结果为正数**：拿不到就返回 ``None``，
**绝不用 0 或负数冒充**（与前端「无行情绝不回填 price:0」同一铁律）。
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "BOOK_KEYS",
    "LAST_PRICE_KEYS",
    "PREV_CLOSE_KEYS",
    "pick_last_price",
    "pick_order_ref_price",
    "pick_prev_close",
]

#: 最新价候选键，按优先级排列。原生名（lastPrice）优先，其次契约名。
LAST_PRICE_KEYS: tuple[str, ...] = ("lastPrice", "last", "price", "close")

#: 昨收候选键。三个名字在不同源/不同链路里都出现过。
PREV_CLOSE_KEYS: tuple[str, ...] = ("preClose", "lastClose", "prevClose")

#: 盘口可成交价候选键（仅 :func:`pick_order_ref_price` 兜底使用）。
BOOK_KEYS: tuple[str, ...] = ("ask", "bid")


def _positive(value: Any) -> Optional[float]:
    """把值转成正的 float；空/非数/非正一律 None（0 与负数都不是有效价格）。"""
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def pick_last_price(quote: Any) -> Optional[float]:
    """取最新价；取不到返回 ``None``。

    明确**不**回退到 ``ask``/``bid`` —— 那是盘口价，不是最新价。
    需要「能成交的参考价」请用 :func:`pick_order_ref_price`。
    """
    if not isinstance(quote, dict):
        return None
    for key in LAST_PRICE_KEYS:
        num = _positive(quote.get(key))
        if num is not None:
            return num
    return None


def pick_prev_close(quote: Any) -> Optional[float]:
    """取昨收价；取不到返回 ``None``（涨跌停判定缺它就无法成立）。"""
    if not isinstance(quote, dict):
        return None
    for key in PREV_CLOSE_KEYS:
        num = _positive(quote.get(key))
        if num is not None:
            return num
    return None


def pick_order_ref_price(quote: Any) -> Optional[float]:
    """取**下单参考价**：最新价优先，退到盘口（ask → bid）。

    为什么下单链路允许退到盘口：市价单风控需要的是「这笔单大概能成交在什么价」，
    没有最新价时盘口可成交价是合理代理。但**只有这条链路**允许这样做 ——
    展示与涨跌停判定不得用盘口价冒充最新价。
    """
    price = pick_last_price(quote)
    if price is not None:
        return price
    if not isinstance(quote, dict):
        return None
    for key in BOOK_KEYS:
        num = _positive(quote.get(key))
        if num is not None:
            return num
    return None
