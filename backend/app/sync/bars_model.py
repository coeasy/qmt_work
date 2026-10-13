"""G1-5 同步器的**纯数据模型与纯函数**（从 bars.py 按职责拆出）。

为什么单独成文件：这些名字全是无副作用的数据结构 / 纯计算（计划、结果、汇总
dataclass 与日历、去重、窗口换算等纯函数），被 bars.py、eod.py、jobs.py、
test_* 多处共用。留在 60KB 的编排器里会让「改一个字段」必须通读几百行编排代码。

被 :mod:`app.sync.bars` **原样转出**（`from ... import`），既有调用方零改动。
"""
from __future__ import annotations
from dataclasses import dataclass, field

from datetime import date, datetime

from typing import Awaitable, Callable, List, Optional, Sequence



from core.clock import bar_date, local_now

#: 日线「新鲜」的默认判定窗口（自然日）。A 股最长休市是春节（约 9~10 天），
#: 取 10 可覆盖长假而不误判；周末 3 天、清明/五一等短假 3~5 天均在窗口内。
STALE_DAYS_DEFAULT = 10

#: 写入裁剪时**附带的尾部修正窗口**（根）：只写新数据时，仍把这 N 根旧数据一起
#: 重写一遍，用于消化复权/除权修正与源端的历史修订。``0`` = 连窗口都不写（最省）。
#: 取值理由：A 股除权除息与复权因子回溯通常只影响最近几个交易日的收盘价序列，
#: 10 根（约两周）足以覆盖，代价却只有原来的 3%。
REWRITE_TAIL_DEFAULT = 10

#: 汇总里保留的失败明细条数上限（超出部分只记数，不记明细）。
ERRORS_MAX = 200

#: 抓取器签名：(code, period, adjust, count) -> Optional[list[dict|Bar]]
#: 生产路径返回 ``(bars, source)`` 元组以便落库写入**真实来源名**；注入式测试可返回纯 list。
FetchBars = Callable[[str, str, str, int], Awaitable[Optional[list]]]

#: 热窗口换算成回源根数的余量（与 ``gateway.market_sync.hot_fetch_count`` 同源，
#: 避免两套公式漂移：热窗口按**自然日**定义，回源按**交易日根数**，二者单位不同）。
_HOT_FETCH_MARGIN = 15


def _hot_fetch_count(hot_days: int) -> int:
    """热窗口（自然日）→ 回源要取的 K 线根数（交易日）。

    热窗口按自然日定义（``bars_hot_days``，默认 92 ≈ 3 个月），而回源接口按
    「最近 N 根」取数，直接用 92 会少取（92 个交易日 ≈ 4.5 个月），
    用 250 会多取（≈ 1 年，把冷数据天天重拉一遍）。换算：
    一年约 250 个交易日 / 365 个自然日 ≈ 0.685，再留 ``_HOT_FETCH_MARGIN`` 根
    余量兜住长假与未收盘的最后一根。取够即可：多取的根由冷仓路由自然进冷区，
    不会污染热表。
    """
    days = max(1, int(hot_days or 1))
    return int(days * 250 / 365) + _HOT_FETCH_MARGIN


@dataclass
class SyncPlan:
    """单标的冷热同步计划（由 :meth:`BarsSyncer._build_plans` 产出）。

    - ``hot``：是否需要刷新**热窗口**（最近 ``bars_hot_days`` 天）数据；
    - ``cold``：是否需要补齐**冷历史**（热窗口之外、落到 ``bars_cold.db`` 的部分）；
    - ``reason``：判定依据（诊断/界面可读，便于解释「为什么这只跳过了」）。
    """
    hot: bool = True
    cold: bool = False
    reason: str = ""


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
    （app/sync/calendar.py：内置 2024-2026 交易所休市安排，
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
    cold_written: int = 0
    error: str = ""
    #: 因「本地已有且源未更新」而**跳过写入**的根数（写入裁剪，见
    #: :attr:`BarsSyncer._trim_new`）。与 ``bars_written`` 分开计：
    #: 「写了 0 根」可能是「源挂了」，也可能是「本来就没新数据」——
    #: 混成一个数字会让「数据停更」与「一切正常」长得一模一样。
    bars_unchanged: int = 0
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
                "bars_written": self.bars_written,
                "cold_written": self.cold_written, "error": self.error,
                "bars_unchanged": self.bars_unchanged,
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
    cold_written: int = 0
    elapsed_ms: int = 0
    errors: List[dict] = field(default_factory=list)
    #: 因超过 ``ERRORS_MAX`` 而**未列入** ``errors`` 的失败条数。
    #: 全市场 5000+ 只集体失败时（网络断了、源熔断了），把几千条明细塞进汇总
    #: 再写进 job 结果 / 快照 manifest，只会把真正的失败原因淹掉 ——
    #: 这里保留前 N 条 + 一个「还有多少条」的计数，既省存储又不丢事实。
    #:
    #: ★ 字段名与 ``app.runtime.system_jobs`` 的**对外契约**一致（前端类型里也
    #:   是这个名）：同步链上有两处截断（本类一层、落库流一层），用两个名字会
    #:   让「到底截了多少」永远对不上账。
    errors_truncated: int = 0
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
    #: 增量模式下因「本地已有最近一根」而**跳过**的标的数（V11 R15）。
    #:
    #: 必须与 ``skipped_complete`` **分开计**：前者是「历史已补齐」、后者是「已经够新」，
    #: 混成一个 skipped 会让用户看不出这次到底做了什么（界面上两者含义完全不同）。
    skipped_fresh: int = 0
    #: 命中写入裁剪、源数据与本地一致而**未重复写库**的根数。
    #: 与 ``bars_written`` 配对看：``written=0 & unchanged 很大`` = 「一切正常、
    #: 今天没有新数据」；``written=0 & unchanged=0`` = 「源可能挂了」。
    #: 没有这个数字，EOD 只能靠 ``written > 0`` 判质量，于是「重跑一次同步」
    #: 会被判成 ``invalid``（数据其实完整且最新）。
    bars_unchanged: int = 0
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
            "bars_written": self.bars_written, "cold_written": self.cold_written,
            "elapsed_ms": self.elapsed_ms,
            "bars_unchanged": self.bars_unchanged,
            "errors": self.errors, "errors_truncated": self.errors_truncated,
            "stale": self.stale,
            "as_of_max": self.as_of_max, "as_of_min": self.as_of_min,
            "mode": self.mode, "paged": self.paged,
            "skipped_complete": self.skipped_complete,
            "skipped_fresh": self.skipped_fresh,
            "batch_id": self.batch_id,
        }


