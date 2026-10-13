"""G1-5 同步器的**冷路径 mixin**（从 bars.py 按职责拆出）。

冷路径 = ``zone in {"cold","both"}`` 时补历史年区间到 ``kline_archive``
（独立 bars_cold.db），并写 ``cold_complete`` 标记。

这 5 个方法完全依赖 :class:`BarsSyncer` 的实例状态（``self._store`` /
``self._zone`` / ``self._period`` / ``self._full_years`` …），所以拆成 mixin
而非模块级函数：方法体**一字未改**，``BarsSyncer(ColdZoneMixin)`` 直接继承，
冷路径的编排逻辑与热路径共享同一个 ``self``，不存在状态拷贝或约定漂移。

被 :mod:`app.sync.bars` 导入并作为基类继承；不对外导出其它名字。
"""
from __future__ import annotations
import asyncio

import logging

import time



from typing import Sequence


from app.sync.bars_model import SyncOutcome, _bar_time, _bars_first_date

from core.clock import bar_date
from datasource.registry import bars_last_date

log = logging.getLogger('qmt_work.sync.bars_cold')


class ColdZoneMixin:
    def _cold_complete_map(self, codes: Sequence[str]) -> set:
        """已标记「冷历史补齐」的标的集合（单条 LIKE 查询，避免逐只读 meta）。"""
        if not codes:
            return set()
        # ★ 必须走 ``self._store._db.query``：``LocalStore`` 本身**没有** ``query`` 方法
        # （其内部一律用 ``self._db.query``），直接 ``self._store.query`` 会抛
        # AttributeError —— 此前被外层 ``except`` 静默吞掉，导致 ``cold_complete`` 永远
        # 读不到、冷补齐标记形同虚设、每周冷调度反复重拉已补齐的标的。
        try:
            rows = self._store._db.query(
                "SELECT key FROM local_sync_meta WHERE key LIKE ?",
                (f"cold_complete:{self._period}:{self._adjust}:%",))
        except Exception:  # noqa: BLE001 读不到 = 全部视为未补齐
            return set()
        out: set = set()
        for r in rows:
            parts = str(r.get("key") or "").split(":")
            if len(parts) == 4 and parts[1] == self._period \
                    and parts[2] == self._adjust:
                out.add(parts[3])
        return out

    def _mark_cold_complete(self, code: str) -> None:
        """冷历史补齐后置标记：后续日常同步直接跳过，不再重拉。"""
        try:
            self._store.set_meta(
                f"cold_complete:{self._period}:{self._adjust}:{code}", "1")
        except Exception as exc:  # noqa: BLE001 标记失败不影响数据写入
            log.debug("冷补齐标记失败（忽略）：%s", exc)

    def _normalize_for_cold(self, code: str, bars: list) -> list:
        """把抓取结果归一成冷仓行 ``(code,period,dt,o,h,l,c,v,a,fetched_at,adjust)``。

        与 :meth:`gateway.kline_cache.KlineCache.put` 的归档行**逐列一致**
        （同一张 ``kline_archive`` 表），保证冷数据既能被图表/回测经
        ``KlineCache.get`` 透明读到，也不会与缓存层写入错位。
        """
        rows: list = []
        now = time.time()
        for b in bars or []:
            # ★ dt 必须归一成 ``YYYYMMDD``（core.clock.bar_date 是唯一入口）。
            #   源链三形状混入：QMT/券商给 ``"20260105"``、腾讯在线源给
            #   ``"2026-01-05"``、东财给 ``datetime`` 或 ``"2026/01/05"``。
            #   若原样透传，冷仓 ``kline_archive.dt`` 混存两种格式：
            #     - ``MIN(dt)`` 取到字典序最小值而非真实最早日（``'-'`` 0x2D
            #       小于 ``'0'`` 0x30），断点续传的 ``_covered`` 判定失效；
            #     - 同一天跨源产出两行，对账认为「两个交易日」；
            #     - ``WHERE dt BETWEEN start AND end`` 对另一种格式整体失效；
            #     - ``KlineCache.get`` 合并热 + 冷时同日重复且顺序错乱。
            #   热路径（bars.py）入库前已归一，冷路径必须一致。
            #   解析不出合法日期的行**丢弃**（不兜底为今天、不抛异常）。
            dt = bar_date(_bar_time(b))
            if not dt:
                continue
            vals: list = []
            for f in ("open", "high", "low", "close", "volume", "amount"):
                v = b.get(f) if isinstance(b, dict) else getattr(b, f, None)
                try:
                    vals.append(None if v is None else float(v))
                except (TypeError, ValueError):
                    vals.append(None)
            rows.append((code, self._period, dt, *vals, now, self._adjust or ""))
        return rows

    def _write_cold(self, rows: list) -> int:
        """写冷仓（``bars_cold.db`` 的 ``kline_archive``）。冷仓不可用 ⇒ 返回 0。"""
        if self._cold is None:
            return 0
        return self._cold.bulk_upsert_bars(rows)

    async def _sync_one_cold(self, code: str) -> SyncOutcome:
        """补齐单标的的**冷历史**（热窗口之外、落 ``bars_cold.db`` 的部分）。

        - 源不支持区间取数（纯在线源只给最近 N 根）⇒ 冷历史无法补齐，
          **不标记** complete（等券商接入后重试），且 ``ok=False`` 让界面知道
          「历史还没齐」，而不是谎报完成。
        - 取到空（退市股无历史）⇒ 标记 complete 避免反复重试空回补。
        - 正常 ⇒ 写冷仓 + 标记 complete，返回 ``cold_written``。
        """
        async with self._sem:
            # ★ 冷回源**同样占一个并发槽**：本方法不再由 ``_sync_one_zones``
            #   代持槽（那层已改成纯编排，见其历史教训注释）。冷区间取数比热
            #   窗口更重，若不加闸门，zone=cold/both 时 gather 里几千只标的会
            #   同时打券商区间接口 —— 无界并发。（2026-10-13 R1）
            if self._cold is None:
                return SyncOutcome(
                    code=code, ok=False,
                    error="冷仓未初始化：冷数据跳过（不阻断热同步）")
            bars0, _src0, paged = await self._fetch_full(code)
            if not paged:
                return SyncOutcome(
                    code=code, ok=False, paged=False,
                    error="冷数据回补未生效：当前源不支持按区间取数")
            if not bars0:
                self._mark_cold_complete(code)
                return SyncOutcome(code=code, ok=True, cold_written=0)
            rows = self._normalize_for_cold(code, bars0)
            n = await asyncio.to_thread(self._write_cold, rows)
            self._mark_cold_complete(code)
            return SyncOutcome(
                code=code, ok=True, cold_written=n,
                as_of=bars_last_date(bars0), as_of_min=_bars_first_date(bars0),
                paged=True)

