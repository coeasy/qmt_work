# -*- coding: utf-8 -*-
"""P1-P4 真机实测（一次性，审计用）：指数/板块/成分股/资金流/ETF/股本/涨跌停。

运行：backend 目录下 python tests/_probe_p1p4.py
"""
import asyncio
import sys
import time
import traceback

sys.path.insert(0, ".")

from app.datasource.eltdx_source import EltdxSource  # noqa: E402

src = EltdxSource()


async def try_(name, coro_fn):
    t0 = time.time()
    try:
        r = await coro_fn()
        cost = time.time() - t0
        if isinstance(r, list):
            desc = f"list[{len(r)}] 前2={r[:2]}"
        elif isinstance(r, tuple):
            desc = f"tuple len={len(r)} 首元素类型={type(r[0]).__name__}"
        elif isinstance(r, dict):
            desc = f"dict keys={list(r.keys())[:8]}"
        else:
            desc = repr(r)[:160]
        print(f"[OK ] {name}  {cost:.2f}s  {desc}", flush=True)
        return r
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] {name}  {time.time() - t0:.2f}s  "
              f"{type(e).__name__}: {str(e)[:200]}", flush=True)
        traceback.print_exc(limit=1)
        return None


async def main():
    print("=== P1 指数快照 ===", flush=True)
    for c in ("000001.SH", "000300.SH", "399006.SZ", "899050.BJ"):
        await try_(f"quote {c}", lambda c=c: src.get_quote(c))

    print("=== P2 板块 ===", flush=True)
    await try_("boards industry", lambda: src.get_boards("industry", "pct", 10))
    await try_("boards concept", lambda: src.get_boards("concept", "pct", 10))
    await try_("boards stat", lambda: src.get_boards("stat", "pct", 10))

    b = await try_("boards industry(取1个板块代码)", lambda: src.get_boards("industry", "pct", 1))
    code = (b[0]["code"] if b and b[0] else "881057.SH")
    print(f"--- 用板块 {code} 测成分股/K线 ---", flush=True)
    await try_("constituents", lambda: src.get_board_constituents(code, 10))
    await try_("board kline 1d", lambda: src.get_board_kline(code, "1d", 20))

    print("=== P3 资金流 ===", flush=True)
    await try_("moneyflow 600519.SH", lambda: src.get_moneyflow("600519.SH"))

    print("=== P4 ETF / 股本 / 涨跌停 ===", flush=True)
    await try_("etf list", lambda: src.get_etf_list(50))
    await try_("share capital", lambda: src.get_share_capital(["600519.SH", "000001.SZ"]))
    await try_("price limits", lambda: src.get_price_limits(["600519.SH", "000001.SZ"]))

    print("=== 分时 ===", flush=True)
    await try_("minutes 600519.SH", lambda: src.get_minutes("600519.SH"))


if __name__ == "__main__":
    asyncio.run(main())
