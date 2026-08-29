# -*- coding: utf-8 -*-
"""P1-P4 实测第二轮：字段完整性 + 口径语义 + 性能量化（审计用）。"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from app.datasource.eltdx_source import EltdxSource  # noqa: E402

src = EltdxSource()


async def main():
    print("=== 1. 指数快照字段完整性（E1/E2 依赖 name + change_pct）===", flush=True)
    for c in ("000001.SH", "000300.SH", "899050.BJ"):
        q = await src.get_quote(c)
        print(f"  {c}: name={q.get('name')!r} last={q.get('last')} "
              f"change={q.get('change')} change_pct={q.get('change_pct')} "
              f"lastClose={q.get('lastClose')}", flush=True)

    print("=== 2. 板块成分股内容（F2 实时性）===", flush=True)
    r = await src.get_board_constituents("881057.SH", 5)
    print(f"  total={r.get('total')} items={r.get('items')}", flush=True)

    print("=== 3. 统计类板块口径（stat 语义核对）===", flush=True)
    st = await src.get_boards("stat", "pct", 3)
    for x in st:
        print(f"  {x['code']} {x['name']}: last={x['last']} "
              f"lastClose={x['lastClose']} pct={x['change_pct']}", flush=True)

    print("=== 4. ETF 全量清单（H1 覆盖率）===", flush=True)
    t0 = time.time()
    all_etf = await src.get_etf_list(5000)
    print(f"  全量 ETF 数量={len(all_etf)} 耗时={time.time() - t0:.2f}s "
          f"（后端 /market/etfs 默认 limit=800 → 覆盖 "
          f"{min(800, len(all_etf))}/{len(all_etf)}）", flush=True)
    t0 = time.time()
    cached = await src.get_etf_list(5000)
    print(f"  二次（缓存）耗时={time.time() - t0:.2f}s 数量={len(cached)}", flush=True)

    print("=== 5. 批量快照性能（/market/etfs?with_quote=true 风险量化）===", flush=True)
    sample = [e["code"] for e in all_etf[:60]]
    t0 = time.time()
    res = await asyncio.gather(*[src.get_quote(c) for c in sample],
                               return_exceptions=True)
    ok_n = sum(1 for x in res if isinstance(x, dict) and x.get("last") is not None)
    cost = time.time() - t0
    print(f"  并发 60 只快照：耗时={cost:.2f}s 成功={ok_n}/60 "
          f"→ 推算 800 只约 {cost / 60 * 800:.0f}s（后端单只超时上限 5s，"
          f"超时即丢弃该 ETF）", flush=True)

    print("=== 6. 板块榜全量耗时（F1 验收 3s 内）===", flush=True)
    t0 = time.time()
    b = await src.get_boards("industry", "pct", 120)
    print(f"  limit=120 热点缓存命中耗时={time.time() - t0:.2f}s 返回={len(b)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
