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
  内置 2024-2026 沪深北交易所休市安排 + runtime_config 扩展，G1-5b 落地；
  R26 起周末一律休市，调休补班日不再判为交易日）。

用法（手动 CLI）：
    python -m app.sync.bars --limit 20 --concurrency 4 --lookback 320
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Callable, Optional, Sequence

from core.clock import bar_date, local_now  # 唯一时钟在 core.clock（V11 R8 收敛）
from core.clock import now_iso  # noqa: F401  —— 唯一实现在 core.clock（V11 R8 收敛）
from datasource.cold_store import get_cold_store
from datasource.local_store import LocalStore, get_store
from datasource.registry import bars_last_date, get_hub

log = logging.getLogger("qmt_work.sync.bars")

# 数据模型 / 纯函数 / 冷仓集成已按职责拆出（Gate 4：源码 50KB 上限）。
# 下列名字在此**原样转出**，既有调用方（含测试）无需改动；冷仓方法是 mixin。
from app.sync.bars_model import (  # noqa: F401
    ERRORS_MAX, FetchBars, REWRITE_TAIL_DEFAULT, STALE_DAYS_DEFAULT,
    SyncOutcome, SyncPlan, SyncSummary, _bar_time, _bars_first_date,
    _dedupe_bars, _hot_fetch_count, _is_stale, _resolve_provider_id,
    _split_fetch_result, weekday_calendar,
)
from app.sync.bars_cold import ColdZoneMixin


class BarsSyncer(ColdZoneMixin):
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
    #: 全量回补容忍的**前导空年**数：最近 ``N`` 年都取不到数据时，仍会再往前翻
    #: 一年（第 ``N+1`` 页）才放弃。退市股 / 长期停牌股的历史就在更早年，年初
    #: 「今年还没有交易日」也属于这一类。拿到数据后遇空年不受此限 —— 那是上市
    #: 日边界，立即停。
    FULL_LEAD_EMPTY_MAX = 3

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
        skip_fresh: bool = False,
        trim_new: bool = True,
        rewrite_tail: int = REWRITE_TAIL_DEFAULT,
        zone: str = "auto",
    ):
        self._store = store or get_store()
        self._fetch = fetch_bars or self._default_fetch
        self._concurrency = max(1, int(concurrency))
        self._lookback = int(lookback)
        #: ``incremental`` / ``full``（非法值按 incremental 处理，绝不静默变全量）
        self._mode = "full" if str(mode).strip().lower() == "full" else "incremental"
        self._full_years = max(1, int(full_years or self.FULL_YEARS_DEFAULT))
        self._full_count = max(1, int(full_count or self.FULL_COUNT_DEFAULT))
        #: 冷热分层（2026-10-10）：``auto`` = 热必做 + 冷按需；``hot`` = 只维护热窗口；
        #: ``cold`` = 只补齐冷历史（不碰热表）；``both`` = 热+冷全量（等价旧 ``mode=full``）。
        self._zone = str(zone or "auto").strip().lower()
        if self._zone not in ("hot", "cold", "auto", "both"):
            self._zone = "auto"
        # ``mode=full`` 向后兼容：等价于 ``zone=both``（热窗口 + 冷历史都补）。
        # 仅当调用方没显式给 zone 时生效，避免覆盖显式选择。
        if self._mode == "full" and self._zone == "auto":
            self._zone = "both"
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
        #: ★ **写入裁剪**（2026-10-10）：增量模式下只把「本地最新一根之后」的
        #: K 线写库，外加尾部 ``rewrite_tail`` 根做修正窗口。
        #:
        #: 为什么必须做：实测本机 ``local_bars`` 已 395 万行、``app.db`` 1.56GB。
        #: 增量同步每天把 ``lookback=320`` 根 **整窗口重写一遍**（7740 只 × 320
        #: ≈ 248 万行/天），而这些行 99% 与库里逐字节相同 —— 纯写放大：
        #: 磁盘持续膨胀、WAL 疯长、落库线程与在线请求抢写锁。
        #:
        #: 为什么敢裁剪：``INSERT OR REPLACE`` 的幂等保证是「重复写也一样」，
        #: 而**复权/除权修正**只影响最近若干根（尾窗口覆盖），历史深处的行
        #: 不会因为今天同步一次就变。真要强制全量重写就传 ``trim_new=False``。
        #:
        #: ★ 只在 **incremental** 生效：``full`` 要补的就是历史区间，裁剪会把它
        #:   裁没了（那才是「假成功」）。且只在**本地已有该标的数据**时生效 ——
        #:   库里没有 = 第一次拉，全写。
        self._trim_new = bool(trim_new)
        self._rewrite_tail = max(0, int(rewrite_tail or 0))
        #: 热窗口天数（单一真源 = ``settings.bars_hot_days``，默认 92 ≈ 3 个月）。
        #: 与 ``KlineCache.hot_cutoff`` 共用同一输入，确保「写入路由」与「读取边界」
        #: 永不过两套公式（否则会出现「刚搬进冷仓又被热同步写回热表」的抖动）。
        try:
            from core.config import settings as _settings
            self._hot_days = int(getattr(_settings, "bars_hot_days", 92) or 92)
        except Exception:  # noqa: BLE001 配置不可用时按默认
            self._hot_days = 92
        #: 热窗口回源根数：由 ``_hot_fetch_count`` 换算，且**不低于** ``lookback``，
        #: 保证旧调度（``lookback=120``）的日线覆盖不受影响，同时日常回源根数
        #: 不再无谓放大（热窗口之外由冷仓承载）。
        self._hot_count = max(_hot_fetch_count(self._hot_days), self._lookback)
        #: 冷仓单例（``bars_cold.db``）。为 ``None`` 时（极少数：冷仓未初始化）
        #: 冷同步降级为「跳过并标记 reason」，绝不抛异常中断整批热同步。
        self._cold = get_cold_store()
        #: 批量读到的「本地最新一根」（code -> dt），供裁剪当基线。空 = 全部首拉。
        self._local_latest: dict = {}
        #: ``incremental`` 下跳过「本地已有最新一根」的标的（V11 R15）。
        #: 默认关闭 = 保持「每只都问一次源」的原行为；定时/调度场景显式开启。
        self._skip_fresh = bool(skip_fresh)
        #: ``skip_fresh`` 本次实际跳过的只数（由 :meth:`_filter_fresh` 产出，
        #: 在 :meth:`sync_many` 汇总时取用后清零）。
        self._skipped_fresh = 0

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

        ★ 两种「空」必须分开（2026-10-10 审计修）：:meth:`get_kline_range` 对
        「链上无源支持区间」与「这一年确实没有数据」都返回 ``(None, None)``，
        靠「第一页空不空」去猜会连出两个真 bug ——

        - **年初空页**：1 月 1 日前后「今年还没有交易日」，第一页返回空 ⇒ 被判成
          「没有源支持区间」，整只标的退化为单次大 count，**历史永不补齐**；
        - **退市 / 长期停牌股**：最近几年都没有数据，第一页空就 ``break`` ⇒
          历史同样一根不补，界面还显示「同步完成」。

        因此先做一次**零 RPC 的声明式探测**（:meth:`kline_range_supported`）
        把「能力不存在」与「这一年没数据」分开：

        - 探测说不支持 ⇒ 直接退化（**一次无效 RPC 都不发**）；
        - 探测说支持 ⇒ 逐页翻；**拿到数据之后**遇空年才停（上市日边界）；
          始终拿不到数据的，允许最多 ``_FULL_LEAD_EMPTY_MAX`` 个前导空年继续往
          前翻（退市股的历史在更早年），仍全空才如实放弃。
        """
        from core.clock import local_now
        hub = get_hub()
        supported = self._range_supported(hub)      # True / False / None(未知)
        if supported is False:
            log.debug("全量回补 %s：链上无源支持日期区间，直接退化为单次大 count",
                      code)
            return None, "", False
        today = local_now().date()
        merged: list = []
        src_name = ""
        paged = False
        lead_empty = 0
        year = today.year
        for _ in range(self._full_years):
            start = f"{year}0101"
            end = today.strftime("%Y%m%d") if year == today.year else f"{year}1231"
            try:
                bars, src = await hub.get_kline_range(
                    code, self._period, adjust=self._adjust,
                    start=start, end=end, source=self._source)
            except Exception as exc:  # noqa: BLE001 单页失败不击穿整只
                log.debug("全量回补 %s %s 区间取数失败：%s", code, year, exc)
                bars, src = None, None
            if bars is None and not paged and supported is None:
                # 「探测未知」的旧路径：第一页拿不到就按「没有源支持区间」处理，
                # 交由调用方走大 count —— 与改造前逐字一致（老 hub / 注入桩）。
                return None, "", False
            paged = True
            if not bars:
                if merged:
                    # 已拿到数据后遇空年 = 到上市日（或更早的边界）⇒ 停止向前翻页
                    break
                if supported is None:
                    break        # 未知态：保持「空页即停」的旧行为
                # 前导空年：今年可能还没有交易日，也可能是退市股 —— 允许再往前翻几页
                lead_empty += 1
                if lead_empty > self.FULL_LEAD_EMPTY_MAX:
                    break
            else:
                merged.extend(bars)
                src_name = src or src_name
            year -= 1
        if not paged:
            return None, "", False
        return _dedupe_bars(merged), src_name, True

    def _range_supported(self, hub) -> Optional[bool]:
        """链上有无源支持区间取数：**True / False / None（未知）**三态。

        ★ 为什么是三态而不是布尔：``get_hub()`` 在测试与部分集成路径里常被换成
        只实现 ``get_kline_range`` 的**注入桩**，探测方法不存在。此时返回
        ``None`` 让 :meth:`_fetch_full` 走**改造前**的判据（第一页空即退化），
        既有行为与既有测试逐字不变；真实 hub 有探测方法才拿到新语义。
        """
        probe = getattr(hub, "kline_range_supported", None)
        if not callable(probe):
            return None
        try:
            return bool(probe(self._source, adjust=self._adjust,
                              period=self._period))
        except Exception as exc:  # noqa: BLE001 探测失败不拦路：按「未知」走原路径
            log.debug("区间能力探测失败（按未知处理）：%s", exc)
            return None

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

    def _fresh_target(self) -> str:
        """增量「已经够新」的判定基准日（``YYYYMMDD``）。

        走 :func:`app.sync.calendar.expected_bar_date` 而不是「今天」—— 收盘后数据源
        普遍要到傍晚才更新当日 K 线，在这个窗口里要求今天的数据只会每天白跑一次
        全市场同步（详见 calendar.expected_bar_date 的取舍说明）。
        """
        from app.sync.calendar import expected_bar_date
        return expected_bar_date()

    def _filter_fresh(self, codes: Sequence[str]) -> tuple[list, int]:
        """筛掉「本地最近一根已达当日/上一交易日」的标的；``(待同步, 已跳过数)``。

        ★ 与 :meth:`_filter_backfilled` 同 philosophy：**数据即游标**。判据是库里真实的
        最近一根，而不是「上次跑到哪」的进度表 —— 后者崩一次就白记，前者永远与事实一致。

        ★ 三个必须如此的边界：
        1. **跳过逻辑只对 incremental 开放**。全量回补（`full`）要补的是**历史区间**，
           只看最近一根就跳过会把「昨天补齐、前年还空着」的标的永久漏掉。
        2. **判断失败一律全量重跑**并如实返回 ``0``。读不到就不敢说它够新 —— 少报跳过
           数只是多花一次 RPC，漏同步是数据静默停更。
        3. ``target`` 为空（日历异常）时**不启用跳过**：不能拿一个未知日期当判定线。

        注意：这里「跳过」只在**本次**生效。下一次目标日推进（新交易日）时又会自然
        重跑 —— 这正是日常定时同步想要的行为：每天只增量拉新的一天。
        """
        target = self._fresh_target()
        if not target:
            return list(codes), 0
        try:
            have = self._store.latest_dt_map(list(codes), self._period, self._adjust)
        except Exception as exc:  # noqa: BLE001
            log.warning("增量跳过：读取本地最新日期失败（将全量重跑）：%s", exc)
            return list(codes), 0
        need = [c for c in codes if not have.get(c) or str(have[c]) < target]
        return need, len(codes) - len(need)

    # ------------------------------------------------------------------
    # 核心
    # ------------------------------------------------------------------
    async def sync_one(self, code: str) -> SyncOutcome:
        """同步单只标的。

        ★ 热路径的并发槽**只在这里取**；冷路径的槽在
        :meth:`app.sync.bars_cold.ColdZoneMixin._sync_one_cold` 各取各的，
        :meth:`_sync_one_zones` **不占槽**只做编排。

        绝对不要写「先占槽再调 sync_one」的包裹层：``asyncio.Semaphore``
        **不可重入**，外层槽占满后内层永远拿不到 ⇒ 整个同步任务永久挂死且
        日志零输出（2026-10-13 实测 P0，见 :meth:`_sync_one_zones` 的历史教训注释）。
        """
        async with self._sem:
            paged = False
            try:
                if self._mode == "full":
                    bars0, src0, paged = await self._fetch_full(code)
                    raw = ((bars0, src0) if paged
                           else await self._fetch(code, self._period, self._adjust,
                                                  self._full_count))
                else:
                    raw = await self._fetch(code, self._period, self._adjust,
                                           self._hot_count)
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
            # ★ as_of_min 用**裁剪前**的 bars：它回答的是「源这次给到了多早」，
            #   裁剪只影响写多少，不该把「源覆盖范围」改成一个窗口起点。
            as_of_min = _bars_first_date(bars)
            # 写入裁剪：只写「本地最新之后 + 尾部修正窗口」（见 __init__ 说明）。
            bars, unchanged = self._trim_bars(code, bars)
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
                               bars_unchanged=unchanged,
                               as_of=as_of, stale=stale, paged=paged,
                               as_of_min=as_of_min)

    def _trim_bars(self, code: str, bars: list) -> tuple[list, int]:
        """写入裁剪：丢掉「本地已有且不可能因本次同步而改变」的行。

        返回 ``(待写 bars, 因一致而跳过的根数)``。三种情况**原样返回**（不裁剪）：

        1. ``trim_new`` 关闭 / ``full`` 模式 —— 全量回补要的就是历史区间；
        2. 本地没有这只标的的记录 —— 第一次拉，全写；
        3. 本地记录比源还新或源日期无法解析 —— 判断不了就不动手，宁可多写。
        """
        if not self._trim_new or self._mode == "full":
            return bars, 0
        cut = str(self._local_latest.get(code) or "")
        if not cut:
            return bars, 0
        dated = [(bar_date(_bar_time(b)), b) for b in bars or []]
        if not any(d for d, _ in dated):
            return bars, 0                       # 日期全不可解析：交给落库去丢弃
        newer = [d for d, _ in dated if d and d > cut]
        if not newer:
            # 源没有任何比本地新的数据 ⇒ 一行都不写，但**如实计为 unchanged**
            return [], len(bars)
        if self._rewrite_tail <= 0:
            keep = {d for d in newer}
            return [b for d, b in dated if d in keep], len(bars) - len(newer)
        older = sorted({d for d, _ in dated if d and d <= cut})
        floor = older[-self._rewrite_tail] if len(older) > self._rewrite_tail else (
            older[0] if older else newer[0])
        keep = [b for d, b in dated if d and d >= floor]
        return keep, len(bars) - len(keep)

    # ------------------------------------------------------------------
    # 冷热分层（2026-10-10）：按数据条件自动决定"热/冷"要不要同步
    # ------------------------------------------------------------------
    def _build_plans(self, codes: Sequence[str]) -> tuple[dict, int, int]:
        """为每只标的算出冷热同步计划（见 :class:`SyncPlan`）。

        判定**只用库里真实已有的起止日期**当事实游标（与 :meth:`_filter_fresh` /
        :meth:`_filter_backfilled` 同哲学），不引入额外游标表，崩了也不丢进度：

        - ``needs_hot``：热表最新一根早于「预期交易日」⇒ 今天的新数据还没拉回来。
        - ``needs_cold``：冷仓最早一根**晚于**目标起点（``full_years`` 年前）⇒
          历史还没补齐；且未被标记 ``cold_complete``（补齐后不再重拉，避免每天
          把已退市/近期上市股的冷历史重拉一遍）。
        - 新股（上市 < 热窗口）天然没有冷区间 ⇒ 不参与冷同步；
        - ``zone`` 直接决定范围：``hot`` 只热、``cold`` 只冷、``auto``/``both`` 按上式。

        返回 ``(plans, skipped_fresh, skipped_cold)``。
        """
        plans: dict = {}
        hot_target = (self._fresh_target()
                      if (self._zone != "cold" and self._skip_fresh) else "")
        hot_latest: dict = {}
        hot_earliest: dict = {}
        # 热表最新/最早日期：非 cold 区都要读（hot/both/auto 都可能需要）。
        # 一次 GROUP BY 同批取回 latest 与 earliest，不付两次查询代价。
        if self._zone != "cold":
            try:
                hot_latest = self._store.latest_dt_map(
                    list(codes), self._period, self._adjust)
                hot_earliest = self._store.earliest_dt_map(
                    list(codes), self._period, self._adjust)
            except Exception as exc:  # noqa: BLE001 读不到 = 全部当作需同步
                log.warning("冷热分区读取热表日期失败：%s", exc)
                hot_latest = {}
                hot_earliest = {}
        # 热窗口起点（YYYYMMDD）：早于它的数据才属于「冷区间」。与 KlineCache.hot_cutoff
        # 同源（都由 bars_hot_days 换算），避免出现「刚搬进冷仓又被热同步写回热表」抖动。
        hot_cutoff = (local_now().date() - timedelta(days=self._hot_days)).strftime("%Y%m%d")
        cold_earliest: dict = {}
        cold_complete: set = set()
        if self._zone != "hot" and self._cold is not None:
            try:
                cold_earliest = self._cold.earliest_dt_map(
                    list(codes), self._period, self._adjust)
            except Exception as exc:  # noqa: BLE001 冷仓不可读 = 全部当作需补齐
                log.warning("冷热分区读取冷仓最早日期失败：%s", exc)
                cold_earliest = {}
            cold_complete = self._cold_complete_map(list(codes))
        cold_target = self._target_start()

        def _covered(c: str) -> bool:
            """历史是否已覆盖到目标起点（热表或冷仓任一即可）。

            用于全量/冷补齐的**断点续传跳过**——等价旧 ``mode=full`` 的
            :meth:`_filter_backfilled` 语义：某只票本地（含冷仓）最早一根已 ≤ 目标起点，
            说明历史已经补齐，整只跳过（热也不必刷），崩了重跑不会把已补齐的再拉一遍。
            """
            he = hot_earliest.get(c)
            ce = cold_earliest.get(c)
            best = None
            if he:
                best = he
            if ce and (best is None or str(ce) < str(best)):
                best = ce
            return best is not None and str(best) <= cold_target

        for c in codes:
            # ★ zone="cold" 严格只补冷历史、**绝不**碰热表（写 local_bars）。
            # 此前 do_hot 默认 True、仅在 zone!="cold" 分支里才可能被置 False ⇒
            # zone="cold" 时 do_hot 恒为 True，热同步照样跑（把冷补齐任务变成
            # 「又热又冷」），既违背 plan 的语义，又让冷仓调度平白写一遍热表。
            do_hot = self._zone != "cold"
            if do_hot and hot_target:
                if c in hot_latest and str(hot_latest[c]) >= hot_target:
                    do_hot = False
            do_cold = False
            if self._zone != "hot":
                if self._zone in ("cold", "both"):
                    # 全量/冷补齐：历史已覆盖到目标起点 ⇒ **整只跳过**（热也不必刷），
                    # 等价旧 mode=full 的断点续传——已补齐的票不再重拉。
                    covered = _covered(c)
                    do_cold = (c not in cold_complete) and not covered
                    if covered:
                        do_hot = False
                else:  # auto：缺失且「确实有冷区间」才补
                    ce = cold_earliest.get(c)
                    # ★ 新股 / 近期上市股：本地最早一根仍在热窗口内 ⇒ 根本没有冷区间，
                    # 不应触发一次全量冷回补（首次补完标记 complete 后虽会跳过，但那次
                    # 回补拉的全是热窗口内的重复数据，纯浪费）。只在「最早一根早于热窗口」
                    # 时才认为该股真正需要冷历史。
                    he = hot_earliest.get(c)
                    has_cold_interval = he is not None and str(he) < hot_cutoff
                    do_cold = (c not in cold_complete) and has_cold_interval and (
                        ce is None or str(ce) > cold_target)
            plans[c] = SyncPlan(hot=do_hot, cold=do_cold)
        skipped_fresh = sum(
            1 for c in codes if self._zone != "cold" and hot_target
            and c in hot_latest and str(hot_latest[c]) >= hot_target)
        skipped_cold = sum(1 for c in codes if not plans[c].cold
                           and self._zone != "hot")
        return plans, skipped_fresh, skipped_cold

    async def _sync_one_zones(self, code: str, plan: SyncPlan) -> SyncOutcome:
        """按计划执行单标的的冷热同步（热/冷可独立成失败，互不拖累）。

        ★ 本方法**不占并发槽**，只做编排：槽由叶子操作各自取（
        :meth:`sync_one` / :meth:`_sync_one_cold`）。

        历史教训（2026-10-13 实测 P0）：本方法曾 ``async with self._sem``
        包住 ``await self.sync_one(code)``，而 ``sync_one`` 内部也取**同一个**
        ``asyncio.Semaphore`` —— 该锁**不可重入**，``concurrency`` 个槽被外层
        占满后内层永远拿不到，于是**整个同步任务永久挂死**：``gather`` 无超时、
        进程仍活着、日志零输出，且 :meth:`sync_many` 每只标的都走这条路径，
        所以日常增量 / EOD 全市场同步都会中招。
        """
        hot_out = None
        cold_out = None
        if plan.hot:
            hot_out = await self.sync_one(code)
        if plan.cold:
            cold_out = await self._sync_one_cold(code)
        outs = [o for o in (hot_out, cold_out) if o is not None]
        if not outs:
            return SyncOutcome(code=code, ok=False, error="无同步计划")
        if len(outs) == 1:
            return outs[0]
        merged = SyncOutcome(code=code)
        merged.ok = any(o.ok for o in outs)
        merged.bars_written = sum(o.bars_written for o in outs)
        merged.cold_written = sum(o.cold_written for o in outs)
        merged.bars_unchanged = sum(o.bars_unchanged for o in outs)
        merged.stale = any(o.stale for o in outs)
        merged.paged = any(o.paged for o in outs)
        merged.error = " | ".join(o.error for o in outs if o.error)
        dates = [o.as_of for o in outs if o.as_of]
        merged.as_of = max(dates) if dates else ""
        mins = [o.as_of_min for o in outs if o.as_of_min]
        merged.as_of_min = min(mins) if mins else ""
        return merged

    async def sync_many(self, codes: Sequence[str],
                        progress_cb: Optional[Callable[[int, int, str], None]] = None,
                        skipped_complete: int = 0,
                        plans: Optional[dict] = None,
                        skipped_cold: int = 0) -> SyncSummary:
        """批量同步（G6 进度钩子：progress_cb(done, total, code) 每完成一只回调）。

        ``skipped_complete``（V11 §5.3 P0-3 III）：全量回补时**因本地历史已覆盖到
        目标起点而跳过**的标的数。它必须以参数传入并原样写进汇总 —— 否则
        「跳过了 4000 只、只跑了 300 只」在结果里完全看不见，用户会以为同步
        坏了（或以为全市场只有 300 只）。

        ★ 增量跳过的 ``skipped_fresh`` **不加进签名**而是走 :attr:`_skipped_fresh`
        实例状态：跳过数是本同步器自己筛出来的，外部无从知晓，让它当参数只会给
        调用方（含测试替身）一次签名破坏却拿不到任何好处 —— 历史教训：加一个参数
        会让所有 ``async def _fake_sync_many(codes, progress_cb, skipped_complete)``
        替身就地 TypeError。自己的派生值，自己携带。
        """
        started = now_iso()
        t0 = time.perf_counter()
        done = 0
        total = len(codes)
        # 写入裁剪的基线：一次 GROUP BY 拿回全部标的最新一根（不是逐只问）。
        # 只在增量模式 + 开裁剪时读，全量回补不必付这次查询。
        #
        # ★ **不能**用 ``asyncio.to_thread``：``DB._read_conn`` / ``IntradayStore``
        #   的只读连接是**惰性创建且绑定创建线程**的（sqlite3 默认
        #   ``check_same_thread=True``）。在 worker 线程里首次触发建连，之后再从
        #   事件循环所在线程读同一连接会抛
        #   ``ProgrammingError: SQLite objects created in a thread ...``
        #   （实测：分钟线数据集同步即在此炸掉）。这里与 :meth:`_filter_fresh`
        #   保持一致——同步读，代价是一次批量 GROUP BY（全市场约 0.3s）。
        if self._trim_new and self._mode != "full" and codes:
            try:
                self._local_latest = self._store.latest_dt_map(
                    list(codes), self._period, self._adjust)
            except Exception as exc:  # noqa: BLE001 读失败 = 按「都没有」处理（全写）
                log.warning("写入裁剪基线读取失败（本次全量重写）：%s", exc)
                self._local_latest = {}

        async def _tracked(code: str):
            nonlocal done
            plan = (plans or {}).get(
                code, SyncPlan(hot=True, cold=(self._zone != "hot")))
            out = await self._sync_one_zones(code, plan)
            done += 1
            if progress_cb:
                progress_cb(done, total, code)
            return out

        # ★ ``return_exceptions=True`` 是硬要求，不是风格选择。
        #
        #   ``sync_one`` 内部虽然吞掉了抓取/落库异常，但它的**外层**（`bars_last_date`、
        #   `AsyncIOSemaphore`、 cooperative cancel）仍然可能抛出 / 被取消。默认 gather
        #   遇第一个异常就整体抛出 ⇒ **已经跑完的几千只结果全部丢弃**，调用方既拿不到
        #   汇总也拿不到分批错误，任务状态直接变 failed 且不留证据（「同步跑了半小时，
        #   日志里只剩一句 traceback」）。
        #   这里把异常**降级为单只失败**（真正的 ROI：坏一只不拖垮一大批）。
        raw_outcomes = await asyncio.gather(
            *[_tracked(c) for c in codes], return_exceptions=True)
        outcomes: list[SyncOutcome] = []
        for code, out in zip(codes, raw_outcomes):
            if isinstance(out, BaseException):
                log.warning("sync %s 任务级异常：%s", code, out)
                outcomes.append(SyncOutcome(code=code, error=f"任务异常：{out}"))
            else:
                outcomes.append(out)
        ok = [o for o in outcomes if o.ok]
        failed = [o for o in outcomes if not o.ok]
        err_list = [o.to_dict() for o in failed]
        omitted = 0
        if len(err_list) > ERRORS_MAX:
            omitted = len(err_list) - ERRORS_MAX
            err_list = err_list[:ERRORS_MAX]
        summary = SyncSummary(
            started=started,
            finished=now_iso(),
            total=len(outcomes),
            ok=len(ok),
            failed=len(failed),
            bars_written=sum(o.bars_written for o in ok),
            cold_written=sum(o.cold_written for o in ok),
            bars_unchanged=sum(o.bars_unchanged for o in ok),
            errors=err_list,
            errors_truncated=omitted,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
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
            # 冷历史补齐跳过数（与全量回补的 skipped_complete 同源：都是
            # 「因本地已满足而跳过」，避免把「跳了 4000 只」藏起来）。
            skipped_complete=int(skipped_cold or skipped_complete or 0),
            # 用完即清：避免上一批的跳过数串到下一批报告里（「这次没跳过」会说谎成
            # 「这次跳了很多」，而用户正是据此判断要不要手动补同步的）。
            skipped_fresh=int(self._skipped_fresh or 0),
            # 批次号随汇总返回：调用方（EOD / 同步任务）发布数据集快照时要用它
            # 回查 local_bars，否则算出来的 row_count 恒为 0。
            batch_id=self._batch_id,
        )
        # 裁剪基线用完即清：它是**本批**的快照，留到下一批会把「上次的最新一根」
        # 当成「这次的最新一根」，于是新数据可能被裁掉（静默漏同步）。
        self._local_latest = {}
        # 分钟仓（IntradayStore）没有 meta 表——它有自己独立的 SQLite 文件，
        # 同步时间已由 :mod:`app.sync.datasets` 记到主库
        # ``dataset.<id>.last_sync_at``。这里必须容错，否则分钟线同步会在
        # **数据已经写完之后**抛 AttributeError，整批被报成「同步异常」。
        if hasattr(self._store, "set_meta"):
            self._store.set_meta("last_sync_at", summary.finished)
        if summary.failed:
            log.warning("同步完成：ok=%d failed=%d（%s）", summary.ok,
                        summary.failed, "; ".join(e["code"] for e in summary.errors[:5]))
        else:
            log.info("同步完成：ok=%d bars=%d unchanged=%d elapsed=%dms",
                     summary.ok, summary.bars_written, summary.bars_unchanged,
                     summary.elapsed_ms)
        # ★ 裁剪生效时必须说清楚「省了多少」：否则 written 骤降会被误读成
        #   「源不给数据了」——而它恰恰是「一切都好，今天没有新数据」的证据。
        if summary.bars_unchanged:
            log.info("写入裁剪：%d 根与本地一致已跳过重写（尾部修正窗口 %d 根仍写）",
                     summary.bars_unchanged, self._rewrite_tail)
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
        """同步全市场股票列表（先刷列表，再按需同步 K 线）。

        ★ 必须用模块级 ``get_hub``（2026-10-04 全量回归实测挂起 30+ 分钟）：
          此前这里是**函数内局部导入** ``from datasource.registry import get_hub as _hub``，
          绕过了模块级可替换点 —— 测试 monkeypatch ``app.sync.bars.get_hub`` 后，
          其余方法都走桩，唯独本方法仍连**真实数据源**。本机装了 eltdx 时，
          「全市场股票列表 + 全量 K 线」会真的从 TDX 拉（5224 只 × 并发 8），
          单跑看不出问题（无券商时快速失败），全量回归直接挂死。
        """
        items = await get_hub().get_stock_list(source="auto")
        fallback = False
        if not items:
            # 券商兜底：券商侧**没有**「全市场股票列表」接口（get_stock_list 恒 None），
            # 但**能**提供板块成分代码（实测「沪深A股」5224 只）。
            # 缺了这一步，纯券商环境下「定时更新日线」每天静默空转、一只都不写，
            # 而任务状态还是 done —— 数据停更，界面却显示「已完成」。
            try:
                codes_fb, _src = await get_hub().get_sector_stocks("沪深A股")
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
        # ★ 冷热分区（2026-10-10）：一次性算出每只标的的「热/冷」要不要同步，
        #   用库里真实已有的起止日期当游标（与旧 ``_filter_fresh`` / ``_filter_backfilled``
        #   同哲学，但统一到一张计划表上，避免热冷两套判定各写一遍）。
        #   放在 ``limit`` 之后 —— limit 是用户显式的「只跑前 N 只」，不该被跳过数稀释。
        #   中断后重跑，已最新的 / 冷历史已补齐的自动跳过 ⇒ 不需要额外游标表。
        plans, skipped_fresh, skipped_cold = self._build_plans(codes)
        self._skipped_fresh = skipped_fresh
        # 过滤掉「热冷都不需要」的标的：否则会产生一串空 outcome（total 虚高、界面误读）。
        run_codes = [c for c in codes
                     if (plans.get(c) or SyncPlan()).hot
                     or (plans.get(c) or SyncPlan()).cold]
        if not run_codes:
            if skipped_fresh or skipped_cold:
                log.info("同步：热跳过 %d 只、冷跳过 %d 只 —— 全部已最新 / 冷历史已补齐",
                         skipped_fresh, skipped_cold)
            else:
                log.info("同步：无待同步标的（代码池为空）")
            # ★ 提前返回也必须把跳过数带出去：调用方（jobs.py）据此区分
            # 「真空转」与「断点续传/已最新」。漏带会让 weekly 冷调度在「已全部补齐」时
            # 被误判为「未获取到任何股票」而报失败（实测风险，见 jobs.py 的同名判据）。
            return SyncSummary(started=now_iso(), finished=now_iso(), total=0,
                               ok=0, failed=0, errors=[], elapsed_ms=0, mode=self._mode,
                               skipped_complete=skipped_cold,
                               skipped_fresh=skipped_fresh)
        if skipped_fresh:
            # ★ 显式报「应跑/实跑」：汇总的 total 是**实际拉取**的只数，跳过数单列。
            log.info("增量同步：应同步 %d 只 → 实拉 %d 只（热跳过已最新 %d 只）",
                     len(codes), len(run_codes), skipped_fresh)
        if skipped_cold:
            log.info("冷历史：%d 只已补齐跳过（仅 %d 只需补冷）",
                     skipped_cold, sum(1 for c in run_codes
                                       if (plans.get(c) or SyncPlan()).cold))
        return await self.sync_many(run_codes, progress_cb=progress_cb,
                                    skipped_complete=skipped_cold, plans=plans)


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
