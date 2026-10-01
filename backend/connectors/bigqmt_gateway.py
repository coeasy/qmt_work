"""大 QMT 桥的 **gateway 面**（``xtquant`` 兼容外观）。

★ 本模块是 ``connectors/bigqmt_bridge.py`` 的**职责拆分**产物：该文件同时承载
  「双槽位契约 + 事件泵 + 行情订阅对账 + 存活判据」与「canonical op → 旧适配器
  shape 的适配」，一度越过单文件体积上限（``scripts/check_execution_architecture.py``
  的 Gate 4 = 50KB）。拆分标准是**关注点**而不是行数：

  | 模块                  | 关注点                                                   |
  |-----------------------|----------------------------------------------------------|
  | ``bigqmt_gateway.py`` | 本文件：**合约适配**（canonical op ↔ ``XTQuantGateway`` shape） |
  | ``bigqmt_bridge.py``  | 双槽位本体、事件泵、行情订阅对账、``is_connected`` 存活判据   |

★ 双槽位（``bridge`` / ``adapter`` 是同一个对象）的完整设计理由、线程与事件循环
  纪律、以及 ``SUB_QUOTE`` 的落地形态，**只在** ``bigqmt_bridge.py`` 的模块
  docstring 维护 —— 本文件刻意不复制那份说明，避免两处漂移。

shape 纪律（与 ``XTQuantGateway`` 的差异与理由）
----------------------------------------------
- 真适配器的方法是**同步**的，由 ``XTQuantBridge.call`` 丢进线程池执行；
- 本类的网关方法是 **async** 的（文件桥本身就是异步 IO），由
  ``BigQmtBridge.call/call_locked`` 直接 ``await``；
- 两者对调用方**同形**：所有调用点一律写
  ``await bridge.call(bridge.gateway.<method>, ...)``，由桥决定是 await
  还是丢线程池。**禁止**直接同步调用本类方法（会拿到未 await 的协程）。
- 例外两项按原契约保持同步：``is_connected()``（HealthPort 要求 O(1) 无 IO）、
  ``subscribe_quote()``（回调对象不可过线，仅作兼容位**但不再是空操作** ——
  它记录订阅意图，由泵对账下发，见 ``BigQmtBridge.want_quotes``）。

★ 与 ``BigQmtBridge`` 的唯一耦合是 ``self._bridge``，且只用**鸭子类型**的三个成员：
  ``is_connected()`` / ``want_quotes(codes)`` / ``_stamp_agent_ok()``。刻意不做
  顶层 import（那会形成 ``bigqmt_bridge → bigqmt_gateway → bigqmt_bridge`` 的环）。
"""
from __future__ import annotations

from typing import Any

from connectors.ports import InstrumentId, OrderRequest, OrderSnapshot


def _split_instrument(code: str) -> InstrumentId:
    """``600036.SH`` → InstrumentId(code=600036, exchange=SH)。"""
    base, _, ex = (code or "").partition(".")
    return InstrumentId(code=base, exchange=ex.upper())


def _snapshot_to_legacy(snap: OrderSnapshot) -> dict:
    """OrderSnapshot → 既有下游期望的下单回执 dict（与 execution._snapshot_to_legacy 同形）。"""
    data: dict[str, Any] = dict(getattr(snap, "raw", None) or {})
    data.setdefault("order_id", getattr(snap, "broker_order_id", "") or "")
    data["ok"] = bool(getattr(snap, "accepted", False))
    data["status"] = getattr(snap, "status", "")
    data["code"] = 0
    cid = getattr(snap, "client_order_id", "")
    if cid:
        data["client_order_id"] = cid
    return data


def _raw_of(snap: Any) -> dict:
    """canonical 快照 → 方言 raw dict（账户/持仓行下游要的是 dict 而不是 dataclass）。"""
    raw = getattr(snap, "raw", None)
    return dict(raw) if isinstance(raw, dict) else {}


class _BigQmtGateway:
    """xtquant 兼容网关外观：把 connectors 的 canonical op 适配为**旧适配器 shape**。

    ★ shape 纪律（与 ``XTQuantGateway`` 的差异与理由）
      - 真适配器的方法是**同步**的，由 ``XTQuantBridge.call`` 丢进线程池执行；
      - 本类的网关方法是 **async** 的（文件桥本身就是异步 IO），由
        ``BigQmtBridge.call/call_locked`` 直接 ``await``；
      - 两者对调用方**同形**：所有调用点一律写
        ``await bridge.call(bridge.gateway.<method>, ...)``，由桥决定是 await
        还是丢线程池。**禁止**直接同步调用本类方法（会拿到未 await 的协程）。
      - 例外两项按原契约保持同步：``is_connected()``（HealthPort 要求 O(1) 无 IO）、
        ``subscribe_quote()``（回调对象不可过线，仅作兼容位**但不再是空操作** ——
        它记录订阅意图，由泵对账下发，见 ``BigQmtBridge.want_quotes``）。
    """

    def __init__(self, connector: Any, bridge: Any):
        self._c = connector
        self._bridge = bridge

    # ---------------- 行情 ----------------
    async def get_quote(self, code: str) -> dict:
        return dict(await self._c.get_quote(code) or {})

    async def get_full_tick(self, codes: list[str]) -> dict:
        return dict(await self._c.get_full_tick(list(codes or [])) or {})

    async def get_tick(self, code: str) -> dict:
        """最新快照。桥没有独立 tick 通道 ⇒ 用 get_full_tick 单标的等价实现。"""
        tick = await self.get_full_tick([code])
        if isinstance(tick, dict) and code in tick:
            inner = tick.get(code)
            return dict(inner) if isinstance(inner, dict) else {"value": inner}
        return dict(tick) if isinstance(tick, dict) else {}

    async def get_kline(self, code: str, period: str, count: int,
                        start: str = "", end: str = "", adjust: str = "") -> list[dict]:
        """K 线。

        ★ 必须返回 **list**（空也返回 ``[]``）：真适配器/``XTQuantGateway`` 的契约
          就是 list。曾经这里写 ``or {}``，空结果会变成 dict，让下游
          （``kline_io`` 的 ``isinstance(bars, dict) and bars.get("code")`` 分支、
          KlineCache 落库）拿到错误的容器类型。
        """
        return list(await self._c.get_kline(
            code, period, int(count or 0),
            adjust=adjust or "", start=start or "", end=end or "") or [])

    # ---------------- 合约 / 板块 / 日历 ----------------
    async def get_instrument_detail(self, code: str) -> dict:
        return dict(await self._c.get_instrument_detail(code) or {})

    async def get_stock_list(self, sector: str = "沪深A股") -> list[dict]:
        return list(await self._c.get_stock_list(sector) or [])

    async def get_sector_list(self) -> list[str]:
        return list(await self._c.get_sector_list() or [])

    async def get_sector_stocks(self, sector: str = "沪深A股") -> list[str]:
        """板块成分代码列表（旧适配器面有此方法，桥没有该 op ⇒ 由成分表派生）。"""
        rows = await self.get_stock_list(sector)
        return [str(r.get("code") or "") for r in rows if isinstance(r, dict)
                and r.get("code")]

    async def search_stocks(self, keyword: str, limit: int = 20) -> list[dict]:
        """按代码/名称关键字搜索（与 ``BrokerAdapter.search_stocks`` 同语义）。"""
        kw = str(keyword or "").strip().upper()
        if not kw:
            return []
        out: list[dict] = []
        for it in await self.get_stock_list():
            if not isinstance(it, dict):
                continue
            if kw in str(it.get("code") or "").upper() \
                    or kw in str(it.get("name") or "").upper():
                out.append(it)
                if len(out) >= int(limit or 0 or 20):
                    break
        return out

    async def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        return list(await self._c.get_trading_calendar(
            start=start or "", end=end or "") or [])

    # ---------------- 桥上不存在的接口（如实报错，绝不返回空冒充成功） ----------------
    async def get_financial(self, code: str) -> dict:
        """财务摘要：大 QMT **内置 API 不提供**（xtdata 侧有，但桥走的是 ContextInfo）。

        ★ 刻意抛 ``BrokerError`` 而不是返回 ``{}``：空 dict 会被上层当成
          「这家公司没有财务数据」，而真相是「这条通道拿不到」—— 前者会污染
          基本面结论，后者只会走既有的数据源回退。
        """
        from xtquant_client.base import BrokerError

        raise BrokerError(
            f"大 QMT 桥未提供财务接口（code={code}）：agent 侧可用的注入函数里"
            "没有财务查询。请改用平台财务数据源（datasource）或把该行情位切到"
            "xtquant 直连（路径 A）。")

    async def get_l2_transactions(self, code: str, count: int = 100) -> list[dict]:
        """Level-2 逐笔：同上，桥上无此注入函数。"""
        from xtquant_client.base import BrokerError

        raise BrokerError(
            f"大 QMT 桥未提供 Level-2 逐笔接口（code={code}）：需行情端 L2 授权"
            "与对应注入函数，当前 agent 未捕获到。")

    # ---------------- 账户 / 持仓 / 委托 / 成交 ----------------
    async def get_account(self) -> dict:
        return _raw_of(await self._c.get_account())

    async def get_cash(self) -> dict:
        return _raw_of(await self._c.get_cash())

    async def get_positions(self, symbol: str | None = None) -> list[dict]:
        snaps = await self._c.get_positions(symbol)
        return [_raw_of(s) for s in (snaps or [])]

    async def get_orders(self) -> list[dict]:
        return [_raw_of(s) for s in (await self._c.get_orders() or [])]

    async def get_deals(self) -> list[dict]:
        return [_raw_of(s) for s in (await self._c.get_deals() or [])]

    # ``XTQuantGateway`` 的旧命名（query_position/query_cash）保持可用。
    async def query_position(self) -> list[dict]:
        return await self.get_positions()

    async def query_cash(self) -> dict:
        return await self.get_cash()

    # ---------------- 交易 ----------------
    async def place_order(self, code: str, direction: str, price_type: str,
                          price: float, volume: int, strategy_name: str = "",
                          remark: str = "") -> dict:
        req = OrderRequest(
            instrument=_split_instrument(code),
            side=str(direction or "").lower(),
            order_type=str(price_type or "limit").lower(),
            price=float(price or 0.0),
            quantity=int(volume or 0),
            strategy_name=strategy_name or "",
            remark=remark or "",
        )
        snap = await self._c.place_order(req)
        # 幂等锚点回读：回执里如实带上平台侧关联号（拿不到就是空串，不伪造）。
        return _snapshot_to_legacy(snap)

    async def cancel_order(self, order_id: str) -> dict:
        snap = await self._c.cancel_order(order_id)
        return {
            "ok": bool(getattr(snap, "accepted", False)),
            "order_id": getattr(snap, "broker_order_id", "") or str(order_id),
            "status": getattr(snap, "status", ""),
            "code": 0,
        }

    async def cancel_order_price(self, order_id: str, deviation: float = 0.01) -> dict:
        """超价撤单：桥侧没有「按偏离价撤」的语义 ⇒ 退化为普通撤单。

        ★ 不做价格计算后伪造成「超价撤成功」：那会让上层的重试逻辑误判。
        """
        return await self.cancel_order(order_id)

    # ---------------- 探活 / 订阅 ----------------
    def is_connected(self) -> bool:
        return self._bridge.is_connected()

    async def test_connection(self) -> dict:
        info = await self._c.test_connection()
        self._bridge._stamp_agent_ok()      # 往返成功 ＝ 存活证据
        base = {"connected": True, "detail": "bigqmt agent reachable"}
        if isinstance(info, dict):
            base.update({k: v for k, v in info.items() if k not in base})
        return base

    def subscribe_quote(self, codes: list[str], on_tick) -> None:
        """记录「想要哪些标的的实时行情」（**纯内存，零 IO，同步契约不变**）。

        ★ 本方法**必须**是同步的（``XTQuantGateway`` / ``sync._subscribe_to_qmt``
          都按同步调用、不 await），因此这里**绝不做 IO**：既不能 ``_run_sync``
          （``_subscribe_to_qmt`` 跑在事件循环线程上，文件桥一次往返可达 30s，
          会把整个后端卡住），也不能火忘一个 task（失败无人知、C18 的
          「成功后才标记已订阅」就退化成谎话）。

        正确分工：本方法只登记意图；``BigQmtBridge`` 的事件泵每秒对账一次
        （``_reconcile_quote_subscriptions``），把整集经 ``SUB_QUOTE`` 下发给
        agent 并记录结果。于是订阅是**幂等 + 自愈**的：agent 重启、首次下发失败、
        泵晚启动，都会在下一轮自动补齐。

        ★ 与 mini **具体适配器**的**已声明**签名差异：``XTPQuantAdapter``
        （mini 侧的 ``BrokerAdapter`` 实现）多一个可选 ``period``。本网关**故意不接**：
        agent 侧的落地形态是轮询 ``get_full_tick``（tick 粒度），把周期传过去也只会
        被无声忽略 —— 「收下却不生效」比「根本没有这个参数」更糟（谎话）。
        本网关与 **ABC**（``XTQuantGateway``，即真正的契约）签名完全一致；产线唯一
        调用点 ``sync/__init__.py`` 也只传 2 个位置参数。该差异由
        ``test_bigqmt_gateway_widening_over_the_mini_contract_is_declared`` 登记并守卫。

        ``on_tick`` 被有意忽略：进程内回调对象无法跨线（见 ``GenericConnector.
        subscribe_quote`` 的 UnsupportedOp 分支）；实时行情经事件泵 / EventPort 回传。
        """
        self._bridge.want_quotes(codes)
