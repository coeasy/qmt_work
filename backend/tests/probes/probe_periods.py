"""周期能力实测（方案 A4）：穷举候选 period 值，验证 eltdx 实际返回什么。

用途：确定 CANONICAL_PERIODS 的真实可用范围，特别是季线（quarter）是否支持。
零 mock：直接打真实数据源；无网络/无数据时如实报告，不伪造结论。

用法：
    cd backend
    python tests/probe_periods.py [code]
"""
import asyncio
import os
import sys
from statistics import median

# 脚本位于 backend/tests/，需把 backend/ 加入模块搜索路径才能 import app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CODE = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"

# canonical -> 待测候选值（含别名，验证哪些被服务端接受）
CANDIDATES = [
    ("1m", ["1m", "1min", "minute", "min"]),
    ("5m", ["5m"]),
    ("15m", ["15m"]),
    ("30m", ["30m"]),
    ("60m", ["60m"]),
    ("1d", ["1d", "day", "d"]),
    ("1w", ["1w", "week", "w"]),
    ("1mo", ["1mo", "1mon", "month", "mo"]),
    ("1q", ["1q", "quarter", "q", "3mo", "3mon", "season"]),
    ("1y", ["1y", "year", "y"]),
]


def _fmt_dt(v):
    s = str(v)
    return s[:19]


def _gaps(dts):
    """相邻 bar 时间差（天），取中位数。"""
    from datetime import datetime

    def parse(s):
        s = str(s)[:19].replace("/", "-")
        # eltdx 的 dt 可能是 20260828(日) / 202608281430(分) / 2026-08-28(ISO)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    ps = [parse(d) for d in dts]
    ps = [p for p in ps if p]
    if len(ps) < 2:
        return None
    diffs = [(ps[i + 1] - ps[i]).total_seconds() / 86400.0 for i in range(len(ps) - 1)]
    diffs = [d for d in diffs if d > 0]
    return median(diffs) if diffs else None


async def probe_one(src, raw_period):
    """返回 (bar数, 首dt, 末dt, 间隔中位数天) 或 异常信息。"""
    try:
        bars = await src.get_kline(CODE, period=raw_period, count=30)
    except Exception as e:  # noqa: BLE001 - 探测脚本需捕获一切
        return ("ERR", f"{type(e).__name__}: {e}")
    if not bars:
        return ("EMPTY", None, None, None)
    dts = [b.get("dt") or b.get("time") or b.get("date") for b in bars]
    return ("OK", len(bars), _fmt_dt(dts[0]), _fmt_dt(dts[-1]), _gaps(dts))


async def main():
    from datasource.eltdx_source import EltdxSource

    src = EltdxSource()
    print(f"标的: {CODE}  数据源: {type(src).__name__}")
    print("=" * 96)
    print(f"{'canonical':<10}{'raw':<10}{'结果':<8}{'根数':<6}{'首':<22}{'末':<22}{'间隔(天)中位数'}")
    print("-" * 96)
    supported = {}
    for canonical, raws in CANDIDATES:
        for raw in raws:
            r = await probe_one(src, raw)
            if r[0] == "OK":
                _, n, d0, d1, gap = r
                gap_s = f"{gap:.2f}" if gap is not None else "—"
                print(f"{canonical:<10}{raw:<10}{'OK':<8}{n:<6}{d0:<22}{d1:<22}{gap_s}")
                if canonical not in supported:
                    supported[canonical] = (raw, gap, n)
            elif r[0] == "EMPTY":
                print(f"{canonical:<10}{raw:<10}{'空':<8}{'0':<6}{'—':<22}{'—':<22}{'—'}")
            else:
                msg = r[1][:40]
                print(f"{canonical:<10}{raw:<10}{'异常':<8}{'':<6}{msg}")
    print("=" * 96)
    print("结论（首个可用 raw 值 / 相邻间隔中位数天）：")
    for k, v in supported.items():
        gap_s = f"{v[1]:.2f}" if v[1] is not None else "—"
        print(f"  {k:<6} -> raw={v[0]:<8} 间隔={gap_s:<8} 根数={v[2]}")
    missing = [c for c, _ in CANDIDATES if c not in supported]
    if missing:
        print(f"  不支持: {missing}  → 这些周期必须前端置灰，绝不静默降级")


if __name__ == "__main__":
    asyncio.run(main())
