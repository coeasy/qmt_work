"""G1-5 全市场日线增量同步（窗口式，BarsSyncer）。

设计决策：
- **窗口增量**：eltdx / 券商 K 线接口均按 ``count`` 取「最近 N 根」，无
  date-range 参数；因此采用「拉最新 lookback 根 → 主键幂等合并」的窗口增量，
  而非逐日对账。数据正确性由 ``local_bars`` 主键 (code,period,adjust,dt) 的
  ``INSERT OR REPLACE`` 保证：重复日期覆盖、缺失日期补齐、旧数据不删除。
- **复权**：默认 ``qfq`` 走支持复权的补充源（eltdx），复权维度入主键，前后复权
  各自独立存储。
- **并发闸门**：信号量限并发（默认 8），G6 JobRuntime 落地前先本地收敛。
- **交易日历**：提供 ``weekday_calendar``（周一至周五启发式）作为兜底日期序列；
  真实节假日日历后续接入 eltdx 日历源后替换（标记 TODO(G1-5b)）。

用法（手动 CLI）：
    python -m app.sync.bars --limit 20 --concurrency 4 --lookback 320
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, List, Optional, Sequence

from app.datasource.local_store import LocalStore, get_store
from app.datasource.registry import get_hub

log = logging.getLogger("qmt_work.sync.bars")

#: 抓取器签名：(code, period, adjust, count) -> Optional[list[dict|Bar]]
FetchBars = Callable[[str, str, str, int], Awaitable[Optional[list]]]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def weekday_calendar(end: date, count: int) -> List[str]:
    """兜底交易日历：最近 ``count`` 个工作日（周一至周五）倒排序列。

    启发式，不含法定节假日修正；节假日精确日历待接入 eltdx 日历源（TODO(G1-5b)）。
    返回升序 "YYYY-MM-DD" 列表。
    """
    out: List[str] = []
    d = end
    while len(out) < count:
        if d.weekday() < 5:  # 0-4 = 周一至周五
            out.append(d.isoformat())
        d -= timedelta(days=1)
    out.reverse()
    return out


@dataclass
class SyncOutcome:
    """单标的同步结果。"""

    code: str
    ok: bool = False
    bars_written: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "ok": self.ok,
                "bars_written": self.bars_written, "error": self.error}


@dataclass
class SyncSummary:
    """一批同步的总览（供日志 / 后续 G6 进度事件 / 运维展示）。"""

    started: str
    finished: str = ""
    total: int = 0
    ok: int = 0
    failed: int = 0
    bars_written: int = 0
    elapsed_ms: int = 0
    errors: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started": self.started, "finished": self.finished,
            "total": self.total, "ok": self.ok, "failed": self.failed,
            "bars_written": self.bars_written, "elapsed_ms": self.elapsed_ms,
            "errors": self.errors,
        }


class BarsSyncer:
    """日线窗口同步器（并发闸门 + 幂等合并 + 结果汇总）。"""

    def __init__(
        self,
        store: Optional[LocalStore] = None,
        fetch_bars: Optional[FetchBars] = None,
        concurrency: int = 8,
        lookback: int = 320,
        period: str = "1d",
        adjust: str = "qfq",
    ):
        self._store = store or get_store()
        self._fetch = fetch_bars or self._default_fetch
        self._concurrency = max(1, int(concurrency))
        self._lookback = int(lookback)
        self._period = period
        self._adjust = adjust
        self._sem = asyncio.Semaphore(self._concurrency)

    # ------------------------------------------------------------------
    # 抓取
    # ------------------------------------------------------------------
    async def _default_fetch(self, code: str, period: str, adjust: str,
                             count: int) -> Optional[list]:
        """生产路径：真实多源回退（auto 链，复权走支持复权的补充源）。"""
        try:
            bars, _src = await get_hub().get_kline(
                code, period, count, source="auto", adjust=adjust)
        except Exception as exc:  # noqa: BLE001 单标的失败不击穿整批
            log.warning("sync fetch %s 失败：%s", code, exc)
            return None
        return bars

    # ------------------------------------------------------------------
    # 核心
    # ------------------------------------------------------------------
    async def sync_one(self, code: str) -> SyncOutcome:
        async with self._sem:
            try:
                bars = await self._fetch(code, self._period, self._adjust, self._lookback)
            except Exception as exc:  # noqa: BLE001
                return SyncOutcome(code=code, error=f"抓取异常：{exc}")
            if not bars:
                return SyncOutcome(code=code, error="源无数据（非交易时段或代码不受支持）")
            try:
                n = self._store.upsert_bars(code, bars, period=self._period,
                                            adjust=self._adjust)
            except Exception as exc:  # noqa: BLE001
                return SyncOutcome(code=code, error=f"落库失败：{exc}")
            return SyncOutcome(code=code, ok=True, bars_written=n)

    async def sync_many(self, codes: Sequence[str],
                        progress_cb: Optional[Callable[[int, int, str], None]] = None
                        ) -> SyncSummary:
        """批量同步（G6 进度钩子：progress_cb(done, total, code) 每完成一只回调）。"""
        started = now_iso()
        t0 = time.perf_counter()
        done = 0
        total = len(codes)

        async def _tracked(code: str):
            nonlocal done
            out = await self.sync_one(code)
            done += 1
            if progress_cb:
                progress_cb(done, total, code)
            return out

        outcomes = await asyncio.gather(*[_tracked(c) for c in codes])
        ok = [o for o in outcomes if o.ok]
        failed = [o for o in outcomes if not o.ok]
        summary = SyncSummary(
            started=started,
            finished=now_iso(),
            total=len(outcomes),
            ok=len(ok),
            failed=len(failed),
            bars_written=sum(o.bars_written for o in ok),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            errors=[o.to_dict() for o in failed],
        )
        self._store.set_meta("last_sync_at", summary.finished)
        if summary.failed:
            log.warning("同步完成：ok=%d failed=%d（%s）", summary.ok,
                        summary.failed, "; ".join(e["code"] for e in summary.errors[:5]))
        else:
            log.info("同步完成：ok=%d bars=%d elapsed=%dms",
                     summary.ok, summary.bars_written, summary.elapsed_ms)
        return summary

    async def sync_stock_list(self, limit: Optional[int] = None,
                              progress_cb: Optional[Callable[[int, int, str], None]] = None
                              ) -> SyncSummary:
        """同步全市场股票列表（先刷列表，再按需同步 K 线）。"""
        from app.datasource.registry import get_hub as _hub
        items = await _hub().get_stock_list(source="auto")
        if not items:
            return SyncSummary(started=now_iso(), finished=now_iso(),
                               total=0, failed=0, ok=0, errors=[],
                               elapsed_ms=0)
        self._store.upsert_stock_list(items)
        codes = [str(i.get("code")) for i in items if i.get("code")]
        if limit:
            codes = codes[: int(limit)]
        return await self.sync_many(codes, progress_cb=progress_cb)


async def _main(limit: int, concurrency: int, lookback: int) -> int:
    syncer = BarsSyncer(concurrency=concurrency, lookback=lookback)
    summary = await syncer.sync_stock_list(limit=limit or None)
    import json
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="G1-5 日线窗口同步")
    parser.add_argument("--limit", type=int, default=0, help="只同步前 N 只（0=全市场）")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--lookback", type=int, default=320)
    args = parser.parse_args()
    exit(asyncio.run(_main(args.limit, args.concurrency, args.lookback)))
