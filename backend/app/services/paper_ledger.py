"""模拟盘账本的**查询契约映射**（PaperEngine → /trade/* 与 /account/status 契约形状）。

为什么需要这一层
----------------
paper 模式的下单早已按模式分流（``/trade/order`` 2026-09-20 修复：只有 live
才要求券商），但**查询端点没有**：``/trade/orders``、``/trade/deals``、
``/trade/positions``、``/account/status`` 一律 ``_need()`` 无条件捅券商。
实测形态（2026-10-12，paper 模式、大QMT 桥已连接、agent 无注入函数）：

    POST /trade/order  → 200 PAPER-9 filled      ← 模拟盘成交成功
    GET  /trade/orders → 503「缺少券商 SDK：未捕获到函数 get_trade_detail_data」

即「**单成交了却永远查不回来**」—— Trade 页三个页签（持仓/委托/成交）在
``catch {}`` 静默兜底下恒为空。同类病灶在账户域：``/account/status`` 同样 503。
这不是券商问题（桥是连着的），是**路由层把模拟盘查询指向了券商**。

本模块把 PaperEngine 账本（``paper_trades`` / ``paper_positions`` /
``paper_cash``）映射为平台契约形状，供上述端点在 paper 模式返回：

    positions → code / name / volume / avail / cost / price / market_value
                / profit / profit_pct   （与 enrich_positions 后的券商行同口径）
    orders    → order_id(PAPER-{id}) / code / side / price / volume / filled
                / status(恒 filled) / time
    deals     → deal_id(PAPER-{id}) / order_id(PAPER-{id}) / code / side /
                price / volume / amount / time

三条诚实性约束（与项目「零 mock」一致）：
1. ``order_id`` 必须与 ``PaperEngine.submit_order`` 返回的
   ``PAPER-{trade_id}`` 逐字符一致 —— 否则撤单/对账会认不出这是模拟盘委托
   （``is_paper_order_id`` 的前缀判据）；
2. **不补 remark**：``paper_orders``（信号受理日志）与 ``paper_trades``
   （成交账本）是两张各自自增的表，没有 trade id 关联，按 id join 等于
   臆造因果；
3. PaperEngine 撮合是即时的（拒绝或成交，无挂单中间态），因此 orders 恒为
   ``filled`` —— 这不是简化，是引擎的真实语义。
"""
from __future__ import annotations

from typing import Any

from engines.paper_engine import PAPER_ORDER_PREFIX

__all__ = [
    "paper_positions", "paper_orders", "paper_deals", "paper_account_status",
]


def _paper_id(trade_id: Any) -> str:
    """成交行 id → 模拟盘委托号（与 submit_order 返回的 order_id 同构）。"""
    return f"{PAPER_ORDER_PREFIX}{trade_id}"


def paper_positions(engine, symbol: str = "") -> list[dict]:
    """PaperEngine 持仓 → Position 契约形状（前端 Position 接口字段集）。

    ``symbol`` 空 = 全部；非空时按代码精确匹配，或匹配去交易所后缀的裸代码
    （查 ``513090`` 命中 ``513090.SH`` 持仓）——与用户输入习惯一致。
    """
    want = str(symbol or "").strip()
    want_bare = want.split(".")[0].upper() if want else ""
    out: list[dict] = []
    for p in engine.get_positions():
        code = str(p.get("code") or "")
        if want and code != want and code.split(".")[0].upper() != want_bare:
            continue
        out.append({
            "code": code,
            "name": p.get("name") or code,
            "volume": p.get("volume") or 0,
            # PaperEngine 无资金冻结概念（现金校验通过即全额可卖）⇒ avail=volume
            "avail": p.get("volume") or 0,
            "cost": p.get("avg_cost"),
            "price": p.get("last_price"),
            "market_value": p.get("market_value"),
            "profit": p.get("unrealized_pnl"),
            # ★ profit_pct 契约口径是**百分数**（5.28 表示 5.28%），而
            #   PaperEngine 的 ``profit_ratio`` 是**分数**（0.0528 = 5.28%）。
            #   实盘路径 positions.py 用 ``(price-cost)/cost*100`` 产出百分数，
            #   前端 format.ts 的 fmtPct 直接当百分数渲染 —— 这里必须 ×100
            #   对齐，否则模拟盘盈亏比显示 +0.05% 而实盘显示 +5.28%，
            #   同一字段两种口径差 100 倍。（2026-10-13 R2）
            #   ratio 为 None（成本缺失，无盈亏可言）时保持 None，不硬凑 0。
            "profit_pct": (
                round(float(p.get("profit_ratio") or 0) * 100, 2)
                if p.get("profit_ratio") is not None else None),
        })
    return out


def paper_orders(engine, limit: int = 50) -> list[dict]:
    """PaperEngine 成交账本 → Order 契约形状（全部为已成交）。

    PaperEngine 没有挂单中间态：``submit_order`` 校验（现金/涨跌停）不过即
    拒绝，通过即当日按价成交并落 ``paper_trades``。因此每一行都是 ``filled``，
    ``filled == volume``。
    """
    out: list[dict] = []
    for t in engine.get_trades(limit):
        tid = t.get("id")
        out.append({
            "order_id": _paper_id(tid),
            "code": t.get("code") or "",
            "side": t.get("side") or "",
            "price": t.get("price") or 0,
            "volume": t.get("volume") or 0,
            "filled": t.get("volume") or 0,
            "status": "filled",
            "time": t.get("ts") or "",
        })
    return out


def paper_deals(engine, limit: int = 50) -> list[dict]:
    """PaperEngine 成交账本 → Deal 契约形状。

    模拟盘一笔委托即一笔成交（无拆单），``deal_id`` 与 ``order_id`` 同值
    （都是 ``PAPER-{id}``）—— 这不是偷懒：成交号在券商侧本就常缺失，
    ``normalize_deal`` 的兜底规则同样是 ``order_id(+seq)``。
    """
    out: list[dict] = []
    for t in engine.get_trades(limit):
        tid = t.get("id")
        oid = _paper_id(tid)
        price = t.get("price") or 0
        volume = t.get("volume") or 0
        out.append({
            "deal_id": oid,
            "order_id": oid,
            "code": t.get("code") or "",
            "side": t.get("side") or "",
            "price": price,
            "volume": volume,
            "amount": round(price * volume, 2),
            "time": t.get("ts") or "",
        })
    return out


def paper_account_status(engine) -> dict:
    """PaperEngine 账户 → /account/status 契约形状。

    ``connected`` 恒为 False：本响应描述的是**模拟盘账本**，此时并没有任何
    券商账户在被展示（paper 模式本就不连券商）。若这里填 True，消费方会把
    模拟盘持仓当成券商真实持仓 —— 正是「假成功」家族要防的形态。
    ``mode: "paper"`` 显式标注来源，供消费方分流（MCP / 脚本 / 未来页面）。
    """
    acc = engine.get_account()
    positions = paper_positions(engine)
    return {
        "connected": False,
        "mode": "paper",
        "assets": acc.get("total_assets", 0.0),
        "cash": acc.get("cash", 0.0),
        "position_count": acc.get("position_count", len(positions)),
        "positions": positions,
    }
