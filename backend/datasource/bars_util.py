"""K 线批次小工具（P1-1 自 ``registry.py`` 拆出）。

``bars_last_date`` 是 ``datasource.registry`` 的**公开 API**（``app/sync/bars.py``
与 ``tests/test_bar_date_unity.py`` 都从那里导入），因此 registry 仍然 re-export 它
—— 本次拆分只挪实现，不动任何调用方。

为什么值得单独成模块：它是一个**纯函数**（无状态、无 IO），却被埋在 1296 行的
registry.py 里，于是「想单测一个纯函数」得先把整个数据源路由中心 import 起来。
"""
from __future__ import annotations

from core.clock import bar_date  # K 线交易日格式唯一入口（V11 R13）

def bars_last_date(bars) -> str:
    """取一批 K 线里**最后一根**的交易日（``"YYYYMMDD"``；无法解析返回 ``""``）。

    ★ 不假设 bars 已按时间升序：实测券商与在线源都升序，但补洞/合并路径不保证，
    直接取 ``bars[-1]`` 会拿错。这里对所有行归一化后取最大，代价 O(n)，
    n 通常 <= 320，可接受。
    """
    if not bars:
        return ""
    latest = ""
    for b in bars:
        if isinstance(b, dict):
            raw = b.get("time")
        else:
            raw = getattr(b, "time", None)
        d = bar_date(raw)
        if d and d > latest:
            latest = d
    return latest
