"""把单个券商连接包装成 ``DataSource`` 形态（P1-1 自 ``registry.py`` 拆出）。

``_BoundBrokerSource`` 与 ``DataSourceManager`` 之间**只有一处**真实耦合
（``_default_broker_factory`` 构造它），却因为同处一个 1296 行文件里而看不出这条边界。

★ 名字**保留前导下划线**：``datasource.registry`` 与
``tests/test_capability_chain_unity.py`` 都按这个名字导入，改名是无谓的破坏性变更。
"""
from __future__ import annotations

from typing import Optional

from xtquant_client.base import BrokerError

class _BoundBrokerSource:
    """把单个券商连接包装成 DataSource 形态（仅行情/基础数据，无交易）。

    内置到 manager，不对外注册；为 broker 连接动态创建，conn_id 已绑定。
    """

    name = "broker"
    # V11 R6：补 kline_qfq/kline_hfq —— get_kline 明确透传 adjust（QMT 经 dividend_type
    # 参数化），契约链也把 broker 列在复权链首位，此前声明漏了两个变体。
    # 不含 stock_list：get_stock_list 恒返回 None（券商侧无全市场列表接口）。
    capabilities = frozenset({"quote", "kline", "kline_qfq", "kline_hfq",
                              "instrument_detail"})
    #: ★ 唯一支持**按日期区间**取 K 线的源（V11 §5.3 P0-3 III）。
    #: 迅投 ``get_market_data(start_time=, end_time=)`` + ``download_history_data``
    #: 都能按日期区间工作，因此「全量回补」可以**逐年向前翻页**；
    #: 而 eltdx / 免费在线源只接受 ``count``（最近 N 根），无法指定区间 ——
    #: 声明这个标志，让回补逻辑只走真正做得到的源，而不是「假装翻页、其实一直
    #: 拿最近 120 根」。声明式能力优于 try/except TypeError。
    supports_kline_range = True

    def __init__(self, bridge):
        self._b = bridge

    async def get_quote(self, code: str) -> Optional[dict]:
        try:
            q = await self._b.call(self._b.gateway.get_quote, code)
        except BrokerError:
            return None
        if isinstance(q, dict) and isinstance(q.get("code"), int):
            return None  # _call 返回了 err(503) dict
        return q

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: Optional[str] = None,
                        start: str = "", end: str = "") -> Optional[list]:
        try:
            # 透传 adjust：QMT 复权经 dividend_type 参数化（quotes.get_kline），
            # 使 broker 能参与 qfq/hfq 链（D9 v1.3）。
            # ``start``/``end``（YYYYMMDD 或 YYYY-MM-DD，空 = 不限）供全量回补
            # 按区间翻页；普通调用不传，行为与改造前完全一致。
            bars = await self._b.call(self._b.gateway.get_kline, code, period, count,
                                      adjust=adjust, start=start or "", end=end or "")
        except BrokerError:
            return None
        if isinstance(bars, dict) and bars.get("code"):
            return None
        return bars

    async def get_instrument_detail(self, code: str) -> Optional[dict]:
        try:
            det = await self._b.call(self._b.gateway.get_instrument_detail, code)
        except (BrokerError, AttributeError):
            # AttributeError：适配器未实现该方法（如 BridgeAdapter 历史缺失
            # get_instrument_detail）。降级为不可用，避免击穿 auto 链 / 误触发熔断。
            return None
        if isinstance(det, dict) and isinstance(det.get("code"), int):
            return None
        return det

    async def get_stock_list(self) -> Optional[list]:
        # 券商侧无统一全市场股票列表接口；auto 链中跳过。
        return None

    async def get_sector_stocks(self, sector: str = "沪深A股") -> Optional[list[str]]:
        """板块成分股代码列表（实测「沪深A股」返回 5224 只）。

        ★ 为什么需要它：``_sup_chain`` 对 ``sector`` 能力**刻意排除 broker**
        （历史上券商未实现 ``get_board_constituents``），但券商其实有
        ``gateway.get_sector_stocks`` —— 同一个能力，K 线同步那条路走得通
        （``app/routes/market.py::_get_sector_stocks``），选股池那条路却走不通，
        于是**纯券商环境下选股永远报「股票池为空」**。本方法把这条能力接进
        ``DataSource`` 抽象，供 universe 作券商兜底。
        """
        try:
            res = await self._b.call(self._b.gateway.get_sector_stocks, sector)
        except (BrokerError, AttributeError):
            return None
        if isinstance(res, dict) and res.get("code"):
            return None
        if isinstance(res, list) and res:
            return [str(c) for c in res]
        return None
