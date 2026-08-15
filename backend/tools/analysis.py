"""绩效归因与滑点分析（覆盖 EzQmt smy.py：cal_deal_comm 滑点 / cal_contri 收益归因 / pnl_monthly）。

- 滑点：逐笔成交价 vs 当日 open/close/avg(VWAP) 的基点差（bps）
- 收益归因：按标的（策略）汇总盈亏贡献
- 月度收益：按自然月聚合净值
所有数据来自券商真实账户/成交/行情，无假数据。
"""
from app.db import get_db
from . import get_bridge


def register_analysis_tools(mcp):
    @mcp.tool()
    async def analyze_slippage(symbol: str, broker_id: str = "") -> dict:
        """滑点分析：该标的逐笔成交价 vs 当日 open/close/avg(VWAP) 的基点差。"""
        b = get_bridge(broker_id or None)
        deals = await b.call(b.gateway.get_deals)
        deals = [d for d in deals if d.get("code") == symbol]
        kline = await b.call(b.gateway.get_kline, symbol, "1d", 250)
        bar_by_day = {d.get("time", "")[:10]: d for d in kline}
        rows = []
        for d in deals:
            day = str(d.get("time", ""))[:10]
            bar = bar_by_day.get(day)
            if not bar or not bar.get("volume"):
                continue
            price = d.get("price") or 0
            vwap = (bar.get("amount") or 0) / bar["volume"] if bar.get("volume") else None
            open_p = bar.get("open") or 0
            close_p = bar.get("close") or 0
            def bps(ref):
                return round((price - ref) / ref * 1e4, 2) if ref else None
            rows.append({
                "time": d.get("time"), "side": d.get("direction"), "price": price,
                "slippage_open_bps": bps(open_p),
                "slippage_close_bps": bps(close_p),
                "slippage_avg_bps": bps(vwap),
            })
        avg = (sum(r["slippage_avg_bps"] for r in rows if r["slippage_avg_bps"] is not None) / len(rows)) if rows else 0
        return {"symbol": symbol, "samples": rows, "avg_abs_slippage_avg_bps": round(abs(avg), 2)}

    @mcp.tool()
    async def analyze_contribution(broker_id: str = "") -> dict:
        """收益归因：按标的汇总当日成交的盈亏贡献（买入成本 vs 卖出/现价）。"""
        b = get_bridge(broker_id or None)
        deals = await b.call(b.gateway.get_deals)
        by_code: dict[str, float] = {}
        for d in deals:
            code = d.get("code")
            side = d.get("direction")
            p = d.get("price") or 0
            v = d.get("volume") or 0
            # 简化：买入记成本负、卖出记收入正
            by_code[code] = by_code.get(code, 0.0) + (p * v if side == "sell" else -p * v)
        return {"by_code": {c: round(v, 2) for c, v in by_code.items()},
                "total": round(sum(by_code.values()), 2)}

    @mcp.tool()
    async def monthly_pnl() -> dict:
        """月度收益：从账户快照聚合逐月净值（需券商连接并积累了快照）。"""
        db = get_db()
        rows = db.query("SELECT ts, net_value FROM account_snapshot ORDER BY ts")
        by_month: dict[str, list[float]] = {}
        for r in rows:
            if not r["net_value"]:
                continue
            m = r["ts"][:7]
            by_month.setdefault(m, []).append(r["net_value"])
        out = []
        for m in sorted(by_month):
            series = by_month[m]
            out.append({"month": m, "start": round(series[0], 2),
                        "end": round(series[-1], 2),
                        "return": round(series[-1] / series[0] - 1, 4) if series[0] else 0})
        return {"months": out}

    @mcp.tool()
    async def net_value_series() -> dict:
        """净值序列（来自账户快照数据仓库）。"""
        db = get_db()
        rows = db.query("SELECT ts, net_value FROM account_snapshot ORDER BY ts")
        return {"series": [{"ts": r["ts"], "net_value": r["net_value"]} for r in rows]}
