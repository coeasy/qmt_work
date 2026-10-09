"""公式选股执行基准（合成数据，零网络零仓依赖）。

复刻 app/screener/engine.py docstring 的实测口径（沪深 A 股规模
5000 只 × 250 根，MA20+RSI14 三条件），量化公式执行效率优化效果：
- P0-A registry.calc(raw=True)：numpy 直出 vs list 转换
- P0-B 字段整列缓存：一次求值内同字段只建一次

用法（后端 venv）：
    python scripts/bench_formula_scan.py [--codes 5000] [--bars 250] [--repeat 3]

注意：基准走 evaluate_scan（纯 CPU），不含取数；取数侧效率由
scan_async 结果缓存（P0-C）与在线并发（P1）承担，无法离线复现。
"""
from __future__ import annotations

import argparse
import statistics
import time

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_universe(n_codes: int, n_bars: int):
    """确定性合成 K 线：正弦+趋势+噪声。

    ★ 用轻量**对象**（SimpleNamespace）而非 dict：evaluate_scan 的价格前置
    过滤按属性读（``getattr(last, "close")``），与生产输入 Bar/BarLite 同构。
    """
    import math
    from types import SimpleNamespace

    codes = [f"SYM{i:05d}.SZ" for i in range(n_codes)]
    bars_map: dict[str, list] = {}
    for k, code in enumerate(codes):
        bars = []
        base = 10.0 + (k % 97) * 0.37
        for i in range(n_bars):
            close = base * (1.0 + 0.02 * math.sin(i / 13.0 + k) + 0.001 * i)
            bars.append(SimpleNamespace(
                time=f"2026{i:04d}",
                open=close * 0.995,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000_000.0 * (1.0 + 0.3 * math.sin(i / 7.0 + k * 0.1)),
            ))
        bars_map[code] = bars
    return codes, bars_map


def _run(codes, bars_map, repeat: int) -> dict:
    from app.screener.engine import evaluate_scan

    cond = {"and": [
        {"indicator": {"name": "ma", "params": {"win": 20}, "output": "ma",
                       "op": "gt", "value": 0, "window": -1}},
        {"indicator": {"name": "rsi", "params": {"win": 14}, "output": "rsi",
                       "op": "lt", "value": 70, "window": -1}},
        {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}},
    ]}
    times, hits, scanned = [], 0, 0
    for _ in range(repeat):
        t0 = time.perf_counter()
        results, scanned, _ms = evaluate_scan(codes, bars_map, cond)
        times.append(time.perf_counter() - t0)
        hits = len(results)
    return {"median_s": statistics.median(times), "min_s": min(times),
            "hits": hits, "scanned": scanned, "repeat": repeat}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=5000)
    ap.add_argument("--bars", type=int, default=250)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    print(f"生成合成数据：{args.codes} 只 × {args.bars} 根 …", flush=True)
    t0 = time.perf_counter()
    codes, bars_map = _make_universe(args.codes, args.bars)
    print(f"  数据就绪 {time.perf_counter() - t0:.2f}s\n", flush=True)

    r = _run(codes, bars_map, args.repeat)
    print("=" * 56)
    print("evaluate_scan 基准（MA20 + RSI14 + close 三条件）")
    print(f"  标的数        : {args.codes}（× {args.bars} 根）")
    print(f"  重复          : {args.repeat}")
    print(f"  中位耗时      : {r['median_s']*1000:.0f} ms")
    print(f"  最快耗时      : {r['min_s']*1000:.0f} ms")
    print(f"  命中 / 扫描   : {r['hits']} / {r['scanned']}")
    print("=" * 56)
    print("参照：优化前实测 ~1.91s（5000×250，同口径，engine.py docstring）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
