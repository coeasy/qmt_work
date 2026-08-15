"""券商适配器统一抽象（多券商 / 多客户端版本）。

所有券商客户端（国金 QMT / 华鑫 / 银河 / 同花顺 / 恒生 PTrade / 掘金 …）都实现本接口。
后端其余部分（routes / tools / mcp / sync）只依赖本抽象，不感知具体券商。

设计原则：
- 不返回任何假数据。券商未连接 / SDK 缺失时，方法抛 `BrokerNotConnectedError` 或 `BrokerSDKError`，
  由上层转换为 503/400，前端展示「未连接券商客户端」。
- 行情/交易/账户全部走券商真实 SDK（XTQuant 等）。
- 回测引擎复用 `get_kline`（真实历史数据），不再使用随机游走假数据。
"""
from abc import ABC, abstractmethod


class BrokerError(Exception):
    """券商层通用异常。"""


class BrokerNotConnectedError(BrokerError):
    """券商客户端未连接（未启动 / 账号未订阅 / 行情服务未开）。"""


class BrokerSDKError(BrokerError):
    """缺少券商 SDK（未安装 xtquant / 同花顺 SDK / PTrade SDK / 掘金 gm 等）。"""

    def __init__(self, sdk: str, extra: str = ""):
        self.sdk = sdk
        super().__init__(
            f"缺少券商 SDK：{sdk}。请在运行本后端的机器上安装（{extra or '参见券商文档'}），"
            f"且券商客户端需处于登录/可交易状态。")


class BrokerAdapter(ABC):
    """券商适配器统一接口（真实实现，禁止假数据）。"""

    # ---------------- 身份元数据 ----------------
    @property
    @abstractmethod
    def broker_name(self) -> str:
        """券商名，如 国金证券 / 华鑫证券 / 同花顺 / 恒生PTrade / 掘金。"""

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """适配器实现标识，如 xtp / ths / ptrade / juejin。"""

    @property
    @abstractmethod
    def client_version(self) -> str:
        """客户端/SDK 版本，如 xtquant 1.0.0 / v2024。"""

    @property
    def account_id(self) -> str:
        """资金账号（可能为空，未配置时）。"""
        return ""

    @property
    def account_type(self) -> str:
        """账户类型：STOCK / CREDIT / OPTION / FUTURES。"""
        return "STOCK"

    @property
    def supported_periods(self) -> list[str]:
        """支持的 K 线周期：1m/5m/15m/30m/60m/1d/1w/1mon 等。"""
        return ["1d"]

    @property
    def supported_account_types(self) -> list[str]:
        """支持的账户类型。"""
        return ["STOCK"]

    @property
    def sdk_required(self) -> str:
        """运行所需 SDK 的 import 名 / pip 包名。"""
        return ""

    # ---------------- 生命周期 ----------------
    @abstractmethod
    def start(self) -> None:
        """建立与券商客户端的连接（行情 + 交易）。"""

    @abstractmethod
    def close(self) -> None:
        """断开连接、释放资源。"""

    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接。"""

    def test_connection(self) -> dict:
        """探测连接健康度，返回结构化结果（默认用 is_connected + 一次轻量查询）。"""
        if not self.is_connected():
            return {"connected": False, "detail": "未连接"}
        try:
            cash = self.get_cash()
            return {"connected": True, "broker": self.broker_name,
                    "account_id": self.account_id, "account_type": self.account_type,
                    "assets": cash.get("assets"), "detail": "ok"}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "detail": f"连接异常：{exc}"}

    # ---------------- 行情 ----------------
    @abstractmethod
    def get_quote(self, code: str) -> dict:
        """实时行情快照（最新价 / 买卖价 / 成交量）。"""

    @abstractmethod
    def get_full_tick(self, codes: list[str]) -> dict:
        """五档盘口全推快照。返回 {code: {lastPrice,askPrice[5],bidPrice[5],volume,...}}。"""

    @abstractmethod
    def get_kline(self, code: str, period: str, count: int,
                  start: str = "", end: str = "") -> list[dict]:
        """历史 K 线。返回 [{time,open,high,low,close,volume}, ...]。"""

    @abstractmethod
    def get_tick(self, code: str) -> dict:
        """最新逐笔/快照（用于 tick 级展示）。"""

    @abstractmethod
    def get_stock_list(self, sector: str = "沪深A股") -> list[dict]:
        """板块股票列表 [{code, name}, ...]。"""

    def search_stocks(self, keyword: str, limit: int = 20) -> list[dict]:
        """按代码/名称关键字搜索。默认实现：遍历板块后模糊匹配代码；名称匹配由子类增强。"""
        kw = (keyword or "").strip().upper()
        if not kw:
            return []
        out = []
        for it in self.get_stock_list():
            if kw in it.get("code", "").upper() or kw in it.get("name", "").upper():
                out.append(it)
                if len(out) >= limit:
                    break
        return out

    @abstractmethod
    def subscribe_quote(self, codes: list[str], on_tick) -> None:
        """订阅实时行情；券商回调线程触发 on_tick({"type":"quote","data":{...}})。"""

    # ---------------- 账户 ----------------
    @abstractmethod
    def get_account(self) -> dict:
        """账户信息（净值 / 可用 / 持仓市值等）。"""

    @abstractmethod
    def get_positions(self, symbol: str | None = None) -> list[dict]:
        """当前持仓 [{code, name, volume, avail, cost, market_value}, ...]。"""

    @abstractmethod
    def get_cash(self) -> dict:
        """资金与资产 {cash, frozen, assets}。"""

    @abstractmethod
    def get_orders(self) -> list[dict]:
        """当日委托。"""

    @abstractmethod
    def get_deals(self) -> list[dict]:
        """当日成交。"""

    # ---------------- 交易 ----------------
    @abstractmethod
    def place_order(self, code: str, direction: str, price_type: str,
                    price: float, volume: int, strategy_name: str = "",
                    remark: str = "") -> dict:
        """下单。direction: buy/sell；price_type: limit/market。返回 {order_id,status,...}。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        """撤单。返回 {order_id, status}。"""

    def cancel_order_price(self, order_id: str, deviation: float = 0.01) -> dict:
        """超价撤单（偏离最新价超过 deviation 撤）。默认实现：直接撤单。"""
        return self.cancel_order(order_id)

    # ---------------- 参考数据 / L2（默认抛 SDK 缺失，子类实现真实调用） ----------------
    def _not_supported(self, what: str):
        raise BrokerSDKError(
            self.sdk_required or "该券商SDK",
            f"{what} 由 {self.adapter_id} 适配器实现，当前未实现或 SDK 未安装")

    def get_sector_list(self) -> list[str]:
        """板块列表（如 沪深A股/ETF/行业板块…）。"""
        self._not_supported("板块列表")

    def get_sector_stocks(self, sector: str = "沪深A股") -> list[str]:
        """板块成分代码列表。"""
        self._not_supported("板块成分")

    def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        """交易日历（YYYYMMDD 列表）。"""
        self._not_supported("交易日历")

    def get_financial(self, code: str) -> dict:
        """财务摘要（每股收益/净资产/营收/利润等，经真实数据源）。"""
        self._not_supported("财务数据")

    def get_l2_transactions(self, code: str, count: int = 100) -> list[dict]:
        """Level-2 逐笔成交。"""
        self._not_supported("Level-2 逐笔")
