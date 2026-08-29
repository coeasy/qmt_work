# -*- coding: utf-8 -*-
"""第三轮：ETF 截断分布 + 大板块成分股量级（审计用）。"""
import asyncio
import collections
import sys

sys.path.insert(0, ".")

from app.datasource.eltdx_source import EltdxSource  # noqa: E402

src = EltdxSource()


async def main():
    etfs = await src.get_etf_list(5000)
    seg = collections.Counter(e["code"][:2] for e in etfs)
    print(f"ETF 全量 {len(etfs)}，代码段分布：{dict(sorted(seg.items()))}")
    cut = etfs[:800]
    seg_cut = collections.Counter(e["code"][:2] for e in cut)
    print(f"后端默认 limit=800 截断后分布：{dict(sorted(seg_cut.items()))}")
    for s in ("51", "56", "58", "15", "16"):
        total = seg.get(s, 0)
        got = seg_cut.get(s, 0)
        flag = "  <-- 被截断" if got < total else ""
        print(f"  段 {s}: 全量 {total} → 可见 {got}{flag}")

    print("\n=== 大板块成分股量级（F2 limit=50 是否够）===")
    boards = await src.get_boards("industry", "amount", 3)
    for b in boards:
        r = await src.get_board_constituents(b["code"], 500)
        n = len(r.get("items") or [])
        print(f"  {b['code']} {b['name']}: total={r.get('total')} 实返={n}"
              f"{'  <-- 超过前端 limit=50' if n > 50 else ''}")

    print("\n=== 概念板块成分股 ===")
    cs = await src.get_boards("concept", "amount", 2)
    for b in cs:
        r = await src.get_board_constituents(b["code"], 500)
        print(f"  {b['code']} {b['name']}: total={r.get('total')} "
              f"实返={len(r.get('items') or [])}")


if __name__ == "__main__":
    asyncio.run(main())
