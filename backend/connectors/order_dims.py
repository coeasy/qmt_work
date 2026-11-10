"""多维度下单：维度轴 × 通道能力矩阵 × 原生调用规划。

**为什么需要这一层**

同一个"买入 100 股"的意图，在四条通道上的表达完全不同：

* C 通道（客户端内 Python 策略）有 29 个交易函数，支持 6 种数量语义、
  单账号/账号组/组合/套利四种作用域、14 种选价类型、算法单与触价单；
* 文件单（CSV/DBF/批量）只能表达"代码 + 方向 + 数量 + 价格"几列；
* ``xttrader`` 能力接近 C，但本机版本锁死；
* ``callFormula`` 只能把命令投给**已存在**的公式实例。

如果每个通道各自拼入参，维度一多就必然出现"某通道悄悄忽略了一个维度"，
也就是本项目反复强调要避免的**假绿灯**。所以这里把维度轴显式声明、
把每个通道的能力显式声明，**任何不被支持的维度都直接报错**。

**权威依据**（不猜，全部来自随客户端下发的官方文档）

``HTML/guosenPythonApiHelp/iQuant_Python_API_Doc.html``（v3.3.5）给出三张表：

* ``opType`` —— 操作类型（0–15 期货、23/24 股票、25–34 组合与两融、50–59 期权、60 ETF 申购）
* ``orderType`` —— 下单方式（11xx 单账号 / 12xx 账号组 / 21xx 组合单账号 /
  22xx 组合账号组 / 233x 组合套利），**数量语义本身就编码在这里**
* ``prType`` —— 选价类型（-1 与 0–14、27–29、42–49），**部分取值有交易所/品种限制**

**约定（与仓库既有纪律一致）**

* 枚举解析一律严格：未知值抛 :class:`DimensionError`，不取默认值。
* 能力矩阵只写有证据的取值，证据见模块底部 ``EVIDENCE``。
* 降级只能显式请求，且结果里必须带说明；**不允许静默忽略维度**。

本模块不导入任何 SDK，纯数据 + 纯函数。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class DimensionError(ValueError):
    """维度取值非法（未知枚举值 / 缺必填维度 / 维度间不自洽）。"""


class UnsupportedDimension(DimensionError):
    """该通道不支持这个维度取值 —— 必须显式失败，不允许静默忽略。"""

    def __init__(self, channel: str, axis: str, value: Any, reason: str = ""):
        self.channel = channel
        self.axis = axis
        self.value = value
        self.reason = reason
        detail = f"通道 {channel} 不支持 {axis}={value!r}"
        if reason:
            detail += f"（{reason}）"
        super().__init__(detail)


# ---------------------------------------------------------------------------
# 维度轴
# ---------------------------------------------------------------------------
class _Axis(str, enum.Enum):
    """所有维度轴的基类：``str`` 子类，便于 JSON 直出。"""

    @classmethod
    def parse(cls, value: Any) -> "_Axis":
        """严格解析。未知值一律报错，**不**回落到任何默认值。"""
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text or member.name.lower() == text:
                return member
        allowed = ", ".join(m.value for m in cls)
        raise DimensionError(f"{cls.__name__} 不接受 {value!r}；允许值：{allowed}")


class Side(_Axis):
    BUY = "buy"
    SELL = "sell"
    MARGIN_BUY = "margin_buy"        # 27 融资买入
    SHORT_SELL = "short_sell"        # 28 融券卖出
    BUY_TO_COVER = "buy_to_cover"    # 29 买券还券
    SELL_TO_REPAY = "sell_to_repay"  # 31 卖券还款


class QtySemantics(_Axis):
    """数量语义 —— "100" 到底是 100 股、100 手，还是 100 元。"""

    SHARES = "shares"
    LOTS = "lots"
    VALUE = "value"
    PERCENT = "percent"
    TARGET_VALUE = "target_value"
    TARGET_PERCENT = "target_percent"


class PriceType(_Axis):
    """选价类型，取值与官方 ``prType`` 表一一对应（见 :data:`PRTYPE`）。"""

    DEFAULT = "default"                  # -1 用交易面板设置
    SELL5 = "sell5"
    SELL4 = "sell4"
    SELL3 = "sell3"
    SELL2 = "sell2"
    SELL1 = "sell1"
    LATEST = "latest"
    BUY1 = "buy1"
    BUY2 = "buy2"
    BUY3 = "buy3"
    BUY4 = "buy4"
    BUY5 = "buy5"
    LIMIT = "limit"                      # 11 指定价/模型价（仅单股）
    PRICE_LIMIT = "price_limit"          # 12 涨跌停价
    HANG = "hang"                        # 13 挂单价
    PEER = "peer"                        # 14 对手价
    OPT_MARKET_CANCEL = "opt_market_cancel"        # 27 期权
    OPT_MARKET_FOK = "opt_market_fok"              # 28 期权
    OPT_MARKET_TO_LIMIT = "opt_market_to_limit"    # 29 期权
    SH_BEST5_CANCEL = "sh_best5_cancel"            # 42 上交所
    SH_BEST5_TO_LIMIT = "sh_best5_to_limit"        # 43 上交所
    SZ_PEER_BEST = "sz_peer_best"                  # 44 深交所
    SZ_OWN_BEST = "sz_own_best"                    # 45 深交所
    SZ_IOC = "sz_ioc"                              # 46 深交所
    SZ_BEST5_CANCEL = "sz_best5_cancel"            # 47 深交所
    SZ_FOK = "sz_fok"                              # 48 深交所
    STAR_AFTER_HOURS = "star_after_hours"          # 49 科创板盘后定价


class TimeInForce(_Axis):
    """时效。官方没有独立的 TIF 轴 —— IOC/FOK 由 ``prType`` 表达，
    因此本轴与 :class:`PriceType` 必须自洽（见 :func:`_check_tif`）。"""

    DAY = "day"
    IOC = "ioc"
    FOK = "fok"


class ExecMode(_Axis):
    IMMEDIATE = "immediate"    # passorder / order_*
    ALGO = "algo"              # algo_passorder（算法交易，可拆单）
    SMART_ALGO = "smart_algo"  # smart_algo_passorder（智能算法）
    TASK = "task"              # algo_passorder + UseTrigger（触价单/条件单）


class InstrumentClass(_Axis):
    STOCK = "stock"
    ETF = "etf"
    BOND = "bond"
    OPTION = "option"
    FUTURE = "future"
    CREDIT = "credit"


class AccountScope(_Axis):
    SINGLE = "single"                  # 11xx 单账号
    MULTI = "multi"                    # 12xx 账号组（无权重）
    PORTFOLIO = "portfolio"            # 21xx 组合 / 单账号
    PORTFOLIO_GROUP = "portfolio_group"  # 22xx 组合 / 账号组
    ARBITRAGE = "arbitrage"            # 233x 组合套利（股票篮子 + 期货合约）

    @property
    def is_group(self) -> bool:
        """该作用域下 accountID 位填的是**组名**而不是资金账号。"""
        return self in (AccountScope.MULTI, AccountScope.PORTFOLIO_GROUP,
                        AccountScope.ARBITRAGE)

    @property
    def is_combo(self) -> bool:
        """该作用域下 orderCode 位填的是**篮子名**而不是单只合约。"""
        return self in (AccountScope.PORTFOLIO, AccountScope.PORTFOLIO_GROUP,
                        AccountScope.ARBITRAGE)


class Channel(_Axis):
    XT_STRATEGY = "xt_strategy"   # C：客户端内 Python 策略
    FILE_CSV = "file_csv"         # F：CsvMonitorSetting 监控目录
    FILE_DBF = "file_dbf"         # F：DbfMonitorSetting 监控目录
    BATCH_ORDER = "batch_order"   # F：batchOrder/orderFile.csv
    XTTRADER = "xttrader"         # A：共享内存（本机版本锁死）
    CALL_FORMULA = "call_formula" # B：58670 callFormula（只能驱动已存在公式）


# ---------------------------------------------------------------------------
# 官方表：opType（操作类型）
# ---------------------------------------------------------------------------
#: 股票普通买卖。
OPTYPE_STOCK = {Side.BUY: 23, Side.SELL: 24}

#: 信用账号的**普通**买卖（担保品买卖）。
OPTYPE_CREDIT_PLAIN = {Side.BUY: 33, Side.SELL: 34}

#: 两融专用操作。
OPTYPE_CREDIT = {
    Side.MARGIN_BUY: 27,
    Side.SHORT_SELL: 28,
    Side.BUY_TO_COVER: 29,
    Side.SELL_TO_REPAY: 31,
}

#: 组合交易。
OPTYPE_COMBO = {Side.BUY: 25, Side.SELL: 26}

#: 期货六键（精确开平）。
OPTYPE_FUTURE_SIX = {
    (Side.BUY, "open"): 0,
    (Side.BUY, "close_yday"): 1,
    (Side.BUY, "close_tday"): 2,
    (Side.SELL, "open"): 3,
    (Side.SELL, "close_yday"): 4,
    (Side.SELL, "close_tday"): 5,
}

#: 期货四键（优先平今/平昨）。
OPTYPE_FUTURE_FOUR = {
    (Side.BUY, "close_tday_first"): 6,
    (Side.BUY, "close_yday_first"): 7,
    (Side.SELL, "close_tday_first"): 8,
    (Side.SELL, "close_yday_first"): 9,
}

#: 期货两键（先平后开）。
OPTYPE_FUTURE_TWO = {
    (Side.SELL, "close_tday_first"): 10,
    (Side.SELL, "close_yday_first"): 11,
    (Side.BUY, "close_tday_first"): 12,
    (Side.BUY, "close_yday_first"): 13,
    (Side.BUY, "no_close"): 14,
    (Side.SELL, "no_close"): 15,
}

#: 期权。
OPTYPE_OPTION = {
    (Side.BUY, "open"): 50,
    (Side.SELL, "close"): 51,
    (Side.SELL, "open"): 52,
    (Side.BUY, "close"): 53,
    (Side.BUY, "covered_open"): 54,
    (Side.BUY, "covered_close"): 55,
}

#: ETF 申购（opType=60，volume 单位为"申购单位"）。
OPTYPE_ETF_PURCHASE = 60


# ---------------------------------------------------------------------------
# 官方表：orderType（下单方式 —— 数量语义编码在这里）
# ---------------------------------------------------------------------------
#: 单股 / 单账号。
ORDERTYPE_SINGLE = {
    QtySemantics.SHARES: 1101,   # 股/手
    QtySemantics.LOTS: 1101,     # 股/手
    QtySemantics.VALUE: 1102,    # 金额（元），只支持股票
    QtySemantics.PERCENT: 1123,  # 可用比例 [0,1]
    QtySemantics.TARGET_PERCENT: 1113,  # 总资产比例 [0,1]
}

#: 单股 / 账号组（无权重）。
ORDERTYPE_GROUP = {
    QtySemantics.SHARES: 1201,
    QtySemantics.LOTS: 1201,
    QtySemantics.VALUE: 1202,
    QtySemantics.PERCENT: 1223,
    QtySemantics.TARGET_PERCENT: 1213,
}

#: 组合 / 单账号。
ORDERTYPE_COMBO_SINGLE = {
    QtySemantics.SHARES: 2101,   # 按篮子设定数量，volume = 份数
    QtySemantics.VALUE: 2102,    # 按篮子权重，volume = 元
    QtySemantics.PERCENT: 2103,  # 按账号可用
}

#: 组合 / 账号组。
ORDERTYPE_COMBO_GROUP = {
    QtySemantics.SHARES: 2201,
    QtySemantics.VALUE: 2202,
    QtySemantics.PERCENT: 2203,
}

#: 组合套利（233x）。
ORDERTYPE_ARBITRAGE = {
    QtySemantics.SHARES: 2331,
    QtySemantics.VALUE: 2332,
    QtySemantics.PERCENT: 2333,
}


def order_type(scope: AccountScope, qty: QtySemantics) -> int:
    """按官方表解出 ``orderType``。

    作用域与表的对应关系是**一对一**的（官方文档原文）：

    ==================  ==========  ==============================
    account_scope       表          官方释义
    ==================  ==========  ==============================
    single              11xx        单股、单账号
    multi               12xx        单股、账号组（无权重）
    portfolio           21xx        组合、单账号
    portfolio_group     22xx        组合、账号组（无权重）
    arbitrage           233x        组合、套利
    ==================  ==========  ==============================

    表里没有的组合一律报错 —— 不猜相邻编号。
    """
    if scope is AccountScope.ARBITRAGE:
        table = ORDERTYPE_ARBITRAGE
    elif scope is AccountScope.PORTFOLIO:
        table = ORDERTYPE_COMBO_SINGLE
    elif scope is AccountScope.PORTFOLIO_GROUP:
        table = ORDERTYPE_COMBO_GROUP
    elif scope is AccountScope.MULTI:
        table = ORDERTYPE_GROUP
    else:
        table = ORDERTYPE_SINGLE
    value = table.get(qty)
    if value is None:
        raise DimensionError(
            f"官方 orderType 表无 (scope={scope.value}, qty={qty.value}) 这一格；"
            "请勿臆造编号，改用受支持的组合")
    return value


# ---------------------------------------------------------------------------
# 官方表：prType（选价类型）
# ---------------------------------------------------------------------------
PRTYPE = {
    PriceType.DEFAULT: -1,
    PriceType.SELL5: 0, PriceType.SELL4: 1, PriceType.SELL3: 2,
    PriceType.SELL2: 3, PriceType.SELL1: 4,
    PriceType.LATEST: 5,
    PriceType.BUY1: 6, PriceType.BUY2: 7, PriceType.BUY3: 8,
    PriceType.BUY4: 9, PriceType.BUY5: 10,
    PriceType.LIMIT: 11,
    PriceType.PRICE_LIMIT: 12,
    PriceType.HANG: 13,
    PriceType.PEER: 14,
    PriceType.OPT_MARKET_CANCEL: 27,
    PriceType.OPT_MARKET_FOK: 28,
    PriceType.OPT_MARKET_TO_LIMIT: 29,
    PriceType.SH_BEST5_CANCEL: 42,
    PriceType.SH_BEST5_TO_LIMIT: 43,
    PriceType.SZ_PEER_BEST: 44,
    PriceType.SZ_OWN_BEST: 45,
    PriceType.SZ_IOC: 46,
    PriceType.SZ_BEST5_CANCEL: 47,
    PriceType.SZ_FOK: 48,
    PriceType.STAR_AFTER_HOURS: 49,
}

#: 只在上交所有效的选价类型。
_PR_SH_ONLY = frozenset({
    PriceType.SH_BEST5_CANCEL, PriceType.SH_BEST5_TO_LIMIT,
})

#: 只在深交所有效的选价类型。
_PR_SZ_ONLY = frozenset({
    PriceType.SZ_PEER_BEST, PriceType.SZ_OWN_BEST, PriceType.SZ_IOC,
    PriceType.SZ_BEST5_CANCEL, PriceType.SZ_FOK,
})

#: 只对股票期权有效。
_PR_OPTION_ONLY = frozenset({
    PriceType.OPT_MARKET_CANCEL, PriceType.OPT_MARKET_FOK,
    PriceType.OPT_MARKET_TO_LIMIT,
})

#: 表达 IOC 的选价类型。
_PR_IOC = frozenset({
    PriceType.SZ_IOC, PriceType.OPT_MARKET_CANCEL,
    PriceType.SH_BEST5_CANCEL, PriceType.SZ_BEST5_CANCEL,
})

#: 表达 FOK 的选价类型。
_PR_FOK = frozenset({PriceType.SZ_FOK, PriceType.OPT_MARKET_FOK})

#: 辅助函数（``order_*`` / ``buy_open``）使用的 ``style`` 字符串。
#: 官方只列了这几个；其余一律报错，不臆造。
STYLE = {
    PriceType.LATEST: "LATEST",
    PriceType.LIMIT: "FIX",
    PriceType.HANG: "HANG",
    PriceType.PEER: "COMPETE",
    PriceType.SELL5: "SALE5", PriceType.SELL4: "SALE4", PriceType.SELL3: "SALE3",
    PriceType.SELL2: "SALE2", PriceType.SELL1: "SALE1",
}


def prtype(price_type: PriceType, *, exchange: str = "",
           instrument_class: InstrumentClass = InstrumentClass.STOCK,
           combo: bool = False) -> int:
    """按官方表解出 ``prType``，并校验交易所/品种/组合限制。

    ★ 为什么必须校验：42/43 只在上交所、44–48 只在深交所、27–29 只对期权、
      11（模型价）官方注明"只对单股情况支持，对组合交易不支持"。
      不校验就会"下单成功但被柜台拒"，比直接报错更难排查。
    """
    value = PRTYPE.get(price_type)
    if value is None:
        raise DimensionError(f"官方 prType 表无 {price_type.value!r}")

    ex = (exchange or "").upper()
    if price_type in _PR_SH_ONLY and ex and ex != "SH":
        raise DimensionError(
            f"prType={value}({price_type.value}) 仅对上交所有效，当前交易所={ex}")
    if price_type in _PR_SZ_ONLY and ex and ex != "SZ":
        raise DimensionError(
            f"prType={value}({price_type.value}) 仅对深交所有效，当前交易所={ex}")
    if price_type in _PR_OPTION_ONLY and instrument_class is not InstrumentClass.OPTION:
        raise DimensionError(
            f"prType={value}({price_type.value}) 仅对股票期权有效，"
            f"当前品种={instrument_class.value}")
    if price_type is PriceType.LIMIT and combo:
        raise DimensionError(
            "prType=11（模型价/指定价）官方注明只对单股情况支持，对组合交易不支持")
    return value


def _check_tif(tif: TimeInForce, price_type: PriceType) -> None:
    """``tif`` 与 ``price_type`` 必须自洽 —— 时效由 prType 表达，没有独立入参。"""
    if tif is TimeInForce.DAY:
        if price_type in _PR_IOC or price_type in _PR_FOK:
            raise DimensionError(
                f"tif=day 但 price_type={price_type.value} 本身就是即时类委托；"
                "要么改 tif，要么换选价类型")
        return
    if tif is TimeInForce.IOC and price_type not in _PR_IOC:
        raise DimensionError(
            f"tif=ioc 需要能表达 IOC 的选价类型（"
            + "/".join(sorted(p.value for p in _PR_IOC))
            + f"），当前 price_type={price_type.value}")
    if tif is TimeInForce.FOK and price_type not in _PR_FOK:
        raise DimensionError(
            f"tif=fok 需要能表达 FOK 的选价类型（"
            + "/".join(sorted(p.value for p in _PR_FOK))
            + f"），当前 price_type={price_type.value}")


# ---------------------------------------------------------------------------
# 意图
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderIntent:
    """多维度的下单意图。

    这是**意图**，不是某一通道的入参。通道入参由 :func:`plan` 生成。
    """

    code: str
    side: Side | str
    quantity: float = 0.0
    qty_semantics: QtySemantics | str = QtySemantics.SHARES
    price_type: PriceType | str = PriceType.LATEST
    price: float = 0.0
    tif: TimeInForce | str = TimeInForce.DAY
    exec_mode: ExecMode | str = ExecMode.IMMEDIATE
    instrument_class: InstrumentClass | str = InstrumentClass.STOCK
    account_scope: AccountScope | str = AccountScope.SINGLE
    exchange: str = ""
    account_id: str = ""
    account_type: str = ""
    strategy_name: str = ""
    remark: str = ""
    client_order_id: str = ""
    #: 算法单参数（``algo_passorder`` 的 ``userOrderParam``）
    algo: dict[str, Any] = field(default_factory=dict)
    #: 任务/触价单参数（合并进 ``userOrderParam`` 的 UseTrigger/TriggerType/TriggerPrice）
    task: dict[str, Any] = field(default_factory=dict)
    #: 期货/期权的开平动作：open / close_yday / close_tday / close_tday_first /
    #: close_yday_first / no_close / covered_open / covered_close
    position_action: str = ""
    #: 多账户时的账户组名（account_scope=multi 时是组名，不是账号列表）
    account_group: str = ""

    def __post_init__(self):
        object.__setattr__(self, "side", Side.parse(self.side))
        object.__setattr__(self, "qty_semantics", QtySemantics.parse(self.qty_semantics))
        object.__setattr__(self, "price_type", PriceType.parse(self.price_type))
        object.__setattr__(self, "tif", TimeInForce.parse(self.tif))
        object.__setattr__(self, "exec_mode", ExecMode.parse(self.exec_mode))
        object.__setattr__(self, "instrument_class",
                           InstrumentClass.parse(self.instrument_class))
        object.__setattr__(self, "account_scope", AccountScope.parse(self.account_scope))

        code = (self.code or "").strip()
        if not code:
            raise DimensionError("code 不能为空")
        object.__setattr__(self, "code", code)

        # 交易所：显式给出优先，否则从 600036.SH 的后缀取。
        ex = (self.exchange or "").strip().upper()
        if not ex and "." in code:
            ex = code.rsplit(".", 1)[-1].upper()
        object.__setattr__(self, "exchange", ex)

        if self.quantity is None or float(self.quantity) < 0:
            raise DimensionError(f"quantity 非法：{self.quantity!r}")
        object.__setattr__(self, "quantity", float(self.quantity))

        if self.qty_semantics in (QtySemantics.PERCENT, QtySemantics.TARGET_PERCENT):
            if not (0 < float(self.quantity) <= 1):
                raise DimensionError(
                    f"{self.qty_semantics.value} 语义下 quantity 必须是 (0,1] 的比例，"
                    f"收到 {self.quantity!r}")
        if self.account_scope in (AccountScope.MULTI, AccountScope.PORTFOLIO,
                                  AccountScope.ARBITRAGE) and not (
                self.account_group or self.account_id):
            raise DimensionError(
                f"account_scope={self.account_scope.value} 需要 account_group 或 "
                "account_id 给出组名/篮子名")
        if self.exec_mode is ExecMode.ALGO and not self.algo:
            raise DimensionError("exec_mode=algo 但 algo 参数为空")
        if self.exec_mode is ExecMode.SMART_ALGO and not self.algo:
            raise DimensionError("exec_mode=smart_algo 但 algo 参数为空")
        if self.exec_mode is ExecMode.TASK and not self.task:
            raise DimensionError("exec_mode=task 但 task 参数为空")

        _check_tif(self.tif, self.price_type)

        if self.instrument_class in (InstrumentClass.FUTURE, InstrumentClass.OPTION) \
                and not self.position_action:
            raise DimensionError(
                f"{self.instrument_class.value} 必须给出 position_action"
                "（open / close_tday / close_yday / close_tday_first / "
                "close_yday_first / no_close / covered_open / covered_close）")

    def with_(self, **kw) -> "OrderIntent":
        return replace(self, **kw)

    def axes(self) -> dict[str, str]:
        return {
            "side": self.side.value,
            "qty_semantics": self.qty_semantics.value,
            "price_type": self.price_type.value,
            "tif": self.tif.value,
            "exec_mode": self.exec_mode.value,
            "instrument_class": self.instrument_class.value,
            "account_scope": self.account_scope.value,
        }


# ---------------------------------------------------------------------------
# 通道能力
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Capability:
    """一个通道在每条维度轴上支持的取值集合。

    ``None`` = 该轴不限制；空 ``frozenset`` = 该轴完全不支持。
    """

    axes: dict[str, Optional[frozenset[str]]]
    note: str = ""

    def supports(self, axis: str, value: str) -> bool:
        allowed = self.axes.get(axis)
        if allowed is None:
            return True
        return value in allowed


_ALL = None  # 不限制

_CREDIT_SIDES = frozenset({
    Side.BUY.value, Side.SELL.value, Side.MARGIN_BUY.value,
    Side.SHORT_SELL.value, Side.BUY_TO_COVER.value, Side.SELL_TO_REPAY.value,
})

CAPABILITIES: dict[Channel, Capability] = {
    Channel.XT_STRATEGY: Capability(
        axes={
            "side": _CREDIT_SIDES,
            "qty_semantics": _ALL,          # 6 种（order_* 族 + orderType 表）
            "price_type": _ALL,             # 全部 prType
            "tif": _ALL,                    # 由 prType 表达
            "exec_mode": frozenset({m.value for m in ExecMode}),
            "instrument_class": frozenset({c.value for c in InstrumentClass}),
            "account_scope": frozenset({s.value for s in AccountScope}),
        },
        note="能力最全：29 个交易函数 + 4 种作用域 + 5 类主推回调",
    ),
    Channel.FILE_CSV: Capability(
        axes={
            "side": frozenset({Side.BUY.value, Side.SELL.value}),
            "qty_semantics": frozenset({QtySemantics.SHARES.value}),
            "price_type": frozenset({PriceType.LIMIT.value, PriceType.LATEST.value}),
            "tif": frozenset({TimeInForce.DAY.value}),
            "exec_mode": frozenset({ExecMode.IMMEDIATE.value}),
            "instrument_class": frozenset({
                InstrumentClass.STOCK.value, InstrumentClass.ETF.value,
                InstrumentClass.BOND.value,
            }),
            "account_scope": frozenset({AccountScope.SINGLE.value}),
        },
        note="列式文件：只能表达 代码/方向/数量/价格；无算法、无 IOC/FOK、无回报回调",
    ),
    Channel.FILE_DBF: Capability(
        axes={
            "side": frozenset({Side.BUY.value, Side.SELL.value}),
            "qty_semantics": frozenset({QtySemantics.SHARES.value}),
            "price_type": frozenset({PriceType.LIMIT.value, PriceType.LATEST.value}),
            "tif": frozenset({TimeInForce.DAY.value}),
            "exec_mode": frozenset({ExecMode.IMMEDIATE.value}),
            "instrument_class": frozenset({
                InstrumentClass.STOCK.value, InstrumentClass.ETF.value,
                InstrumentClass.BOND.value,
            }),
            "account_scope": frozenset({AccountScope.SINGLE.value}),
        },
        note="与 CSV 同构；额外支持 orderidfeedbackval 回写委托号",
    ),
    Channel.BATCH_ORDER: Capability(
        axes={
            "side": frozenset({Side.BUY.value, Side.SELL.value}),
            "qty_semantics": frozenset({QtySemantics.SHARES.value}),
            "price_type": frozenset({PriceType.LIMIT.value}),
            "tif": frozenset({TimeInForce.DAY.value}),
            "exec_mode": frozenset({ExecMode.IMMEDIATE.value}),
            "instrument_class": frozenset({InstrumentClass.STOCK.value,
                                           InstrumentClass.ETF.value}),
            "account_scope": frozenset({AccountScope.SINGLE.value}),
        },
        note="batchOrder/orderFile.csv：人工批量下单用的固定文件，key_enable 可做口令",
    ),
    Channel.XTTRADER: Capability(
        axes={
            "side": _CREDIT_SIDES,
            "qty_semantics": frozenset({QtySemantics.SHARES.value}),
            "price_type": _ALL,
            "tif": _ALL,
            "exec_mode": frozenset({ExecMode.IMMEDIATE.value}),
            "instrument_class": frozenset({c.value for c in InstrumentClass}),
            "account_scope": frozenset({AccountScope.SINGLE.value}),
        },
        note="能力接近 C，但本机 2026-08 桥 × 2021/2023 pyd 版本锁死，当前不可用",
    ),
    Channel.CALL_FORMULA: Capability(
        axes={
            "side": _ALL,
            "qty_semantics": _ALL,
            "price_type": _ALL,
            "tif": _ALL,
            "exec_mode": frozenset({ExecMode.IMMEDIATE.value}),
            "instrument_class": _ALL,
            "account_scope": _ALL,
        },
        note="只能把命令投递给**已存在**的公式实例；无 createFormula，不能独立下单",
    ),
}


# ---------------------------------------------------------------------------
# 规划结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    """某一通道上的原生调用方案。"""

    channel: Channel
    #: 原生函数名（passorder / order_shares / algo_passorder / order_stock …）
    func: str
    #: 位置实参（按该函数真实签名排好序）
    args: tuple = ()
    #: 关键字实参（仅 xttrader 这类关键字风格接口使用）
    kwargs: dict = field(default_factory=dict)
    #: 结构化载荷（文件单用）
    payload: dict = field(default_factory=dict)
    #: 解出的官方编号，便于日志与排障（opType/orderType/prType）
    codes: dict = field(default_factory=dict)
    #: 人读说明（含待实测项）
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.channel.value}:{self.func}"


@dataclass(frozen=True)
class Resolution:
    """一次多通道求解的结果。"""

    intent: OrderIntent
    plan: Optional[Plan]
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def report(self) -> str:
        head = "无可用通道" if self.plan is None else f"选用 {self.plan.describe()}"
        lines = [head]
        for ch, why in self.rejected:
            lines.append(f"  ✗ {ch}: {why}")
        if self.plan:
            if self.plan.codes:
                lines.append(f"  codes: {self.plan.codes}")
            for n in self.plan.notes:
                lines.append(f"  · {n}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 能力检查
# ---------------------------------------------------------------------------
_AXES = ("side", "qty_semantics", "price_type", "tif", "exec_mode",
         "instrument_class", "account_scope")


def check(intent: OrderIntent, channel: Channel) -> list[str]:
    """返回该通道**缺失**的维度说明列表；空列表 = 完全支持。"""
    cap = CAPABILITIES[channel]
    return [f"{axis}={value}" for axis, value in intent.axes().items()
            if not cap.supports(axis, value)]


def _require(intent: OrderIntent, channel: Channel) -> None:
    missing = check(intent, channel)
    if missing:
        raise UnsupportedDimension(channel.value, "&".join(
            m.split("=")[0] for m in missing),
            ",".join(m.split("=")[1] for m in missing), "该通道缺这些维度")


# ---------------------------------------------------------------------------
# C 通道：客户端内策略
# ---------------------------------------------------------------------------
#: 数量语义 → ``order_*`` 辅助函数名。
#: 官方签名统一为 ``fn(stockcode, amount[, style, price], ContextInfo[, accId])``。
HELPER_BY_QTY = {
    QtySemantics.SHARES: "order_shares",
    QtySemantics.LOTS: "order_lots",
    QtySemantics.VALUE: "order_value",
    QtySemantics.PERCENT: "order_percent",
    QtySemantics.TARGET_VALUE: "order_target_value",
    QtySemantics.TARGET_PERCENT: "order_target_percent",
}

#: 期货开平仓辅助函数（官方 6 个）。
FUTURE_HELPER = {
    (Side.BUY, "open"): "buy_open",
    (Side.SELL, "open"): "sell_open",
    (Side.BUY, "close_tday_first"): "buy_close_tdayfirst",
    (Side.BUY, "close_yday_first"): "buy_close_ydayfirst",
    (Side.SELL, "close_tday_first"): "sell_close_tdayfirst",
    (Side.SELL, "close_yday_first"): "sell_close_ydayfirst",
}


def _resolve_optype(intent: OrderIntent) -> int:
    """按品种与方向解出 ``opType``。表里没有就报错，不猜。"""
    ic, side, act = intent.instrument_class, intent.side, intent.position_action

    if ic is InstrumentClass.FUTURE:
        for table in (OPTYPE_FUTURE_SIX, OPTYPE_FUTURE_FOUR, OPTYPE_FUTURE_TWO):
            if (side, act) in table:
                return table[(side, act)]
        raise DimensionError(
            f"期货 opType 表无 (side={side.value}, action={act!r})")

    if ic is InstrumentClass.OPTION:
        if (side, act) in OPTYPE_OPTION:
            return OPTYPE_OPTION[(side, act)]
        raise DimensionError(
            f"期权 opType 表无 (side={side.value}, action={act!r})；"
            "期权必须先给出 position_action")

    if ic is InstrumentClass.CREDIT:
        if side in OPTYPE_CREDIT:
            return OPTYPE_CREDIT[side]
        if side in OPTYPE_CREDIT_PLAIN:
            return OPTYPE_CREDIT_PLAIN[side]
        raise DimensionError(f"两融 opType 表无 side={side.value}")

    if intent.account_scope.is_combo:
        if side in OPTYPE_COMBO:
            return OPTYPE_COMBO[side]
        raise DimensionError(
            f"组合交易 opType 表无 side={side.value}"
            "（组合只支持普通买卖；两融操作请用 instrument_class=credit）")

    if side in OPTYPE_STOCK:
        return OPTYPE_STOCK[side]
    raise DimensionError(
        f"{side.value} 需要 instrument_class=credit（两融）或 portfolio（组合），"
        f"当前 instrument_class={ic.value}、scope={intent.account_scope.value}")


def plan_xt_strategy(intent: OrderIntent, *, context: Any = None,
                     account_id: str = "") -> Plan:
    """C 通道：把意图映射到客户端内 Python 交易函数。

    决策树（顺序即优先级）：

    1. ``exec_mode=task``  → ``algo_passorder`` + ``UseTrigger``（触价单/条件单）
    2. ``exec_mode=algo``  → ``algo_passorder`` + ``userOrderParam``
    3. ``exec_mode=smart_algo`` → ``smart_algo_passorder``
    4. 期货 → 官方 6 个开平仓辅助函数
    5. 期权 / 两融 / 组合 / 账号组 → ``passorder``（opType + orderType + prType）
    6. 其余（普通股票/ETF/债券）→ ``order_*`` 数量语义辅助函数
    """
    _require(intent, Channel.XT_STRATEGY)
    # ★ accountID 位填什么，取决于作用域：组作用域填**组名**，否则填资金账号。
    #   搞反了就是"下单成功但下到了错的地方"，比报错更危险。
    acct = account_id or intent.account_id
    if intent.account_scope.is_group:
        acct = intent.account_group or acct
    if not acct:
        raise DimensionError(
            f"account_scope={intent.account_scope.value} 缺 accountID/组名")
    notes: list[str] = []
    codes: dict[str, Any] = {}

    # ---- 1/2. 触价单与算法单 -------------------------------------------
    if intent.exec_mode in (ExecMode.TASK, ExecMode.ALGO):
        op_type = _resolve_optype(intent)
        otype = order_type(intent.account_scope, intent.qty_semantics)
        pr_type = prtype(intent.price_type, exchange=intent.exchange,
                         instrument_class=intent.instrument_class,
                         combo=intent.account_scope.is_combo)
        param = dict(intent.algo)
        if intent.exec_mode is ExecMode.TASK:
            param.update(intent.task)
            param.setdefault("UseTrigger", 1)
            if "TriggerPrice" not in param:
                raise DimensionError(
                    "触价单缺 TriggerPrice；官方 userOrderParam 需 "
                    "UseTrigger=1 + TriggerType + TriggerPrice")
            notes.append("触价单：UseTrigger/TriggerType/TriggerPrice 走 userOrderParam")
        notes.append("userOrderParam 键名依官方文档，实盘前需小单验证")
        codes.update(opType=op_type, orderType=otype, prType=pr_type)
        return Plan(
            channel=Channel.XT_STRATEGY, func="algo_passorder",
            args=(op_type, otype, acct, intent.code, pr_type, intent.price,
                  intent.quantity, intent.strategy_name, 1,
                  intent.remark or intent.client_order_id, param, context),
            codes=codes, notes=tuple(notes))

    # ---- 3. 智能算法 ---------------------------------------------------
    if intent.exec_mode is ExecMode.SMART_ALGO:
        op_type = _resolve_optype(intent)
        otype = order_type(intent.account_scope, intent.qty_semantics)
        pr_type = prtype(intent.price_type, exchange=intent.exchange,
                         instrument_class=intent.instrument_class,
                         combo=intent.account_scope.is_combo)
        algo_type = intent.algo.get("algo_type")
        if not algo_type:
            raise DimensionError("smart_algo 需要 algo['algo_type']（如 TWAP）")
        notes.append("smart_algo_passorder 的 algoParam/minAmount 语义需实盘验证")
        codes.update(opType=op_type, orderType=otype, prType=pr_type)
        return Plan(
            channel=Channel.XT_STRATEGY, func="smart_algo_passorder",
            args=(op_type, otype, acct, intent.code, pr_type, intent.price,
                  intent.quantity, intent.strategy_name, 1,
                  intent.remark or intent.client_order_id,
                  algo_type, intent.algo.get("algo_param", 0),
                  intent.algo.get("min_amount", 0),
                  intent.algo.get("valid_begin", ""),
                  intent.algo.get("valid_end", ""), context),
            codes=codes, notes=tuple(notes))

    # ---- 4. 期货：官方开平仓辅助函数（能表达才用） ---------------------
    if intent.instrument_class is InstrumentClass.FUTURE:
        fn = FUTURE_HELPER.get((intent.side, intent.position_action))
        style = STYLE.get(intent.price_type)
        # 官方辅助函数只覆盖 open / close_tday_first / close_yday_first，
        # 且 style 只有 5 个字符串。表达不了就**落到 passorder**（opType 0–15
        # 能精确表达平今/平昨），而不是拒绝 —— 拒绝会丢掉合法维度。
        if fn is not None and style is not None and \
                intent.qty_semantics in (QtySemantics.SHARES, QtySemantics.LOTS):
            notes.append("期货辅助函数 amount 单位是手")
            return Plan(channel=Channel.XT_STRATEGY, func=fn,
                        args=(intent.code, int(intent.quantity), style,
                              intent.price, context, acct),
                        codes={"style": style}, notes=tuple(notes))
        if fn is not None:
            notes.append("辅助函数表达不了该组合（数量语义/选价类型超界），改走 passorder")
        else:
            notes.append("精确平今/平昨用 passorder 的 opType 0–15（辅助函数只到"
                         " close_tday_first / close_yday_first）")

    # ---- 5. passorder：期权 / 两融 / 组合 / 账号组 / 辅助函数表达不了 ----
    needs_passorder = (
        intent.instrument_class in (InstrumentClass.OPTION, InstrumentClass.CREDIT)
        or intent.account_scope is not AccountScope.SINGLE
        or intent.instrument_class is InstrumentClass.FUTURE
        or intent.price_type not in STYLE
    )
    if needs_passorder:
        op_type = _resolve_optype(intent)
        otype = order_type(intent.account_scope, intent.qty_semantics)
        pr_type = prtype(intent.price_type, exchange=intent.exchange,
                         instrument_class=intent.instrument_class,
                         combo=intent.account_scope.is_combo)
        if intent.account_scope is AccountScope.ARBITRAGE:
            notes.append("组合套利：accountID='股票账号,期货账号'、"
                         "orderCode='篮子名,期货合约'、price 位传套利比例")
        if intent.qty_semantics is QtySemantics.VALUE and \
                intent.instrument_class is not InstrumentClass.STOCK:
            raise DimensionError("orderType=1102/1202（金额方式）官方注明只支持股票")
        codes.update(opType=op_type, orderType=otype, prType=pr_type)
        if intent.account_scope.is_group:
            notes.append("accountID 位传的是组名，orderType 已按官方表选 12xx/22xx/233x")
        return Plan(
            channel=Channel.XT_STRATEGY, func="passorder",
            args=(op_type, otype, acct, intent.code, pr_type, intent.price,
                  intent.quantity, intent.strategy_name, 1,
                  intent.remark or intent.client_order_id, context),
            codes=codes, notes=tuple(notes))

    # ---- 6. 普通品种：数量语义辅助函数 ---------------------------------
    fn = HELPER_BY_QTY[intent.qty_semantics]
    style = STYLE.get(intent.price_type)
    if style is None:
        raise UnsupportedDimension(
            "xt_strategy", "price_type", intent.price_type.value,
            f"{fn} 的 style 只接受 LATEST/FIX/HANG/COMPETE/SALE5..SALE1；"
            "需要更细的选价请改用 passorder")
    notes.append(f"数量语义 {intent.qty_semantics.value} 由 {fn} 原生表达")
    notes.append(f"style={style}（官方字符串枚举，不是数字）")
    return Plan(channel=Channel.XT_STRATEGY, func=fn,
                args=(intent.code, intent.quantity, style, intent.price,
                      context, acct),
                codes={"style": style}, notes=tuple(notes))


# ---------------------------------------------------------------------------
# F 通道：文件单
# ---------------------------------------------------------------------------
def plan_file(intent: OrderIntent, channel: Channel) -> Plan:
    """F 通道：生成文件单载荷。

    ★ 列名目前是**推断值**，必须实测确认后再用于生产（方案文档 §9.6 第 1 步）。
    """
    _require(intent, channel)
    payload = {
        "code": intent.code,
        "side": intent.side.value,
        "quantity": int(intent.quantity),
        "price": float(intent.price),
        "price_type": intent.price_type.value,
        "account_id": intent.account_id,
        "remark": intent.remark or intent.client_order_id,
    }
    notes = ["列名与校验顺序尚未实测确认（columns_confirmed=False）",
             "文件单无回报回调，需另配对账（导出/日志/xtdata）"]
    if channel is Channel.BATCH_ORDER:
        notes.append("batch_order 需人工在界面触发或 key_enable=1 自动")
    return Plan(channel=channel, func="write_order_file", payload=payload,
                notes=tuple(notes))


# ---------------------------------------------------------------------------
# A 通道：xttrader
# ---------------------------------------------------------------------------
def plan_xttrader(intent: OrderIntent) -> Plan:
    _require(intent, Channel.XTTRADER)
    return Plan(
        channel=Channel.XTTRADER, func="order_stock",
        kwargs={
            "account": intent.account_id,
            "stock_code": intent.code,
            "order_type": intent.side.value,
            "order_volume": int(intent.quantity),
            "price_type": intent.price_type.value,
            "price": float(intent.price),
            "strategy_name": intent.strategy_name,
            "order_remark": intent.remark or intent.client_order_id,
        },
        notes=("本机不可用：客户端 2026-08 桥 × 2021/2023 pyd 版本锁死"
               "（方案文档 §9.3）",),
    )


_PLANNERS = {
    Channel.XT_STRATEGY: plan_xt_strategy,
    Channel.FILE_CSV: plan_file,
    Channel.FILE_DBF: plan_file,
    Channel.BATCH_ORDER: plan_file,
    Channel.XTTRADER: plan_xttrader,
}

#: 默认优先级：先能力最全，再退化到简单通道。
DEFAULT_PRIORITY = (
    Channel.XT_STRATEGY,
    Channel.XTTRADER,
    Channel.FILE_CSV,
    Channel.FILE_DBF,
    Channel.BATCH_ORDER,
)


def resolve(intent: OrderIntent, priority: Iterable[Channel] = DEFAULT_PRIORITY,
            *, context: Any = None, account_id: str = "") -> Resolution:
    """按优先级挑第一个**完全支持**该意图的通道。

    任一通道缺维度只记原因、不降级；全部不支持则 ``plan is None``，
    调用方必须显式处理（不能当成功）。
    """
    rejected: list[tuple[str, str]] = []
    for ch in priority:
        missing = check(intent, ch)
        if missing:
            rejected.append((ch.value, "缺维度 " + ", ".join(missing)))
            continue
        planner = _PLANNERS.get(ch)
        if planner is None:
            rejected.append((ch.value, "无规划器"))
            continue
        try:
            if ch is Channel.XT_STRATEGY:
                p = planner(intent, context=context, account_id=account_id)
            else:
                p = planner(intent)
        except UnsupportedDimension as e:
            rejected.append((ch.value, str(e)))
            continue
        except DimensionError as e:
            rejected.append((ch.value, f"维度不自洽：{e}"))
            continue
        return Resolution(intent=intent, plan=p, rejected=tuple(rejected))
    return Resolution(intent=intent, plan=None, rejected=tuple(rejected))


def capability_matrix() -> list[dict]:
    """把能力矩阵摊平成表格行，供前端/文档直接渲染。"""
    rows = []
    for ch, cap in CAPABILITIES.items():
        row: dict[str, Any] = {"channel": ch.value, "note": cap.note}
        for axis in _AXES:
            allowed = cap.axes.get(axis)
            row[axis] = "*" if allowed is None else sorted(allowed)
        rows.append(row)
    return rows


def tables() -> dict:
    """导出三张官方表的可读版本（供文档/前端下拉框使用）。"""
    return {
        "opType": {
            "stock": {k.value: v for k, v in OPTYPE_STOCK.items()},
            "credit": {k.value: v for k, v in OPTYPE_CREDIT.items()},
            "credit_plain": {k.value: v for k, v in OPTYPE_CREDIT_PLAIN.items()},
            "combo": {k.value: v for k, v in OPTYPE_COMBO.items()},
            "future_six": {f"{k[0].value}/{k[1]}": v
                           for k, v in OPTYPE_FUTURE_SIX.items()},
            "future_four": {f"{k[0].value}/{k[1]}": v
                            for k, v in OPTYPE_FUTURE_FOUR.items()},
            "future_two": {f"{k[0].value}/{k[1]}": v
                           for k, v in OPTYPE_FUTURE_TWO.items()},
            "option": {f"{k[0].value}/{k[1]}": v
                       for k, v in OPTYPE_OPTION.items()},
            "etf_purchase": OPTYPE_ETF_PURCHASE,
        },
        "orderType": {
            "single": {k.value: v for k, v in ORDERTYPE_SINGLE.items()},
            "group": {k.value: v for k, v in ORDERTYPE_GROUP.items()},
            "combo_single": {k.value: v for k, v in ORDERTYPE_COMBO_SINGLE.items()},
            "combo_group": {k.value: v for k, v in ORDERTYPE_COMBO_GROUP.items()},
            "arbitrage": {k.value: v for k, v in ORDERTYPE_ARBITRAGE.items()},
        },
        "prType": {k.value: v for k, v in PRTYPE.items()},
    }


__all__ = [
    "AccountScope", "CAPABILITIES", "Capability", "Channel", "DimensionError",
    "ExecMode", "FUTURE_HELPER", "HELPER_BY_QTY", "InstrumentClass", "OPTYPE_COMBO",
    "OPTYPE_CREDIT", "OPTYPE_CREDIT_PLAIN", "OPTYPE_ETF_PURCHASE", "OPTYPE_FUTURE_FOUR",
    "OPTYPE_FUTURE_SIX", "OPTYPE_FUTURE_TWO", "OPTYPE_OPTION", "OPTYPE_STOCK",
    "ORDERTYPE_ARBITRAGE", "ORDERTYPE_COMBO_GROUP", "ORDERTYPE_COMBO_SINGLE",
    "ORDERTYPE_GROUP", "ORDERTYPE_SINGLE", "OrderIntent", "PRTYPE", "Plan",
    "PriceType", "QtySemantics", "Resolution", "STYLE", "Side", "TimeInForce",
    "UnsupportedDimension", "capability_matrix", "check", "order_type", "plan_file",
    "plan_xt_strategy", "plan_xttrader", "prtype", "resolve", "tables",
]

# ---------------------------------------------------------------------------
# EVIDENCE
# ---------------------------------------------------------------------------
#: 三张表与所有函数签名均来自随客户端下发的官方文档：
#: ``P:/stock/guoxin_iquant_11/HTML/guosenPythonApiHelp/iQuant_Python_API_Doc.html``
#: （v3.3.5，538 KB），章节 3.2.4「交易函数」。
#:
#: * ``opType``：0–5 期货六键、6–9 期货四键、10–15 期货两键、23/24 股票、
#:   25/26 组合、27–34 两融、40/43/46–49 期货组合、50–59 期权、60 ETF 申购。
#: * ``orderType``：1101/1102/1113/1123（单账号）、1201/1202/1213/1223（账号组）、
#:   2101/2102/2103（组合单账号）、2201/2202/2203（组合账号组）、
#:   2331/2332/2333（组合套利）。**数量语义编码在 orderType 里。**
#: * ``prType``：-1 与 0–14、27–29、42–49；其中 42/43 仅上交所、
#:   44–48 仅深交所、27–29 仅股票期权、49 科创板盘后定价。
#: * 辅助函数签名：``order_shares(stockcode, shares[, style, price], ContextInfo[, accId])``、
#:   ``buy_open(stockcode, amount[, style, price], ContextInfo[, accId])``，
#:   ``style`` 是**字符串**（LATEST/FIX/HANG/COMPETE/SALE5..SALE1）。
#: * ``algo_passorder(opType, orderType, accountid, orderCode, prType, modelprice,
#:   volume[, strategyName, quickTrade, userOrderId, userOrderParam], ContextInfo)``；
#:   ``userOrderParam`` 键含 UseTrigger / TriggerType / TriggerPrice（触价单）。
#: * ``smart_algo_passorder(opType, orderType, accountID, orderCode, prType, price,
#:   volume, strategyName, quickTrade, remark, algoType, algoParam, minAmount
#:   [, validBegin, validEnd], ContextInfo)``。
#:
#: 通道能力的证据：
#: * ``file_csv``/``file_dbf``/``batch_order``：``userdata/users/15624979679/Config.xml``
#:   的 ``CsvMonitorSetting``/``DbfMonitorSetting``/``batchOrderSetting``
#:   只有 ``monitor_path``/``is_automonitor`` 一类开关，没有算法/时效/回报回调配置位。
#: * ``xttrader``：``XtQuantServer_qmt.dll`` 存在但客户端自带 pyd 为
#:   2021-11-26 / 2023-02-15、桥为 2026-08-11，``connect()`` 恒 3.00s 超时（§9.3）。
#: * ``call_formula``：``XtModel.exe`` 字符串表导出的 RPC 注册表**无**
#:   ``createFormula`` ⇒ 无法创建公式实例（§9.2）。
