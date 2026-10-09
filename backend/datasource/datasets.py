"""可下载数据集 SSOT（R28 · 多数据类型同步）。

## 为什么需要这一层

R28 之前「定时下载数据」只有一条路：``app/sync/bars.py::BarsSyncer`` 拉 **1d 日线**。
分钟线 / 逐笔 / 财务 / 板块成分 / 股本 / 资金流要么只能实时查询、要么压根不存在——
**不是数据源没能力**（``tdx`` 源声明了 15 项能力、``broker`` 有财务/板块接口），
而是**没有「数据集」这个概念**去把它们组织成可调度、可断点、可溯源的下载任务。

本模块只做一件事：把「能下载什么」固化成一张表。下载器
（``app/sync/datasets.py::DatasetSyncer``）、调度播种、REST API、前端页面**全部**
读这张表——新增一种数据只需在这里加一行，四处自动跟上。

## 三个必须记住的约定

1. **源链即策略**：``chain`` 声明的优先级就是「QMT 优先、无券商降级第三方」。
   ``broker`` 是券商（xtquant）源 id，链首挂它即表示「券商可用就用券商」。
   运行时由 ``ProviderCatalog.resolve_chain`` 做**能力校验 + 熔断 + 商用许可**过滤，
   所以链里写了某源不代表它一定会被选中——**声明是偏好，选中看运行时**。
   这是既有机制，本模块不重新发明，只是把 per-dataset 的链也纳入同一套治理。

2. **游标语义分三种**，混用会造出假成功（见 :class:`DataSetSpec` 的 ``cursor``）：
   - ``bar_date``：K 线。有「最早/最新一根」概念 ⇒ 可断点续传、可增量跳过；
   - ``snapshot``：参考数据（列表/板块/股本）。**没有时间序列**，只能整批覆盖刷新，
     靠 ``last_sync_at`` + TTL 判定要不要重跑。对它做「增量」是伪需求；
   - ``event_date``：tick / 分时。**按交易日切片**，一天一份，天然按日滚动过期。

3. **保留窗口不是可选的**：全市场 1 分钟线约 **3 亿行/年**，不设窗口会把磁盘和
   主库 WAL 一起撑爆（TD-25 的教训：主库 585MB 就让全量回归跑 11 分钟）。
   分钟线一律走**独立 SQLite 文件**（``datasource/intraday_store.py``），
   日线留在主库。``retention_days=0`` 表示永久保留，只允许低频数据集使用。

## 与能力链的关系（不要绕过）

``capability`` 字段必须能在 ``datasource/providers.py::DEFAULT_CAPABILITY_CHAINS``
里找到，否则 ``resolve_chain`` 拿不到链、同步必然空转。护栏
``tests/test_capability_chain_unity.py`` 双向锁死两侧，新增数据集请同步补链。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: 数据集类别。前端按此分组展示，调度按此决定默认时段。
CAT_BARS = "bars"                # K 线（日/周/月/分钟）
CAT_TICK = "tick"                # 逐笔成交
CAT_INTRADAY = "intraday"        # 当日分时
CAT_REFERENCE = "reference"      # 参考数据（列表 / 板块 / 成分 / 股本 / ETF）
CAT_FUNDAMENTAL = "fundamental"  # 财务 / 基本面
CAT_FLOW = "flow"                # 资金流
CAT_CALENDAR = "calendar"        # 交易日历

CATEGORY_LABEL = {
    CAT_BARS: "K 线",
    CAT_TICK: "逐笔",
    CAT_INTRADAY: "分时",
    CAT_REFERENCE: "参考数据",
    CAT_FUNDAMENTAL: "财务",
    CAT_FLOW: "资金流",
    CAT_CALENDAR: "日历",
}

#: 游标语义（决定同步器怎么判定「要不要下载」）。
CUR_BAR_DATE = "bar_date"        # 按 K 线日期：支持全量翻页 + 增量跳过
CUR_SNAPSHOT = "snapshot"        # 整体快照：按 last_sync_at + TTL 刷新
CUR_REPORT = "report_period"     # 按报告期（财务）
CUR_EVENT = "event_date"         # 按交易日切片（tick / 分时）

#: 存储目标。``intraday`` 特判走独立 SQLite 文件，其余为主库表名。
STORE_MAIN_BARS = "local_bars"
STORE_INTRADAY = "intraday"

#: **无需 provider 注册**即可工作的内置源：即使 ``chain_for()`` 解析为空，
#: 该数据集也确实能同步成功（实现走本地/内置，不依赖网络）。
#:
#: 为什么需要它：``ProviderCatalog.resolve_chain`` 会过滤掉未注册的源，而
#: ``local``（内置交易日历）不是注册 provider ⇒ ``calendar`` 的解析链恒为空。
#: 若前端据此显示「无可用源」，就是在**假告警**——日历其实每次都能同步成功
#: （见 ``DatasetSyncer._fetch_calendar`` 的内置回退）。这份名单让「解析为空」
#: 与「真的没人能供数」区分开。
BUILTIN_SOURCES: dict[str, str] = {
    "local": "内置（不依赖网络）",
}


@dataclass(frozen=True)
class DataSetSpec:
    """一个「可下载数据集」的完整声明。

    Attributes:
        id: 唯一标识，API / 调度 / 前端共用（如 ``bars_1d``）。
        label: 中文名。
        category: :data:`CAT_BARS` 等类别。
        capability: 依赖的数据源能力 id，必须在 ``DEFAULT_CAPABILITY_CHAINS`` 中。
        chain: 源优先级链。**链首 = 首选**；``broker`` 即 QMT 券商源。
        store: 落库目标。主库表名，或 :data:`STORE_INTRADAY`（独立文件）。
        cursor: 游标语义 :data:`CUR_BAR_DATE` 等。
        period: K 线周期（非 K 线数据集为 ``""``）。
        supports_range: 源是否支持**区间**拉取（支持 ⇒ 全量可逐年翻页；
            不支持 ⇒ 只能按 count 拉最近 N 根，全量退化为单次大 count）。
        lookback: 增量模式下默认拉最近 N 根。
        retention_days: 保留窗口（自然日）。``0`` = 永久。
        cron: 默认调度 cron（5 段，本地时区）。
        default_enabled: 是否默认播种调度。分钟线默认关——数据量太大，
            且多数用户只需要日线。
        adjust: K 线复权方式（``""`` / ``qfq`` / ``hfq``）。
        unit_note: 单位/口径备注。**跨源单位不一致是长期事故源**
            （如 TDX 逐笔 vol=手、K 线 vol=股），在此显式登记。
        tags: 展示/筛选标记（``core`` / ``heavy`` / ``broker_only``）。
    """

    id: str
    label: str
    category: str
    capability: str
    chain: tuple[str, ...]
    store: str
    cursor: str
    period: str = ""
    supports_range: bool = True
    lookback: int = 320
    retention_days: int = 0
    cron: str = "0 16 * * 1-5"
    default_enabled: bool = True
    adjust: str = ""
    unit_note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        """API / 前端视图。不含任何「当前有没有数据」的运行时判断。"""
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "category_label": CATEGORY_LABEL.get(self.category, self.category),
            "capability": self.capability,
            "chain": list(self.chain),
            "store": self.store,
            "cursor": self.cursor,
            "period": self.period,
            "supports_range": self.supports_range,
            "lookback": self.lookback,
            "retention_days": self.retention_days,
            "cron": self.cron,
            "default_enabled": self.default_enabled,
            "adjust": self.adjust,
            "unit_note": self.unit_note,
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# 数据集注册表（唯一真源）
# ---------------------------------------------------------------------------
# 源 id 约定：
#   broker = QMT 券商（xtquant）  tdx = easy_tdx 公共行情
#   akshare / baostock = 可选依赖（未安装时 resolve_chain 自动过滤）
#   local = 本地（交易日历等无需远程的能力）
#
# ★ 链的排列就是业务策略：**券商优先**。QMT 开着就走券商数据（最快、含复权与
#   财务），QMT 没开则自动降级到 tdx，再降级到 akshare/baostock。
#   这一行为由 ProviderCatalog.resolve_chain 在运行时兑现，本表只表达意图。
DATASETS: dict[str, DataSetSpec] = {}


def _ds(*specs: DataSetSpec) -> None:
    for s in specs:
        if s.id in DATASETS:
            raise ValueError(f"duplicate dataset id: {s.id}")
        DATASETS[s.id] = s


_ds(
    # ---------------- K 线 ----------------
    DataSetSpec(
        id="bars_1d", label="日线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx", "baostock", "akshare", "tencent", "sina"),
        store=STORE_MAIN_BARS, cursor=CUR_BAR_DATE, period="1d",
        supports_range=True, lookback=320, retention_days=0,
        cron="0 16 * * 1-5", default_enabled=True, adjust="qfq",
        unit_note="volume=股，amount=元",
        tags=("core",),
    ),
    DataSetSpec(
        id="bars_1w", label="周线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx", "baostock", "akshare"),
        store=STORE_MAIN_BARS, cursor=CUR_BAR_DATE, period="1w",
        supports_range=True, lookback=160, retention_days=0,
        cron="10 16 * * 5", default_enabled=True, adjust="qfq",
        unit_note="volume=股",
    ),
    DataSetSpec(
        id="bars_1mo", label="月线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx", "baostock", "akshare"),
        store=STORE_MAIN_BARS, cursor=CUR_BAR_DATE, period="1mo",
        supports_range=True, lookback=120, retention_days=0,
        cron="15 16 1 * *", default_enabled=True, adjust="qfq",
        unit_note="volume=股",
    ),
    # ---------------- 分钟线（独立库，默认关闭） ----------------
    DataSetSpec(
        id="bars_1m", label="1 分钟线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx"), store=STORE_INTRADAY, cursor=CUR_BAR_DATE,
        period="1m", supports_range=False, lookback=2000, retention_days=120,
        cron="30 15 * * 1-5", default_enabled=False,
        unit_note="volume=股；全市场约 3 亿行/年，务必保留窗口",
        tags=("heavy",),
    ),
    DataSetSpec(
        id="bars_5m", label="5 分钟线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx"), store=STORE_INTRADAY, cursor=CUR_BAR_DATE,
        period="5m", supports_range=False, lookback=1000, retention_days=250,
        cron="35 15 * * 1-5", default_enabled=False,
        unit_note="volume=股",
        tags=("heavy",),
    ),
    DataSetSpec(
        id="bars_15m", label="15 分钟线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx"), store=STORE_INTRADAY, cursor=CUR_BAR_DATE,
        period="15m", supports_range=False, lookback=800, retention_days=500,
        cron="40 15 * * 1-5", default_enabled=False,
        unit_note="volume=股",
        tags=("heavy",),
    ),
    DataSetSpec(
        id="bars_30m", label="30 分钟线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx"), store=STORE_INTRADAY, cursor=CUR_BAR_DATE,
        period="30m", supports_range=False, lookback=600, retention_days=730,
        cron="45 15 * * 1-5", default_enabled=False,
        unit_note="volume=股",
    ),
    DataSetSpec(
        id="bars_60m", label="60 分钟线", category=CAT_BARS, capability="kline",
        chain=("broker", "tdx"), store=STORE_INTRADAY, cursor=CUR_BAR_DATE,
        period="60m", supports_range=False, lookback=500, retention_days=730,
        cron="50 15 * * 1-5", default_enabled=False,
        unit_note="volume=股",
    ),
    # ---------------- 逐笔 / 分时 ----------------
    DataSetSpec(
        id="ticks", label="逐笔成交", category=CAT_TICK, capability="ticks",
        # ★ 刻意不挂 broker：券商 L2 逐笔是另一个端点（/market/l2，未连接 503），
        #   这条链的存在意义就是「无券商也能拿到真实成交流」。
        chain=("tdx",), store="local_ticks", cursor=CUR_EVENT,
        supports_range=False, lookback=500, retention_days=7,
        cron="20 15 * * 1-5", default_enabled=False,
        unit_note="volume=手（注意：与 K 线的股不同）",
        tags=("heavy",),
    ),
    DataSetSpec(
        id="minutes", label="当日分时", category=CAT_INTRADAY, capability="minutes",
        chain=("tdx",), store="local_minutes", cursor=CUR_EVENT,
        supports_range=False, lookback=240, retention_days=7,
        cron="25 15 * * 1-5", default_enabled=False,
        unit_note="volume=股",
        tags=("heavy",),
    ),
    # ---------------- 参考数据 ----------------
    DataSetSpec(
        id="stock_list", label="证券列表", category=CAT_REFERENCE,
        capability="stock_list", chain=("broker", "tdx", "baostock", "akshare"),
        store="local_stock_list", cursor=CUR_SNAPSHOT, supports_range=False,
        retention_days=0, cron="0 9 * * 1-5", default_enabled=True,
        unit_note="全市场代码 + 名称，其他数据集的 universe 来源",
        tags=("core",),
    ),
    DataSetSpec(
        id="etf_list", label="ETF 列表", category=CAT_REFERENCE,
        capability="etf_list", chain=("tdx",),
        store="local_stock_list", cursor=CUR_SNAPSHOT, supports_range=False,
        retention_days=0, cron="5 9 * * 1-5", default_enabled=True,
        unit_note="仅 tdx 源提供 ETF 列表",
    ),
    DataSetSpec(
        id="boards", label="板块列表", category=CAT_REFERENCE,
        capability="sector", chain=("broker", "tdx"),
        store="local_boards", cursor=CUR_SNAPSHOT, supports_range=False,
        retention_days=0, cron="10 9 * * 1-5", default_enabled=True,
        unit_note="行业 / 概念板块",
    ),
    DataSetSpec(
        id="board_members", label="板块成分", category=CAT_REFERENCE,
        capability="index_constituent", chain=("broker", "tdx", "akshare"),
        store="local_board_members", cursor=CUR_SNAPSHOT, supports_range=False,
        retention_days=0, cron="30 9 * * 1-5", default_enabled=False,
        unit_note="板块 → 成分股双向索引（对标 free-stockdb 的 bk.get）",
        tags=("heavy",),
    ),
    DataSetSpec(
        id="capital", label="股本结构", category=CAT_REFERENCE,
        capability="capital", chain=("broker", "tdx"),
        store="local_capital", cursor=CUR_SNAPSHOT, supports_range=False,
        retention_days=0, cron="20 9 * * 1-5", default_enabled=True,
        unit_note="总股本 / 流通股本",
    ),
    # ---------------- 财务 ----------------
    DataSetSpec(
        id="financial", label="财务数据", category=CAT_FUNDAMENTAL,
        capability="fundamental", chain=("broker",),
        store="local_fundamentals", cursor=CUR_REPORT, supports_range=False,
        retention_days=0, cron="0 20 * * 1-5", default_enabled=True,
        unit_note="仅券商源提供（fundamental 链当前只有 broker）",
        tags=("broker_only",),
    ),
    # ---------------- 资金流 ----------------
    DataSetSpec(
        id="moneyflow", label="资金流", category=CAT_FLOW,
        capability="moneyflow", chain=("broker", "tdx"),
        store="local_moneyflow_hist", cursor=CUR_EVENT, supports_range=False,
        retention_days=90, cron="55 15 * * 1-5", default_enabled=False,
        unit_note="与 moneyflow_cache 的盘中采样不同：这是收盘后的历史沉淀",
        tags=("heavy",),
    ),
    # ---------------- 日历 ----------------
    DataSetSpec(
        id="calendar", label="交易日历", category=CAT_CALENDAR,
        capability="calendar", chain=("broker", "local"),
        store="exchange_calendar", cursor=CUR_SNAPSHOT, supports_range=False,
        retention_days=0, cron="0 8 1 * *", default_enabled=True,
        unit_note="本地内置 2024-2026；券商可用时用券商日历覆盖",
    ),
)


def get(dataset_id: str) -> Optional[DataSetSpec]:
    return DATASETS.get(dataset_id)


def require(dataset_id: str) -> DataSetSpec:
    """取数据集；不存在抛 ``KeyError``（调用方转 400）。"""
    spec = DATASETS.get(dataset_id)
    if spec is None:
        raise KeyError(dataset_id)
    return spec


def enabled_by_default() -> list[DataSetSpec]:
    return [s for s in DATASETS.values() if s.default_enabled]


# 注：``by_category`` / ``ids`` / ``kline_ids`` 曾在此暴露，但全仓库零调用
# （前端自行按 category 分组，调度只消费 enabled_by_default，路由只消费 get/
# require），按「无孤儿逻辑」原则移除。真需要时 ``sorted(DATASETS)`` /
# ``[s for s in DATASETS.values() if s.category == CAT_BARS]`` 一行即可，
# 不必为了这种过滤再维护一个导出符号。

__all__ = [
    "CAT_BARS", "CAT_CALENDAR", "CAT_FLOW", "CAT_FUNDAMENTAL", "CAT_INTRADAY",
    "CAT_REFERENCE", "CAT_TICK", "CATEGORY_LABEL", "CUR_BAR_DATE", "CUR_EVENT",
    "CUR_REPORT", "CUR_SNAPSHOT", "DATASETS", "STORE_INTRADAY", "STORE_MAIN_BARS",
    "BUILTIN_SOURCES", "DataSetSpec", "enabled_by_default", "get", "require",
]
