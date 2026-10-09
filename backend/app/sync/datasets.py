"""R28 通用数据集同步器（多数据类型 × 多数据源 × 统一断点/并发/溯源）。

## 它解决什么

R28 之前，「定时下载数据」= ``app/sync/bars.py::BarsSyncer`` 拉 **1d 日线**，仅此一种。
分钟线 / 逐笔 / 财务 / 板块成分 / 股本 / 资金流**不是源没能力**——``tdx`` 源声明了
15 项能力、``broker`` 有财务与板块接口——而是**没有编排层**把它们变成可调度任务。

本模块在 :mod:`datasource.datasets` 的数据集声明之上提供**唯一的下载执行体**：

- **K 线类**（日/周/月/分钟）**直接委托** ``BarsSyncer``——它已经参数化了
  ``period``，且断点续传/并发闸门/进度回调/真实溯源都写好了。这里只负责按
  ``spec.store`` 把落库目标路由到主库或分钟线独立仓，**绝不写第二套断点逻辑**
  （两套游标迟早漂移，TD-33 的教训）。
- **参考数据 / 财务 / 逐笔 / 分时** 由本模块实现，但**复用同一套并发闸门与
  汇总契约**，让调用方（JobRuntime / REST）无需分数据类型写代码。

## 三条不可妥协的约定

1. **零 mock**：所有取数走 ``get_hub()`` 真实调用。测试注入的是 *fetch 函数*，
   不是假数据——注入的目的是隔离网络，不是伪造成功。

2. **源路由交给既有能力链**：本模块**不自己挑源**。``spec.chain`` 只是声明
   （「券商优先」的意图），真正选中谁由 ``ProviderCatalog.resolve_chain`` 在运行时
   按 能力校验 → 依赖可用 → 商用许可 → 熔断状态 决定。所以「QMT 开着走券商、
   没开降级 tdx」是既有机制的**自然结果**，不是本模块的分支。

3. **溯源拿不到就记空，绝不伪造**：``get_minutes`` / ``get_stock_list`` 这两个
   registry 方法**不返回**来源名（历史设计如此）。本模块对此记 ``provider_id=""``
   并在汇总里置 ``source_unknown=True``——宁可暴露「来源未知」，也不写一个编造的
   源名让对账失真（P3-4 定案）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

from core.clock import bar_date, local_now, now_iso
from datasource import datasets as DS
from datasource.local_store import LocalStore, get_store
from datasource.registry import get_hub

log = logging.getLogger("qmt_work.sync.datasets")

#: 默认并发。参考数据整批刷新不需要高并发（源多为单次批量接口），
#: 而逐笔/分时是逐标的请求，需要并发但也要给源留余地。
DEFAULT_CONCURRENCY = 8

#: 单次同步的标的上限（0 = 不限）。用于「先跑 20 只看看通不通」的手动验证，
#: 定时调度场景应为 0（全市场）。
DEFAULT_LIMIT = 0


@dataclass
class DatasetSyncSummary:
    """一次数据集同步的结果。**失败必须看得见**。"""

    dataset: str
    label: str = ""
    ok: bool = True
    mode: str = "incremental"
    requested: int = 0        # 计划同步的标的数（参考数据类为 1 批）
    written: int = 0          # 实际落库行数
    skipped: int = 0          # 命中「已够新/已补齐」而跳过的
    failed: int = 0           # 取数失败或源不可用的
    source: str = ""          # 请求的源（"auto" = 按能力链）
    sources_used: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    source_unknown: bool = False
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "dataset": self.dataset, "label": self.label, "ok": self.ok,
            "mode": self.mode, "requested": self.requested, "written": self.written,
            "skipped": self.skipped, "failed": self.failed, "source": self.source,
            "sources_used": dict(self.sources_used),
            "problems": list(self.problems),
            "source_unknown": self.source_unknown,
            "dry_run": self.dry_run,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 3),
            "detail": dict(self.detail),
        }
        return d


#: 取数函数签名：``(code) -> (rows, source_name)`` 或裸 rows。
FetchRows = Callable[[str], Awaitable[Any]]


class _DryRunBarsStore:
    """K 线数据集 dry_run 代理：**只数不写**。

    ``BarsSyncer.sync_one`` 是「取数 → 立即落库」，没有 dry_run 开关。直接把
    真实 store 交给它会让 ``dry_run=True`` **偷偷改库**，违反接口契约
    （``POST /datasets/{id}/sync {dry_run: true}`` 必须是只读的）。

    代理只把 :meth:`upsert_bars` 换成计数；游标读（``latest_dt_map`` /
    ``earliest_dt_map``）**仍走真库** —— 否则「这次会写多少」会被算成
    「从零开始写多少」，dry run 的报告就失去了意义。故意不实现 ``set_meta``
    与 ``provider_counts``：前者 BarsSyncer 已按 ``hasattr`` 容错跳过，
    后者让本模块的溯源统计在 dry run 下自动跳过（库里没有这批数据可数）。
    """

    def __init__(self, real: Any) -> None:
        self._real = real

    def upsert_bars(self, code: str, bars: Any, **_: Any) -> int:
        """返回行数 = 「如果真跑会写多少」，不写任何字节。"""
        return len(list(bars or []))

    def latest_dt_map(self, codes: Sequence[str], period: str = "1d",
                      adjust: str = "") -> dict:
        return self._real.latest_dt_map(codes, period, adjust)

    def earliest_dt_map(self, codes: Sequence[str], period: str = "1d",
                        adjust: str = "") -> dict:
        return self._real.earliest_dt_map(codes, period, adjust)


class DatasetSyncer:
    """按数据集声明执行同步。**唯一**入口是 :meth:`sync`。"""

    def __init__(
        self,
        dataset_id: str,
        *,
        mode: str = "incremental",
        limit: int = DEFAULT_LIMIT,
        concurrency: int = DEFAULT_CONCURRENCY,
        source: str = "auto",
        conn_id: Optional[str] = None,
        dry_run: bool = False,
        codes: Optional[Sequence[str]] = None,
        intraday_store: Any = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
        fetcher: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        # ``fetcher`` 是**注入点**（与 ``BarsSyncer.fetch_bars`` 同构）：测试用它
        # 隔离网络。注入的是**取数函数**而不是假数据——返回什么仍由被测逻辑处理，
        # 落库、游标、溯源、汇总全走真实代码路径。
        self.spec = DS.require(dataset_id)
        self._mode = "full" if str(mode).strip().lower() == "full" else "incremental"
        self._limit = int(limit or 0)
        self._concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
        self._source = (source or "auto").strip() or "auto"
        self._conn_id = conn_id
        self._dry_run = bool(dry_run)
        self._codes = list(codes) if codes else None
        self._intraday = intraday_store
        self._progress_cb = progress_cb
        self._fetcher = fetcher
        self._store: LocalStore = get_store()
        self._sem = asyncio.Semaphore(self._concurrency)
        self._sources: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def sync(self) -> DatasetSyncSummary:
        """执行一次同步。**不抛**——任何异常都转成 ``problems`` + ``ok=False``。

        ★ 这是刻意的：调度器（JobRuntime）需要「跑完了但失败」与「跑挂了」区分开，
        抛异常会让 job 落到 unknown 态，界面上看不出是数据源问题还是代码问题。
        """
        s = DatasetSyncSummary(
            dataset=self.spec.id, label=self.spec.label, mode=self._mode,
            source=self._source, dry_run=self._dry_run, started_at=now_iso())
        t0 = time.monotonic()
        try:
            if self.spec.cursor == DS.CUR_BAR_DATE:
                await self._sync_bars(s)
            elif self.spec.cursor == DS.CUR_SNAPSHOT:
                await self._sync_snapshot(s)
            elif self.spec.cursor == DS.CUR_EVENT:
                await self._sync_event(s)
            elif self.spec.cursor == DS.CUR_REPORT:
                await self._sync_report(s)
            else:
                s.ok = False
                s.problems.append(f"未实现的游标语义：{self.spec.cursor}")
        except Exception as exc:  # noqa: BLE001 兜底：调度场景不能让异常逃逸
            log.exception("数据集 %s 同步异常", self.spec.id)
            s.ok = False
            s.problems.append(f"同步异常：{exc}")
        s.duration_s = time.monotonic() - t0
        s.finished_at = now_iso()
        s.sources_used = dict(self._sources)
        s.ok = s.ok and not s.problems
        # ★ 只有**真的写入了数据**才记录「上次同步时间」，而且集中在唯一入口：
        #   早期只有 snapshot 分支写这个 key，导致 14 个 K 线/逐笔/财务数据集的
        #   「上次同步」永远为空，前端一直显示「从未同步」——数据明明已经下载了。
        if s.ok and s.written and not self._dry_run:
            try:
                self._store.set_meta(f"dataset.{self.spec.id}.last_sync_at",
                                     s.finished_at)
            except Exception:  # noqa: BLE001 时间戳记录失败不影响同步结果
                log.debug("记录 %s 的 last_sync_at 失败", self.spec.id)
        return s

    # ------------------------------------------------------------------
    # ① K 线（日/周/月/分钟）—— 委托 BarsSyncer，绝不重写断点逻辑
    # ------------------------------------------------------------------
    async def _sync_bars(self, s: DatasetSyncSummary) -> None:
        from app.sync.bars import BarsSyncer  # 局部导入：避免与 bars 模块循环依赖

        # dry_run 走只数不写的代理（否则 BarsSyncer 会真实落库）。
        real_store = self._bar_store()
        store = _DryRunBarsStore(real_store) if self._dry_run else real_store
        syncer = BarsSyncer(
            store=store,
            concurrency=self._concurrency,
            lookback=self.spec.lookback,
            period=self.spec.period,
            adjust=self.spec.adjust,
            provider_id=self._source,
            mode=self._mode,
            skip_fresh=(self._mode != "full"),
        )
        codes = await self._resolve_codes()
        if self._limit > 0:
            codes = codes[: self._limit]
        s.requested = len(codes)
        if not codes:
            s.ok = False
            s.problems.append("标的池为空（证券列表不可用且未传入 codes）")
            return

        def _progress(done: int, total: int, code: str = "") -> None:
            # ★ BarsSyncer.sync_many 的契约是 ``cb(done, total, code)`` 三参。
            #   只声明两参会就地 TypeError，而 BarsSyncer 的 ``_tracked`` 在
            #   sync_one 已经落库成功**之后**才回调 —— 异常会让已同步的结果被
            #   ``return_exceptions`` 吃掉并按「任务级失败」计入，表现为
            #   「数据写进去了但汇总说全部失败」（假失败，与假绿灯同族）。
            if self._progress_cb:
                self._progress_cb({"done": done, "total": total, "code": code,
                                   "dataset": self.spec.id})

        summary = await syncer.sync_many(codes, progress_cb=_progress)
        # ★ 字段名必须精确：BarsSyncer 的是 ``bars_written``，本类是 ``written``。
        #   曾写成 ``getattr(summary, "written", 0)`` ⇒ 永远取到 0，K 线数据集
        #   同步了上万根却汇报「写入 0 行」，而 runner 的「一只都没同步就报错」
        #   护栏因此永远失效。字段漂移与签名漂移一样，是最阴的断链。
        s.written = int(getattr(summary, "bars_written", 0) or 0)
        s.skipped = int(getattr(summary, "skipped_fresh", 0) or 0) + \
            int(getattr(summary, "skipped_complete", 0) or 0)
        s.failed = int(getattr(summary, "failed", 0) or 0)
        s.detail = {
            "period": self.spec.period,
            "adjust": self.spec.adjust,
            "store": self.spec.store,
            "codes_ok": int(getattr(summary, "ok", 0) or 0),
            "paged": bool(getattr(summary, "paged", False)),
            "stale": int(getattr(summary, "stale", 0) or 0),
            "as_of_max": str(getattr(summary, "as_of_max", "") or ""),
            "as_of_min": str(getattr(summary, "as_of_min", "") or ""),
            "batch_id": str(getattr(summary, "batch_id", "") or ""),
        }
        self._tally_bars_sources(codes, summary)
        if s.written == 0 and s.failed:
            s.problems.append(
                f"全部 {s.failed} 只取数失败（源={self._source}，链={list(self.spec.chain)}）")
        elif not self._dry_run and s.written == 0 and not s.failed and not s.skipped:
            # 取数成功但一根没写：通常是时间字段无法解析（源格式漂移）。
            # 不说明这一点，界面上就是一句看不懂的「已完成 0 行」。
            s.problems.append(
                f"{len(codes)} 只标的均取到数据但未写入任何 K 线"
                f"（源返回的时间字段无法解析，period={self.spec.period}）")
        if self._dry_run:
            s.detail["dry_run_note"] = "dry_run：只计数未写入，也未执行保留清理"
        self._apply_retention(s)

    def _tally_bars_sources(self, codes: Sequence[str], summary: Any) -> None:
        """K 线的真实来源分布：从库里读 ``provider_id``，不拿 ``auto`` 冒充。

        BarsSyncer 不向上汇总来源名（它是逐标的写进落库列的），所以只能回到
        库里数。必须按 ``batch_id`` 限定本次批次——否则「本次同步的来源分布」
        会被统计成「库里全部历史」，那是另一种假数字。取不到就保持
        ``sources_used`` 为空：空比编造一个源名诚实。
        """
        store = self._bar_store()
        if not hasattr(store, "provider_counts"):
            return
        batch = str(getattr(summary, "batch_id", "") or "")
        try:
            counts = store.provider_counts(list(codes or []), self.spec.period,
                                           self.spec.adjust, batch_id=batch)
        except Exception:  # noqa: BLE001 溯源统计失败不影响同步结果
            log.debug("K 线溯源统计失败（不影响同步结果）")
            return
        for src, n in (counts or {}).items():
            self._tally(src, int(n))

    def _bar_store(self):
        """按 ``spec.store`` 路由落库目标：主库日线仓 vs 分钟线独立仓。"""
        if self.spec.store == DS.STORE_INTRADAY:
            if self._intraday is not None:
                return self._intraday
            from datasource.intraday_store import get_intraday_store
            return get_intraday_store()
        return self._store

    def _apply_retention(self, s: DatasetSyncSummary) -> None:
        """分钟线：按 ``retention_days`` 滚动清理。**不清理** = 磁盘必然撑爆。"""
        days = int(self.spec.retention_days or 0)
        if days <= 0 or self.spec.store != DS.STORE_INTRADAY:
            return
        # ★ dry_run 绝不删除数据：清理是不可逆的，「预览一次同步」不能变成
        #   「顺手清掉了两周分钟线」。
        if self._dry_run:
            s.detail["retention_note"] = f"dry_run：跳过保留清理（窗口 {days} 天）"
            return
        from datetime import timedelta
        cut = (local_now().date() - timedelta(days=days)).strftime("%Y%m%d")
        try:
            n = self._bar_store().prune_before(cut, self.spec.period)
        except Exception as exc:  # noqa: BLE001 清理失败不该让同步判失败
            s.problems.append(f"保留清理失败（不影响本次写入）：{exc}")
            return
        s.detail["pruned"] = n
        s.detail["retention_cutoff"] = cut

    # ------------------------------------------------------------------
    # ② 整批快照（证券列表 / 板块 / 股本 / ETF / 日历）
    # ------------------------------------------------------------------
    async def _sync_snapshot(self, s: DatasetSyncSummary) -> None:
        """整批覆盖刷新。

        ★ **为什么不做增量**：这类数据没有时间序列概念，源给的就是「当下全量」。
        对它做「增量」是伪需求——做出来只能靠「上次同步时间」猜要不要重跑，
        而猜错的代价是板块成分悄悄过期（新股进了板块却查不到）。
        所以这里的语义是：**跑一次 = 拉全量覆盖**，是否值得跑由调用方按 cron 决定。
        """
        s.requested = 1
        fetch = self._snapshot_fetcher()
        if fetch is None:
            s.ok = False
            s.problems.append(f"数据集 {self.spec.id} 未实现取数逻辑")
            return
        try:
            raw = await fetch()
        except Exception as exc:  # noqa: BLE001
            s.ok = False
            s.problems.append(f"取数失败：{exc}")
            return
        rows, src = self._split(raw)
        if not rows:
            s.ok = False
            s.problems.append(
                f"源返回空（源={self._source}，链={list(self.spec.chain)}）；"
                "请确认数据源可用性（GET /api/v1/data/providers/health）")
            return
        self._tally(src, len(rows))
        if self._dry_run:
            s.written = len(rows)
            s.detail["dry_run_rows"] = len(rows)
            return
        try:
            n = await asyncio.to_thread(self._write_snapshot, rows)
        except Exception as exc:  # noqa: BLE001
            s.ok = False
            s.problems.append(f"落库失败：{exc}")
            return
        s.written = n
        # last_sync_at 统一由 :meth:`sync` 记录（只有真的写入才记）

    # ------------------------------------------------------------------
    # ③ 按交易日切片（逐笔 / 分时 / 资金流）
    # ------------------------------------------------------------------
    async def _sync_event(self, s: DatasetSyncSummary) -> None:
        """按标的 × 交易日切片同步。``lookback`` 在此语义下是「每只取多少条」。

        ★ 与 K 线不同：这类数据**按天整体过期**（今天的分时明天就没用了），
        所以保留窗口默认很短（7 天），且**没有「增量跳过」**——同一天重跑就是覆盖。
        """
        codes = await self._resolve_codes()
        if self._limit > 0:
            codes = codes[: self._limit]
        s.requested = len(codes)
        if not codes:
            s.ok = False
            s.problems.append("标的池为空（证券列表不可用且未传入 codes）")
            return
        fetch = self._event_fetcher()
        if fetch is None:
            s.ok = False
            s.problems.append(f"数据集 {self.spec.id} 未实现取数逻辑")
            return

        today_d = local_now().date()
        today = today_d.strftime("%Y%m%d")
        # ★ 非交易日**整体跳过**而不是跑完全场再报「全部为空」。逐笔/分时/资金流
        #   按天整体过期，周末跑一遍只会拿到上个交易日的数据再当成今天 —— 那正是
        #   「假成功」。周末/节假日明确记录跳过原因，界面看到的是「未同步」而不是
        #   「已完成」。
        try:
            from app.sync.calendar import is_trading_day
            trading = is_trading_day(today_d)
        except Exception:  # noqa: BLE001 日历不可用时不因此拦住同步
            trading = True
        if not trading:
            s.skipped = len(codes)
            s.detail["slice_date"] = today
            s.detail["note"] = f"{today} 非交易日，未同步（本数据集按天整体过期）"
            return

        done = [0]

        async def _one(code: str) -> tuple[str, int]:
            async with self._sem:
                try:
                    raw = await fetch(code)
                except Exception:  # noqa: BLE001 单标的失败不击穿整批
                    return "failed", 0
                got = self._norm_event(raw, today)
                if got is None:
                    # 源不可用 —— 真失败
                    return "failed", 0
                dt, rows, src = got
                if not rows:
                    # 源可用但当下无数据（盘前 / 该标的今日无成交）—— 跳过，不是成功
                    return "skipped", 0
                self._tally(src, len(rows))
                if self._dry_run:
                    return "written", len(rows)
                try:
                    n = await asyncio.to_thread(self._write_event, code, dt, rows)
                except Exception:  # noqa: BLE001
                    return "failed", 0
                return ("written" if n > 0 else "skipped"), int(n)

        results = await asyncio.gather(*(_one(c) for c in codes),
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                s.failed += 1
            else:
                state, n = r
                if state == "failed":
                    s.failed += 1
                elif state == "skipped":
                    s.skipped += 1
                else:
                    s.written += int(n)
            done[0] += 1
            if self._progress_cb and done[0] % 20 == 0:
                self._progress_cb({"done": done[0], "total": len(codes),
                                   "dataset": self.spec.id})
        if s.written == 0 and s.failed:
            s.problems.append(
                f"全部 {s.failed} 只取数失败（源={self._source}，链={list(self.spec.chain)}）")
        elif s.written == 0 and s.skipped and not s.failed:
            # 交易日内整批空 —— 不是「已完成」。可能是源故障，也可能是盘前，
            # 两种都说清楚，让用户自己判断，而不是给一个绿灯。
            s.problems.append(
                f"{s.skipped} 只标的均返回空数据（源可用但无内容；"
                f"盘前或非交易时段属正常，否则请检查数据源"
                f"（源={self._source}，链={list(self.spec.chain)}））")
        s.detail["slice_date"] = today
        self._prune_event(s)

    def _prune_event(self, s: DatasetSyncSummary) -> None:
        days = int(self.spec.retention_days or 0)
        if days <= 0:
            return
        if self._dry_run:
            s.detail["retention_note"] = f"dry_run：跳过保留清理（窗口 {days} 天）"
            return
        from datetime import timedelta
        cut_s = (local_now().date() - timedelta(days=days)).strftime("%Y%m%d")
        try:
            n = self._store.prune_before(self.spec.store, cut_s)
        except Exception as exc:  # noqa: BLE001
            s.problems.append(f"保留清理失败（不影响本次写入）：{exc}")
            return
        s.detail["pruned"] = n
        s.detail["retention_cutoff"] = cut_s

    # ------------------------------------------------------------------
    # ④ 财务（按报告期）
    # ------------------------------------------------------------------
    async def _sync_report(self, s: DatasetSyncSummary) -> None:
        """财务数据：**只有券商源提供**（``fundamental`` 链当前只有 ``broker``）。

        未连券商时**明确失败并说明原因**——不做「拿空当成功」的粉饰，
        也不伪造一份财务数据。这是契约要求（未连券商 → 明说，不包 code=0）。
        """
        codes = await self._resolve_codes()
        if self._limit > 0:
            codes = codes[: self._limit]
        s.requested = len(codes)
        if not codes:
            s.ok = False
            s.problems.append("标的池为空（证券列表不可用且未传入 codes）")
            return
        hub = get_hub()
        bridge = hub.active_bridge() if hasattr(hub, "active_bridge") else None
        if bridge is None:
            s.ok = False
            s.problems.append(
                "财务数据仅券商源提供，当前无活跃券商连接。"
                "请先连接 QMT 客户端（或改用其他数据集）。")
            return

        async def _one(code: str) -> int:
            async with self._sem:
                try:
                    raw = await bridge.get_financial(code)
                except Exception:  # noqa: BLE001
                    return -1
                if not raw:
                    return 0
                self._tally("broker", 1)
                if self._dry_run:
                    return 1
                try:
                    return await asyncio.to_thread(
                        self._store.upsert_fundamentals, code, raw, "broker")
                except Exception:  # noqa: BLE001
                    return -1

        results = await asyncio.gather(*(_one(c) for c in codes),
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException) or r is None or r < 0:
                s.failed += 1
            else:
                s.written += int(r)
        if s.written == 0 and s.failed:
            s.problems.append(f"全部 {s.failed} 只财务取数失败")

    # ------------------------------------------------------------------
    # 取数器分派
    # ------------------------------------------------------------------
    def _snapshot_fetcher(self) -> Optional[Callable[[], Awaitable[Any]]]:
        if self._fetcher is not None:
            return self._fetcher
        hub = get_hub()
        sid = self.spec.id
        if sid == "stock_list":
            # ★ 该方法不返回来源名 ⇒ 溯源记空（见模块 docstring 第 3 条）
            return lambda: hub.get_stock_list(source=self._source)
        if sid == "etf_list":
            return lambda: hub.get_etf_list(0, source=self._source)
        if sid == "boards":
            return lambda: hub.get_boards("industry", "pct", 0, source=self._source)
        if sid == "board_members":
            return self._fetch_board_members
        if sid == "capital":
            return self._fetch_capital
        if sid == "calendar":
            return self._fetch_calendar
        return None

    async def _fetch_board_members(self):
        """板块成分：先取板块列表，再逐个拉成分（板块数有限，串行即可）。"""
        hub = get_hub()
        boards, _ = await hub.get_boards("industry", "pct", 0, source=self._source)
        if not boards:
            return []
        out: list[dict] = []
        src = ""
        for b in boards[:200]:
            code = b.get("code") if isinstance(b, dict) else getattr(b, "code", "")
            if not code:
                continue
            try:
                res, s = await hub.get_board_constituents(
                    code, 500, 0, source=self._source)
            except Exception:  # noqa: BLE001 单板块失败不击穿整批
                continue
            if not res:
                continue
            src = s or src
            name = (b.get("name") if isinstance(b, dict)
                    else getattr(b, "name", "")) or ""
            for it in (res.get("items") or []):
                c = it.get("code") if isinstance(it, dict) else getattr(it, "code", "")
                if c:
                    out.append({"board_code": code, "board_name": name, "code": c,
                                "name": (it.get("name") if isinstance(it, dict)
                                         else getattr(it, "name", "")) or "",
                                "weight": (it.get("weight") if isinstance(it, dict)
                                           else getattr(it, "weight", None))})
        if not src:
            return out
        return out, src

    async def _fetch_capital(self):
        codes = await self._resolve_codes()
        hub = get_hub()
        res, src = await hub.get_share_capital(list(codes), source=self._source)
        if not res:
            return []
        rows = []
        for c, v in res.items():
            if isinstance(v, dict):
                rows.append({"code": c, **v})
            else:
                rows.append({"code": c, "float_shares": v})
        return rows, src

    async def _fetch_calendar(self):
        """交易日历：券商可用则用券商，否则用本地内置日历。

        本地日历是**内置事实**（不依赖网络），所以这里永远不会返回空。
        """
        hub = get_hub()
        bridge = hub.active_bridge() if hasattr(hub, "active_bridge") else None
        if bridge is not None:
            try:
                days = await bridge.get_trading_calendar()
                if days:
                    return [{"date": bar_date(d), "source": "broker"}
                            for d in days if bar_date(d)], "broker"
            except Exception as exc:  # noqa: BLE001 券商日历失败则回退本地
                from core.errors import swallow
                swallow(exc, why="券商交易日历不可用；回退本地内置日历（覆盖年内精确）",
                        logger=log)
        # 本地日历是内置事实（不依赖网络），取最近 365 个交易日即可覆盖一年。
        from app.sync.calendar import trading_calendar
        days = trading_calendar(local_now().date(), 365)
        return [{"date": bar_date(d), "source": "local"} for d in days], "local"

    def _event_fetcher(self) -> Optional[FetchRows]:
        if self._fetcher is not None:
            return self._fetcher
        sid = self.spec.id
        if sid == "ticks":
            return self._fetch_ticks
        if sid == "minutes":
            return self._fetch_minutes
        if sid == "moneyflow":
            return self._fetch_moneyflow
        return None

    # ------------------------------------------------------------------
    # 逐笔 / 分时 / 资金流：源返回的是**复合 dict**，不是裸行列表
    # ------------------------------------------------------------------
    # ★ 这里踩过一次：``get_ticks`` 返回 ``{code, items, trading_date, source}``、
    #   ``get_minutes`` 返回 ``{code, trading_date, points}``，而旧的 ``_split``
    #   把 dict 当成「一整行」塞进落库函数 —— 结果是逐笔/分时**同步报成功但库里
    #   全是一行垃圾**。复合结构必须**先拆包再落库**，且交易日要取源给的
    #   ``trading_date``：休市时 TDX 返回的是**上一个交易日**的数据，按「今天」
    #   存会把昨天的成交冒充成今天的（与 TradingDateBadge 同一类诚实性要求）。
    async def _fetch_ticks(self, code: str):
        """→ ``(dt, rows, src)``；源全部不可用返回 ``None``（与「空列表」语义不同）。"""
        hub = get_hub()
        res = await hub.get_ticks(code, self.spec.lookback, source=self._source)
        if res is None:
            return None
        rows = []
        for it in (res.get("items") or []):
            d = it if isinstance(it, dict) else {}
            rows.append({
                # 源键是 ``time`` / ``side``，落库列是 ``tm`` / ``bs_flag``
                "tm": str(d.get("time") or d.get("tm") or ""),
                "price": d.get("price"),
                "volume": d.get("volume"),
                "amount": d.get("amount"),
                "bs_flag": str(d.get("side") or d.get("bs_flag") or ""),
            })
        return (bar_date(res.get("trading_date")) or "", rows,
                str(res.get("source") or ""))

    async def _fetch_minutes(self, code: str):
        """→ ``(dt, rows, src)``；源无数据返回 ``None``。"""
        hub = get_hub()
        res = await hub.get_minutes(code, None, source=self._source)
        if res is None:
            return None
        rows = []
        for p in (res.get("points") or []):
            d = p if isinstance(p, dict) else {}
            avg = d.get("avg")
            if avg is None:
                avg = d.get("avg_price")
            rows.append({
                # 源键是 ``t`` / ``avg``，落库列是 ``tm`` / ``avg_price``
                "tm": str(d.get("t") or d.get("tm") or d.get("time") or ""),
                "price": d.get("price"),
                "avg_price": avg,
                "volume": d.get("volume"),
                "amount": d.get("amount"),
            })
        return (bar_date(res.get("trading_date")) or "", rows,
                str(res.get("source") or ""))

    async def _fetch_moneyflow(self, code: str):
        """→ ``(dt, rows, src)``。快照型数据无交易日，``dt`` 留空由调用方取今天。"""
        hub = get_hub()
        raw, src = await hub.get_moneyflow(code, source=self._source)
        if raw is None:
            return None
        row = dict(raw) if isinstance(raw, dict) else {"value": raw}
        return ("", [row], str(src or ""))

    def _norm_event(self, raw: Any, today: str):
        """归一化逐笔/分时/资金流结果 → ``(dt, rows, src)``。

        ``None`` = **源不可用**（真失败）；``rows`` 空 = 源可用但当下无数据（跳过）。
        两者必须分开：把它们混为一谈就会回到「源故障却报 ok」的假绿灯。
        """
        if raw is None:
            return None
        if isinstance(raw, tuple) and len(raw) == 3:
            dt, rows, src = raw
            return (str(dt or "") or today, list(rows or []), str(src or ""))
        # 旧形态（注入的 fetcher 常给 ``(rows, src)`` 或裸 rows）
        rows, src = self._split(raw)
        return (today, rows, src)

    # ------------------------------------------------------------------
    # 落库分派
    # ------------------------------------------------------------------
    def _write_snapshot(self, rows: list) -> int:
        sid = self.spec.id
        if sid == "stock_list":
            return self._store.upsert_stock_list(rows)
        if sid == "etf_list":
            return self._store.upsert_stock_list(
                [{**r, "category": "ETF"} for r in rows])
        if sid == "boards":
            return self._store.upsert_boards("industry", rows)
        if sid == "board_members":
            return self._store.upsert_board_members(rows)
        if sid == "capital":
            return self._store.upsert_capital(rows)
        if sid == "calendar":
            return self._store.upsert_calendar(rows)
        return 0

    def _write_event(self, code: str, dt: str, rows: Any) -> int:
        sid = self.spec.id
        if sid == "ticks":
            return self._store.upsert_ticks(code, dt, rows, self.spec.retention_days)
        if sid == "minutes":
            # 拆包已在 ``_fetch_minutes`` 完成（源给的是 ``{"points": [...]}``）
            return self._store.upsert_minutes(code, dt, rows)
        if sid == "moneyflow":
            row = rows[0] if rows else {}
            return self._store.upsert_moneyflow_hist(code, dt, row)
        return 0

    # ------------------------------------------------------------------
    # 公共辅助
    # ------------------------------------------------------------------
    async def _resolve_codes(self) -> list[str]:
        """标的池：显式 codes > 本地证券列表 > 回源拉一次。

        ★ 拉不到就返回空并让调用方报错——**绝不**用内置假代码列表兜底
        （那样会让同步「看起来成功」但库里全是错的标的）。
        """
        if self._codes:
            return list(self._codes)
        try:
            local = self._store.get_stock_list()
            codes = [r.get("code") for r in local if r.get("code")]
            if codes:
                return codes
        except Exception:  # noqa: BLE001 本地仓不可用时回源
            log.debug("本地证券列表不可用，回源获取")
        try:
            lst = await get_hub().get_stock_list(source=self._source)
        except Exception:  # noqa: BLE001
            return []
        return [r.get("code") for r in (lst or []) if r.get("code")]

    def _split(self, raw: Any) -> tuple[list, str]:
        """归一化返回：``(rows, source)`` 元组 / 裸 rows / dict-of-code。"""
        if isinstance(raw, tuple):
            rows = raw[0] if raw else None
            src = raw[1] if len(raw) > 1 and raw[1] else ""
            return list(rows or []), str(src).strip()
        if isinstance(raw, dict):
            # get_moneyflow 返回单个 dict（非列表）
            return [raw], ""
        return list(raw or []), ""

    def _tally(self, src: str, n: int) -> None:
        """累计各源贡献量。**拿不到来源时记 ``""``** —— 空串会在汇总里体现为
        ``source_unknown``，而不是伪装成某个具体源。"""
        if not src:
            self._sources.setdefault("", 0)
            self._sources[""] += n
            return
        self._sources[src] = self._sources.get(src, 0) + n


# 注：曾在此导出便捷入口 ``sync_dataset(dataset_id, **kwargs)``
# （一行 ``DatasetSyncer(dataset_id, **kwargs).sync()``），但全仓库零调用——
# 路由与 JobRuntime 都直接实例化 ``DatasetSyncer``（它们需要拿到中间态与进度
# 回调，一行包装反而藏住了这些）。按「无孤儿逻辑」原则移除。

__all__ = ["DatasetSyncer", "DatasetSyncSummary", "DEFAULT_CONCURRENCY"]
