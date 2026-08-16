from app.routes._common import ok, err, state, _need, _call, BrokerError

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import time



router = APIRouter()

@router.get("/account/status")
async def account_status(conn_id: str = ""):
    b = _need(conn_id or None)
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    cash = await _call(b, b.gateway.get_cash)
    if isinstance(cash, dict) and cash.get("code"):
        return cash
    pos = await _call(b, b.gateway.get_positions)
    if isinstance(pos, dict) and pos.get("code"):
        return pos
    pos_value = sum(p.get("market_value", 0.0) for p in pos)
    return ok({"connected": b.gateway.is_connected(),
               "assets": round((cash.get("assets", 0.0) or 0.0) + pos_value, 2),
               "cash": cash.get("cash", 0.0), "position_count": len(pos),
               "positions": pos})

@router.get("/account/aggregate")
async def account_aggregate():
    """多账户聚合视图：遍历所有已连接券商账户，汇总资产/持仓/委托/成交。"""
    conns = [c for c in state.broker_manager.all_connections() if c.connected]
    if not conns:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    accounts = []
    positions_by_code: dict[str, dict] = {}
    total_assets = total_cash = total_mv = 0.0
    orders = deals = 0
    for c in conns:
        try:
            acc = await c.bridge.call(c.adapter.get_account)
            pos = await c.bridge.call(c.adapter.get_positions)
            o = await c.bridge.call(c.adapter.get_orders)
            d = await c.bridge.call(c.adapter.get_deals)
        except BrokerError as exc:
            accounts.append({"conn_id": c.cfg.conn_id, "name": c.cfg.name,
                             "broker": c.adapter.broker_name, "error": str(exc)})
            continue
        assets = float(acc.get("assets") or 0)
        cash = float(acc.get("cash") or 0)
        mv = float(acc.get("market_value") or 0)
        total_assets += assets
        total_cash += cash
        total_mv += mv
        orders += len(o or [])
        deals += len(d or [])
        accounts.append({
            "conn_id": c.cfg.conn_id, "name": c.cfg.name,
            "broker": c.adapter.broker_name, "account_id": c.cfg.account_id,
            "account_type": c.cfg.account_type,
            "assets": assets, "cash": cash, "market_value": mv,
            "position_count": len(pos or []),
        })
        for p in (pos or []):
            code = p.get("code", "")
            if not code:
                continue
            cur = positions_by_code.setdefault(code, {"code": code, "name": p.get("name", code),
                                                      "volume": 0, "market_value": 0.0})
            cur["volume"] += int(p.get("volume", 0) or 0)
            cur["market_value"] += float(p.get("market_value", 0) or 0)
    return ok({
        "account_count": len(accounts), "total_assets": round(total_assets, 2),
        "total_cash": round(total_cash, 2), "total_market_value": round(total_mv, 2),
        "orders_count": orders, "deals_count": deals,
        "accounts": accounts,
        "positions": sorted(positions_by_code.values(),
                            key=lambda x: x["market_value"], reverse=True),
    })

@router.get("/account/pnl")
async def account_pnl():
    """净值/月度收益（账户快照数据仓库；无数据时返回空序列）。"""
    rows = state.db.query(
        "SELECT ts, net_value FROM account_snapshot ORDER BY ts DESC LIMIT 50")
    return ok({"net_value_series": [{"ts": r["ts"], "net_value": r["net_value"]} for r in reversed(rows)]})

@router.get("/account/slippage")
async def account_slippage(code: str = "600519.SH", conn_id: str = ""):
    """滑点分析（EzQmt cal_deal_comm：成交价 vs 当日 open/close/avg 基点差）。"""
    b = _need(conn_id or None)
    if b is None:
        return err(503, "未连接任何券商客户端。")
    deals = await _call(b, b.gateway.get_deals)
    if isinstance(deals, dict) and deals.get("code"):
        return deals
    deals = [d for d in deals if d.get("code") == code]
    kline = await _call(b, b.gateway.get_kline, code, "1d", 250)
    if isinstance(kline, dict) and kline.get("code"):
        return kline
    bar_by_day = {d.get("time", "")[:10]: d for d in kline}
    rows = []
    for d in deals:
        bar = bar_by_day.get(str(d.get("time", ""))[:10])
        if not bar or not bar.get("volume"):
            continue
        price = d.get("price") or 0
        vwap = (bar.get("amount") or 0) / bar["volume"] if bar.get("volume") else None
        def bps(ref):
            return round((price - ref) / ref * 1e4, 2) if ref else None
        rows.append({"time": d.get("time"), "side": d.get("direction"), "price": price,
                     "slippage_open_bps": bps(bar.get("open") or 0),
                     "slippage_close_bps": bps(bar.get("close") or 0),
                     "slippage_avg_bps": bps(vwap)})
    avg = (sum(r["slippage_avg_bps"] for r in rows if r["slippage_avg_bps"] is not None) / len(rows)) if rows else 0
    return ok({"code": code, "samples": rows, "avg_abs_slippage_avg_bps": round(abs(avg), 2)})


# ---------------- 多账户网格视图（P0：统一看板） ----------------

def _account_row(conn) -> dict:
    """构造单个连接的账户看板行（失败则带 error）。"""
    base = {"conn_id": conn.cfg.conn_id, "name": conn.cfg.name,
            "broker": conn.adapter.broker_name, "broker_id": conn.cfg.broker_id,
            "account_id": conn.cfg.account_id, "account_type": conn.cfg.account_type,
            "connected": bool(conn.connected),
            "assets": 0.0, "cash": 0.0, "market_value": 0.0,
            "position_count": 0, "order_count": 0, "deal_count": 0, "error": ""}
    if not conn.connected:
        base["error"] = conn.last_error or "未连接"
        return base
    try:
        acc = conn.adapter.get_account()
        pos = conn.adapter.get_positions()
        o = conn.adapter.get_orders()
        d = conn.adapter.get_deals()
    except BrokerError as exc:
        base["error"] = str(exc)
        return base
    base["assets"] = float(acc.get("assets") or 0)
    base["cash"] = float(acc.get("cash") or 0)
    base["market_value"] = float(acc.get("market_value") or 0)
    base["position_count"] = len(pos or [])
    base["order_count"] = len(o or [])
    base["deal_count"] = len(d or [])
    return base


@router.get("/account/grid")
async def account_grid():
    """多券商 / 多账户统一看板：逐账户指标行 + 按标的汇总的持仓矩阵 + 总资产合计。

    未连接的账户亦列出（error 字段说明原因），便于统一运维视图。
    """
    conns = state.broker_manager.all_connections()
    if not conns:
        return err(503, "尚未添加任何券商连接：请到「券商连接」页添加券商。")
    rows = [_account_row(c) for c in conns]
    total_assets = sum(r["assets"] for r in rows)
    total_cash = sum(r["cash"] for r in rows)
    total_mv = sum(r["market_value"] for r in rows)
    # 按标的汇总持仓（跨账户）
    by_code: dict[str, dict] = {}
    for c in conns:
        if not c.connected:
            continue
        try:
            pos = c.adapter.get_positions()
        except BrokerError:
            continue
        for p in (pos or []):
            code = p.get("code", "")
            if not code:
                continue
            cell = by_code.setdefault(code, {
                "code": code, "name": p.get("name", code),
                "total_volume": 0, "total_market_value": 0.0, "accounts": []})
            vol = int(p.get("volume", 0) or 0)
            mv = float(p.get("market_value", 0) or 0)
            cell["total_volume"] += vol
            cell["total_market_value"] += mv
            cell["accounts"].append({"conn_id": c.cfg.conn_id, "name": c.cfg.name,
                                     "volume": vol, "market_value": mv})
    positions = sorted(by_code.values(), key=lambda x: x["total_market_value"], reverse=True)
    return ok({
        "account_count": len(rows),
        "connected_count": sum(1 for r in rows if r["connected"]),
        "total_assets": round(total_assets, 2),
        "total_cash": round(total_cash, 2),
        "total_market_value": round(total_mv, 2),
        "accounts": rows,
        "positions": positions,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })


# ---------------- 批量操作（P0：跨账户统一执行） ----------------

def _expand_batch_orders(body: dict) -> list[dict]:
    """将请求体展开为逐账户订单列表。

    支持两种形态：
    - {"orders":[{conn_id,code,direction,volume,price,price_type}, ...]}
    - {"conn_ids":[...], code, direction, volume, price, price_type}（同一指令广播多账户）
    """
    explicit = body.get("orders")
    if isinstance(explicit, list) and explicit:
        out = []
        for o in explicit:
            out.append({
                "conn_id": str(o.get("conn_id", "")),
                "code": str(o.get("code", "")).strip().upper(),
                "direction": (o.get("direction") or "buy").lower(),
                "volume": int(o.get("volume", 0)),
                "price": float(o.get("price", 0) or 0),
                "price_type": o.get("price_type", "limit"),
            })
        return out
    conn_ids = body.get("conn_ids") or []
    if isinstance(conn_ids, str):
        conn_ids = [conn_ids]
    shared = {
        "code": str(body.get("code", "")).strip().upper(),
        "direction": (body.get("direction") or "buy").lower(),
        "volume": int(body.get("volume", 0)),
        "price": float(body.get("price", 0) or 0),
        "price_type": body.get("price_type", "limit"),
    }
    return [dict(conn_id=str(cid), **shared) for cid in conn_ids]


@router.post("/account/batch/order")
async def account_batch_order(body: dict):
    """批量下单（跨账户统一执行）：每个订单独立走风控 + 对应连接下单。

    body:
      {"orders":[{"conn_id","code","direction","volume","price","price_type"}]}
      或 {"conn_ids":[...],"code","direction","volume","price","price_type"}
    返回逐单结果（含风控拒绝/连接缺失/成功 order_id）。
    """
    orders = _expand_batch_orders(body)
    if not orders:
        return err(400, "orders 或 conn_ids 至少提供一个")
    results = []
    ok_count = 0
    for o in orders:
        rec = {"conn_id": o["conn_id"], "code": o["code"], "direction": o["direction"],
               "volume": o["volume"], "status": "rejected", "detail": ""}
        if not o["code"] or o["direction"] not in ("buy", "sell") or o["volume"] <= 0:
            rec["detail"] = "参数非法（code/direction/volume）"
            results.append(rec)
            continue
        b = state.broker_manager.bridge(o["conn_id"] or None)
        if b is None:
            rec["detail"] = "连接不存在或未连接"
            results.append(rec)
            continue
        okc, reason = state.risk.check_order(
            o["code"], o["price"] if o["price"] > 0 else 100.0, o["volume"], o["direction"])
        if not okc:
            rec["detail"] = f"风控拒绝：{reason}"
            results.append(rec)
            continue
        res = await _call(b, b.gateway.place_order, o["code"], o["direction"],
                          o["price_type"], o["price"], o["volume"], "batch", "")
        if isinstance(res, dict) and res.get("code", 0) == 0:
            rec["status"] = "submitted"
            rec["order_id"] = res.get("order_id")
            rec["detail"] = "ok"
            ok_count += 1
        else:
            rec["detail"] = (res.get("message") if isinstance(res, dict) else str(res))
        results.append(rec)
    state.db.audit("trading", "account.batch_order", "", {"count": len(orders),
                   "ok": ok_count}, "ok")
    return ok({"total": len(orders), "ok": ok_count, "results": results})


@router.post("/account/batch/cancel")
async def account_batch_cancel(body: dict):
    """批量撤单（跨账户）：items=[{conn_id,order_id}] 或 conn_ids+order_id 广播。"""
    items = body.get("items")
    if isinstance(items, list) and items:
        targets = [{"conn_id": str(i.get("conn_id", "")), "order_id": str(i.get("order_id", ""))}
                   for i in items]
    else:
        oid = str(body.get("order_id", ""))
        cids = body.get("conn_ids") or []
        if isinstance(cids, str):
            cids = [cids]
        targets = [{"conn_id": str(c), "order_id": oid} for c in cids]
    if not targets:
        return err(400, "items 或 conn_ids+order_id 至少提供一个")
    results = []
    ok_count = 0
    for t in targets:
        rec = {"conn_id": t["conn_id"], "order_id": t["order_id"],
               "status": "failed", "detail": ""}
        if not t["order_id"]:
            rec["detail"] = "order_id 为空"
            results.append(rec)
            continue
        b = state.broker_manager.bridge(t["conn_id"] or None)
        if b is None:
            rec["detail"] = "连接不存在或未连接"
            results.append(rec)
            continue
        res = await _call(b, b.gateway.cancel_order, t["order_id"])
        if isinstance(res, dict) and res.get("code", 0) == 0:
            rec["status"] = "canceled"
            rec["detail"] = "ok"
            ok_count += 1
        else:
            rec["detail"] = (res.get("message") if isinstance(res, dict) else str(res))
        results.append(rec)
    return ok({"total": len(targets), "ok": ok_count, "results": results})


@router.post("/account/batch/reconnect")
async def account_batch_reconnect(body: dict):
    """批量重连指定账户（崩溃恢复 / 换会话后一键拉起）。"""
    cids = body.get("conn_ids") or []
    if isinstance(cids, str):
        cids = [cids]
    if not cids:
        # 默认重连所有已标记 active 的连接
        cids = [c.cfg.conn_id for c in state.broker_manager.all_connections()
                if c.cfg.active]
    results = []
    for cid in cids:
        rec = {"conn_id": cid, "status": "failed", "detail": ""}
        try:
            state.broker_manager.connect(cid)
            conn = state.broker_manager._conns.get(cid)
            if conn and conn.connected:
                rec["status"] = "connected"
                rec["detail"] = "ok"
            else:
                rec["detail"] = "重连后仍不可用"
        except (KeyError, BrokerError) as exc:
            rec["detail"] = str(exc)
        results.append(rec)
    return ok({"total": len(cids), "results": results})


# ---------------- 分仓再平衡（EzQmt Reblance） ----------------

