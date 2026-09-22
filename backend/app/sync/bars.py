"""G1-5 全市场日线增量同步（窗口式，BarsSyncer）。

设计决策：
- **窗口增量**：eltdx / 券商 K 线接口均按 ``count`` 取「最近 N 根」，无
  date-range 参数；因此采用「拉最新 lookback 根 → 主键幂等合并」的窗口增量，
  而非逐日对账。数据正确性由 ``local_bars`` 主键 (code,period,adjust,dt) 的
  ``INSERT OR REPLACE`` 保证：重复日期覆盖、缺失日期补齐、旧数据不删除。
- **复权**：默认 ``qfq``，按能力链 QMT→eltdx→baostock→akshare 求源（QMT 复权经
  ``dividend_type`` 参数化后同样参与复权链），复权维度入主键，前后复权各自独立存储。
- **溯源为真**：``provider_id`` 写入**本次真实命中来源名**（非 ``"auto"``），
  供跨源对账按质量序选主（QMT>eltdx>baostock>akshare）；拿不到来源时记空而非伪造。
- **并发闸门**：信号量限并发（默认 8），G6 JobRuntime 落地前先本地收敛。
- **交易日历**：``weekday_calendar`` 已升级为真实 A 股日历（app/sync/calendar.py，
  内置 2024-2027 节假日表 + runtime_config 扩展，G1-5b 落地）。

用法（手动 CLI）：
    python -m app.sync.bars --limit 20 --concurrency 4 --lookback 320
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable, List, Optional, Sequence

from core.clock import bar_date, local_now  # 唯一时钟在 core.clock（V11 R8 收敛）
from core.clock import now_iso  # noqa: F401  —— 唯一实现在 core.clock（V11 R8 收敛）
from datasource.local_store import LocalStore, get_store
from datasource.registry import bars_last_date, get_hub

log = logging.getLogger("qmt_work.sync.bars")

#: 日线「新鲜」的默认判定窗口（自然日）。A 股最长休市是春节（约 9~10 天），
#: 取 10 可覆盖长假而不误判；周末 3 天、清明/五一等短假 3~5 天均在窗口内。
STALE_DAYS_DEFAULT = 10

#: 抓取器签名：(code, period, adjust, count) -> Optional[list[dict|Bar]]
#: 生产路径返回 ``(bars, source)`` 元组以便落库写入**真实来源名**；注入式测试可返回纯 list。
FetchBars = Callable[[str, str, str, int], Awaitable[Optional[list]]]


def _split_fetch_result(raw) -> tuple[Optional[list], str]:
    """兼容两种抓取器返回：``(bars, source)`` 元组 / 纯 ``bars`` 列表。

    元组形态携带真实命中来源名，用于 ``local_bars.provider_id`` 溯源（P3-4：
    拒绝把真实来源丢成 ``"auto"`` 的假溯源）。
    """
    if isinstance(raw, tuple):
        bars = raw[0] if raw else None
        src = raw[1] if len(raw) > 1 and raw[1] else ""
        return bars, str(src).strip()
    return raw, ""


def _bar_time(b) -> object:
    """从 dict / 模型对象里取 K 线时间字段（两种形态都支持）。"""
    if isinstance(b, dict):
        return b.get("time")
    return getattr(b, "time", None)


def _bars_first_date(bars: Sequence) -> str:
    """这一批 K 线里**最早**的一根交易日（``""`` = 未知）。

    与 ``datasource.registry.bars_last_date`` 对称：全量回补只报「写入了 N 根」
    说明不了「历史推到了哪一年」，必须同时给出最早一根。
    取 **min** 而不是 ``bars[0]`` —— 不假设源的返回顺序。
    """
    ds = [d for d in (bar_date(_bar_time(b)) for b in bars or []) if d]
    return min(ds) if ds else ""


def _dedupe_bars(bars: Sequence) -> list:
    """按交易日去重并升序排列（逐年翻页的相邻年份不会重叠，但接口可能回边界日）。

    只做**内存内**去重：真正的幂等由 ``local_bars`` 主键
    (code, period, adjust, dt, provider_id) 保证（重复日期覆盖、旧数据不删）。
    这里去重的意义是少写一遍库，不是正确性来源。
    """
    seen: dict = {}
    for b in bars or []:
        key = bar_date(_bar_time(b)) or str(_bar_time(b) or "")
        if key:
            seen[key] = b
    return [seen[k] for k in sorted(seen)]


def _is_stale(as_of: str, stale_days: int = STALE_DAYS_DEFAULT) -> bool:
    """最后一根交易日是否陈旧（距今超过 ``stale_days`` 个自然日）。

    ``as_of`` 为空 / 无法解析时返回 **False** —— 不是「判它新鲜」，而是
    「无从判断」：这种行由 :meth:`SyncOutcome.as_of` 的空值单独暴露，
    不应被计进陈旧统计而淹没真正陈旧的标的。
    """
    d = bar_date(as_of)
    if not d:
        return False
    try:
        day = datetime.strptime(d, "%Y%m%d").date()
    except ValueError:
        return False
    return (local_now().date() - day).days > int(stale_days)


def _resolve_provider_id(real_src: str, configured: str) -> str:
    """落库溯源解析：**真实命中来源优先**。

    - 拿到真实来源名 → 直接采用（不因调用方传了别的标签而说谎）；
    - 未拿到来源但配置了具体源名 → 用配置值；
    - 配置为 ``auto``（按能力链自动降级）且无来源信息 → 记 ``""``（未知），
      **绝不伪造 ``"auto"``** —— 空值在 ``local_store`` 读路径中按最低优先级处理。
    """
    real = (real_src or "").strip()
    if real:
        return real
    cfg = (configured or "").strip()
    if cfg and cfg.lower() != "auto":
        return cfg
    return ""


def weekday_calendar(end: date, count: int) -> List[str]:
    """交易日历：最近 ``count`` 个交易日（升序 "YYYY-MM-DD"）。

    G1-5b：已由「周一至周五启发式」升级为真实 A 股节假日日历
    （app/sync/calendar.py：内置 2024-2027 法定节假日 + 调休补班，
    runtime_config `market.calendar.holidays/workdays` 可热扩展；
    超出覆盖年份回退工作日启发式并显式打日志）。函数名保留兼容既有调用。
    """
    from app.sync.calendar import trading_calendar
    return trading_calendar(end, count)


@dataclass
class SyncOutcome:
    """单标的同步结果。"""

    code: str
    ok: bool = False
    bars_written: int = 0
    error: str = ""
    #: 最后一根 K 线的交易日（``""`` = 未知）。**有数据不等于够新**，
    #: 调用方（任务报告 / 界面）必须同时看这个字段。
    as_of: str = ""
    #: 数据是否陈旧（最后一根距今超过 ``stale_days`` 个自然日）。
    stale: bool = False
    #: 本次是否**真的按日期区间向前翻页**（V11 §5.3 P0-3 III）。
    #: ``False`` 且 ``mode=full`` 表示没有支持区间的源、已退化为「单次大 count」——
    #: 必须在报告里说出来，否则用户会以为历史已经全部补齐。
    paged: bool = False
    #: 本次写入的 K 线里最早的一根（``""`` = 未知），用于判断回补到了哪一年。
    as_of_min: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "ok": self.ok,
                "bars_written": self.bars_written, "error": self.error,
                "as_of": self.as_of, "stale": self.stale}


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
    #: 成功但**数据陈旧**的标的数（V11 R13）。
    #: ★ 这是「假成功」的显影剂：``ok`` 很高、``stale`` 也很高 ⇒
    #: 写入了一堆历史数据却宣称完成，界面与报告必须把它摆到台面上。
    stale: int = 0
    #: 全批中最新的最后一根交易日（``""`` = 全部未知）。
    as_of_max: str = ""
    #: 全批中最**早**的一根交易日（``""`` = 全部未知）。全量回补用它回答
    #: 「这次到底把历史推到了哪一年」——只报写入根数说明不了这件事。
    as_of_min: str = ""
    #: ``incremental`` / ``full``（V11 §5.3 P0-3 III）。
    mode: str = "incremental"
    #: 本次是否**真的按日期区间向前翻页**取数（见 :attr:`SyncOutcome.paged`）。
    #: ``mode=full`` 而这里为 ``False`` ⇒ 已退化为单次大 count，**历史未真正补齐**。
    paged: bool = False
    #: 全量模式下因「本地历史已覆盖到目标起点」而**跳过**的标的数。
    #: 这就是断点续传的可见证据：中断后重跑，这个数会明显变大。
    skipped_complete: int = 0
    #: ★ 本批**真正写进** ``local_bars.batch_id`` 的批次号（``bars-<hex>``）。
    #:
    #: 为什么必须暴露出来：数据集快照（``dataset_snapshots``）要按批次回查
    #: ``local_bars`` 才能算出真实的 ``row_count`` / 覆盖区间。此前调用方只能
    #: 拿 ``finished``（ISO 时间戳）当批次号传进去，而落库用的是内部生成的
    #: ``bars-<hex>`` ⇒ 回查**永远命中 0 行**，于是每个快照都写着
    #: 「row_count=0、覆盖为空」却标着 ``complete``（实测 2026-09-20）。
    #: 批次号是「这批数据到底写了什么」的唯一钥匙，必须随汇总一起返回。
    batch_id: str = ""

    def to_dict(self) -> dict:
        return {
            "started": self.started, "finished": self.finished,
            "total": self.total, "ok": self.ok, "failed": self.failed,
            "bars_written": self.bars_written, "elapsed_ms": self.elapsed_ms,
            "errors": self.errors, "stale": self.stale,
            "as_of_max": self.as_of_max, "as_of_min": self.as_of_min,
            "mode": self.mode, "paged": self.paged,
            "skipped_complete": self.skipped_complete,
            "batch_id": self.batch_id,
        }


class BarsSyncer:
    """日线窗口同步器（并发闸门 + 幂等合并 + 结果汇总）。

    两种模式（V11 §5.3 P0-3 III）：

    - ``incremental``（默认）：只把**最近** ``lookback`` 根合并进库 —— 日常维护，
      要的是「够新」；
    - ``full``：把**历史一次性补齐** —— 按自然年从今年往前逐页取，直到某一年
      没有数据（早于上市日）。走的是券商渠道的日期区间能力
      （``DataSourceManager.get_kline_range``，唯一声明 ``supports_kline_range``
      的源），因此**不会**出现「假装翻页、其实一直拿最近 N 根」。

    ⚠️ 免费在线源只接受 ``count``（最近 N 根），**无法**指定日期区间；纯在线源
    环境下 ``full`` 会退化为单次大 ``count``，并把 ``paged=False`` 如实报出来。
    """

    #: 全量回补默认往前翻多少年（1990 年开市至今约 36 年；12 年已覆盖内置策略
    #: 与绝大多数回测需求，且不至于让单只耗时失控）。
    FULL_YEARS_DEFAULT = 12
    #: 退化为单次大 count 时取的根数（12 年 ≈ 2900 个交易日，留足余量）。
    FULL_COUNT_DEFAULT = 3600

    def __init__(
        self,
        store: Optional[LocalStore] = None,
        fetch_bars: Optional[FetchBars] = None,
        concurrency: int = 8,
        lookback: int = 320,
        period: str = "1d",
        adjust: str = "qfq",
        provider_id: str = "auto",
        batch_id: Optional[str] = None,
        stale_days: int = STALE_DAYS_DEFAULT,
        mode: str = "incremental",
        full_years: int = FULL_YEARS_DEFAULT,
        full_count: int = FULL_COUNT_DEFAULT,
    ):
        self._store = store or get_store()
        self._fetch = fetch_bars or self._default_fetch
        self._concurrency = max(1, int(concurrency))
        self._lookback = int(lookback)
        #: ``incremental`` / ``full``（非法值按 incremental 处理，绝不静默变全量）
        self._mode = "full" if str(mode).strip().lower() == "full" else "incremental"
        self._full_years = max(1, int(full_years or self.FULL_YEARS_DEFAULT))
        self._full_count = max(1, int(full_count or self.FULL_COUNT_DEFAULT))
        self._period = period
        self._adjust = adjust
        #: 新鲜度门槛：最后一根距今超过该自然日数即判「陈旧」。
        #: 0 / 负数 = 关闭门槛（恢复改造前「非空即算数」的行为）。
        self._stale_days = int(stale_days)
        # provider_id 既是「请求的数据源」（"auto" = 按能力链自动降级），
        # 也是拿到真实来源名之前的溯源兜底标签。
        self._provider_id = provider_id
        self._source = (provider_id or "auto").strip() or "auto"
        self._batch_id = batch_id or f"bars-{uuid.uuid4().hex}"
        self._sem = asyncio.Semaphore(self._concurrency)

    # ------------------------------------------------------------------
    # 抓取
    # ------------------------------------------------------------------
    async def _default_fetch(self, code: str, period: str, adjust: str,
                             count: int) -> Optional[tuple]:
        """生产路径：真实多源回退（按 ``self._source`` 求链，复权走支持复权的源）。

        返回 ``(bars, source_name)`` —— ``source_name`` 是本次真实命中的数据源，
        由 :meth:`sync_one` 写入 ``local_bars.provider_id``，保证溯源为真。
        """
        try:
            bars, src = await get_hub().get_kline(
                code, period, count, source=self._source, adjust=adjust,
                min_date=self._min_date())
        except Exception as exc:  # noqa: BLE001 单标的失败不击穿整批
            log.warning("sync fetch %s(%s) 失败：%s", code, self._source, exc)
            return None
        return bars, src

    def _min_date(self) -> str:
        """本次同步要求的**最低**最后一根交易日（``""`` = 不设门槛）。

        从唯一时钟取「今天」回推 ``stale_days`` 个自然日 —— 数据源链据此在
        「非空但陈旧」时继续降级，而不是停在第一个非空源上（V11 R13）。
        """
        if self._stale_days <= 0:
            return ""
        from core.clock import local_now
        return (local_now().date() - timedelta(days=self._stale_days)).strftime("%Y%m%d")

    # ------------------------------------------------------------------
    # 全量回补（V11 §5.3 P0-3 III）
    # ------------------------------------------------------------------
    async def _fetch_full(self, code: str) -> tuple[Optional[list], str, bool]:
        """按自然年**从今年往前逐页**取，直到某一年没有数据。

        返回 ``(bars, source, paged)``：

        - ``paged=True``：确实走了区间翻页，``bars`` 是多年合并去重后的全量；
        - ``paged=False``：**没有任何源支持日期区间**（纯在线源环境）⇒ 返回
          ``(None, "", False)``，由 :meth:`sync_one` 退化为单次大 count。
          ★ 绝不在这里假装成功：退化的结果必须在报告里看得见。

        为什么从**今年往前**翻而不是从最早往今年翻：新股上市日未知，往前翻可以
        「遇到空页就停」，页数最少；从最早翻则必然多翻十几年空页。
        """
        from core.clock import local_now
        today = local_now().date()
        merged: list = []
        src_name = ""
        paged = False
        year = today.year
        for _ in range(self._full_years):
            start = f"{year}0101"
            end = today.strftime("%Y%m%d") if year == today.year else f"{year}1231"
            try:
                bars, src = await get_hub().get_kline_range(
                    code, self._period, adjust=self._adjust,
                    start=start, end=end, source=self._source)
            except Exception as exc:  # noqa: BLE001 单页失败不击穿整只
                log.debug("全量回补 %s %s 区间取数失败：%s", code, year, exc)
                bars, src = None, None
            if bars is None and not paged:
                # 第一页就说明「没有源支持区间」⇒ 退化，交由调用方走大 count
                return None, "", False
            paged = True
            if not bars:
                # 该年无数据 = 早于上市日（或已到更早的边界）⇒ 停止向前翻页
                break
            merged.extend(bars)
            src_name = src or src_name
            year -= 1
        if not paged:
            return None, "", False
        return _dedupe_bars(merged), src_name, True

    def _target_start(self) -> str:
        """全量回补的目标起点（``YYYYMMDD``）：``full_years`` 年前的 1 月 1 日。"""
        from core.clock import local_now
        return f"{local_now().year - self._full_years + 1}0101"

    def _filter_backfilled(self, codes: Sequence[str]) -> tuple[list, int]:
        """筛掉「本地历史已覆盖到目标起点」的标的；返回 ``(待回补, 已跳过数)``。

        ★ 为什么用**数据**当游标，而不是「上次跑到第几个 code」的游标表：
        游标表只在**正常退出**时才被写对 —— 崩一次就白跑（下次从 0 重来）；
        而「库里最早一根是哪天」本身就是事实，**不需要额外状态，也不会与真实
        进度不一致**。中断后重跑，已补齐的自动跳过，这就是断点续传。

        判据是 ``earliest > target``（严格早于目标起点才算补齐）：库里最早一根
        正好等于 target 时也已达标，不必重跑。

        读本地最早日期失败（表不存在 / 库锁）时**全量重跑**并如实返回 ``0`` ——
        宁可多做功，不可漏补。跳过数少报比多报安全。
        """
        target = self._target_start()
        try:
            have = self._store.earliest_dt_map(list(codes), self._period, self._adjust)
        except Exception as exc:  # noqa: BLE001
            log.warning("全量回补读取本地最早日期失败（将全量重跑）：%s", exc)
            return list(codes), 0
        need = [c for c in codes
                if not have.get(c) or str(have[c]) > target]
        return need, len(codes) - len(need)

    # ------------------------------------------------------------------
    # 核心
    # ------------------------------------------------------------------
    async def sync_one(self, code: str) -> SyncOutcome:
        async with self._sem:
            paged = False
            try:
                if self._mode == "full":
                    bars0, src0, paged = await self._fetch_full(code)
                    raw = ((bars0, src0) if paged
                           else await self._fetch(code, self._period, self._adjust,
                                                  self._full_count))
                else:
                    raw = await self._fetch(code, self._period, self._adjust, self._lookback)
            except Exception as exc:  # noqa: BLE001
                return SyncOutcome(code=code, error=f"抓取异常：{exc}")
            bars, real_src = _split_fetch_result(raw)
            if not bars:
                return SyncOutcome(code=code, error="源无数据（非交易时段或代码不受支持）",
                                   paged=paged)
            # 溯源为真：写真实命中来源名，绝不用 "auto" 冒充（P3-4）
            provider_id = _resolve_provider_id(real_src, self._provider_id)
            # 新鲜度判定（V11 R13）：拿到数据 ≠ 数据够新。券商本地历史可能只到
            # 一年多前却照样非空，不标注就会让「同步完成」变成一句谎话。
            as_of = bars_last_date(bars)
            stale = self._stale_days > 0 and _is_stale(as_of, self._stale_days)
            if stale:
                log.info("sync %s 数据陈旧：as_of=%s（要求 >= %s）", code, as_of,
                         self._min_date())
            try:
                # 同步 sqlite 写必须移出事件循环：EOD 全市场同步（实测 7175 只）期间
                # 逐只在事件循环里落库，会持续阻塞**所有** HTTP 请求——实测
                # /trade/positions 由 0.019s 恶化到 6.59s、纯内存的 /capabilities
                # 由 0.032s 恶化到 2.41s（取消 EOD 任务后立刻全部恢复）。
                # to_thread 后落库在线程池执行，事件循环只负责调度。
                n = await asyncio.to_thread(
                    self._store.upsert_bars, code, bars,
                    period=self._period, adjust=self._adjust,
                    provider_id=provider_id, batch_id=self._batch_id,
                    schema_version="bars.v2", quality_state="raw")
            except Exception as exc:  # noqa: BLE001
                return SyncOutcome(code=code, error=f"落库失败：{exc}", paged=paged)
            return SyncOutcome(code=code, ok=True, bars_written=n,
                               as_of=as_of, stale=stale, paged=paged,
                               as_of_min=_bars_first_date(bars))

    async def sync_many(self, codes: Sequence[str],
                        progress_cb: Optional[Callable[[int, int, str], None]] = None,
                        skipped_complete: int = 0) -> SyncSummary:
        """批量同步（G6 进度钩子：progress_cb(done, total, code) 每完成一只回调）。

        ``skipped_complete``（V11 §5.3 P0-3 III）：全量回补时**因本地历史已覆盖到
        目标起点而跳过**的标的数。它必须以参数传入并原样写进汇总 —— 否则
        「跳过了 4000 只、只跑了 300 只」在结果里完全看不见，用户会以为同步
        坏了（或以为全市场只有 300 只）。
        """
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
            stale=sum(1 for o in ok if o.stale),
            as_of_max=max((o.as_of for o in ok if o.as_of), default=""),
            # 全批中**最早**的一根：只报「写入了 N 根」回答不了
            # 「这次到底把历史推到了哪一年」。
            as_of_min=min((o.as_of_min for o in ok if o.as_of_min), default=""),
            mode=self._mode,
            # ★ 只有**真的按区间翻了页**才算 full 生效。``paged=False`` 而
            #   ``mode=full`` ⇒ 当前环境没有支持区间的源，已退化为单次大 count，
            #   历史**并未**补齐 —— 界面与报告必须如实说出来。
            paged=(self._mode == "full" and any(o.paged for o in ok)),
            skipped_complete=int(skipped_complete or 0),
            # 批次号随汇总返回：调用方（EOD / 同步任务）发布数据集快照时要用它
            # 回查 local_bars，否则算出来的 row_count 恒为 0。
            batch_id=self._batch_id,
        )
        self._store.set_meta("last_sync_at", summary.finished)
        if summary.failed:
            log.warning("同步完成：ok=%d failed=%d（%s）", summary.ok,
                        summary.failed, "; ".join(e["code"] for e in summary.errors[:5]))
        else:
            log.info("同步完成：ok=%d bars=%d elapsed=%dms",
                     summary.ok, summary.bars_written, summary.elapsed_ms)
        # ★ 陈旧必须显式报警：ok 高 + stale 高 = 写了大量历史数据却宣称完成，
        # 这是最容易被忽略的失败（界面显示「已完成」，数据其实没更新）。
        if summary.stale:
            log.warning(
                "同步数据陈旧：%d/%d 只标的最后一根早于 %s 天前（最新 as_of=%s）——"
                "数据源可能未更新或无近期历史，请检查数据源与券商本地数据下载范围",
                summary.stale, summary.ok, self._stale_days, summary.as_of_max or "未知")
        # ★ 全量模式却没翻成页：这是**最危险的一种「看起来成功了」** ——
        #   mode 写着 full、ok 是满的，用户会以为历史已经补齐，实际上只是把
        #   最近 N 根又写了一遍。必须在日志里说清「历史其实没补齐」。
        if self._mode == "full" and not summary.paged and summary.ok:
            log.warning(
                "全量回补未按日期区间翻页：当前数据源链上没有任何源声明 "
                "supports_kline_range（免费在线源只接受 count=最近 N 根），"
                "已退化为单次 %d 根 —— 历史**未**真正补齐，"
                "如需完整历史请连接券商数据源后重跑", self._full_count)
        if summary.skipped_complete:
            log.info("全量回补断点续传：本地历史已覆盖目标起点的 %d 只被跳过",
                     summary.skipped_complete)
        return summary

    async def sync_stock_list(self, limit: Optional[int] = None,
                              progress_cb: Optional[Callable[[int, int, str], None]] = None
                              ) -> SyncSummary:
        """同步全市场股票列表（先刷列表，再按需同步 K 线）。"""
        from datasource.registry import get_hub as _hub
        items = await _hub().get_stock_list(source="auto")
        fallback = False
        if not items:
            # 券商兜底：券商侧**没有**「全市场股票列表」接口（get_stock_list 恒 None），
            # 但**能**提供板块成分代码（实测「沪深A股」5224 只）。
            # 缺了这一步，纯券商环境下「定时更新日线」每天静默空转、一只都不写，
            # 而任务状态还是 done —— 数据停更，界面却显示「已完成」。
            try:
                codes_fb, _src = await _hub().get_sector_stocks("沪深A股")
            except Exception:  # noqa: BLE001
                codes_fb = None
            if codes_fb:
                items = [{"code": str(c), "name": ""} for c in codes_fb]
                fallback = True
        if not items:
            return SyncSummary(started=now_iso(), finished=now_iso(),
                               total=0, failed=0, ok=0, errors=[],
                               elapsed_ms=0, mode=self._mode)
        if not fallback:
            # 兜底路径只有代码没有名称，写进股票列表会把名称覆盖成空 —— 不写。
            self._store.upsert_stock_list(items)
        codes = [str(i.get("code")) for i in items if i.get("code")]
        if limit:
            codes = codes[: int(limit)]
        # ★ 全量模式的**断点续传**：先按「本地最早一根是哪天」筛掉已补齐的标的。
        #   放在 ``limit`` 之后 —— limit 是用户显式的「只跑前 N 只」，
        #   不该被跳过数稀释成「前 N 只里再挑几只」。
        #   中断后重跑，已补齐的自动跳过 ⇒ 不需要额外的游标表，也不会与真实进度不一致。
        skipped = 0
        if self._mode == "full":
            codes, skipped = self._filter_backfilled(codes)
            if not codes and skipped:
                log.info("全量回补：%d 只标的本地历史均已覆盖目标起点，无需回补", skipped)
        return await self.sync_many(codes, progress_cb=progress_cb,
                                    skipped_complete=skipped)


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
