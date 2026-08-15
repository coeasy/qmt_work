"""目标持仓差量同步（借鉴 jq2qmt 目标持仓算法）。

- 提交目标持仓（code -> target_volume 或 code -> weight）
- 对比当前真实持仓，生成买卖差量信号
- 经 SignalRouter 统一路由（支持 paper/dry_run 旁路预演）
- 支持按权重 + 总资产自动计算目标股数
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("qmt_work.target")


class TargetPortfolioEngine:
    def __init__(self, manager, signal_router=None, db=None):
        self._manager = manager
        self._router = signal_router
        self._db = db

    async def sync(self, targets: dict[str, Any], total_capital: float = 0.0,
                   mode: str = "volume", broker_id: str = "",
                   dry_run: bool = False) -> dict:
        """差量同步。

        targets: {code: target_volume} 或 {code: weight(0~1)}
        mode: volume（按目标股数）/ weight（按权重+总资产计算股数）
        dry_run: True 时只返回计划，不触发信号
        """
        b = self._manager.bridge(broker_id or None)
        if b is None:
            return {"ok": False, "reason": "未连接券商客户端"}
        # 当前持仓
        positions = await b.call(b.gateway.get_positions)
        cur: dict[str, int] = {}
        for p in positions or []:
            code = (p.get("code") or "").upper()
            vol = int(p.get("volume", 0) or 0)
            if code:
                cur[code] = vol
        # 权重模式：按总资产 + 最新价计算目标股数
        resolved = dict(targets)
        if mode == "weight":
            cash = await b.call(b.gateway.get_cash)
            assets = float(cash.get("assets", 0) or 0)
            capital = total_capital or assets
            for code, w in list(resolved.items()):
                w = float(w)
                if w <= 0:
                    resolved[code] = 0
                    continue
                q = await b.call(b.gateway.get_quote, code)
                price = float(q.get("last") or 0)
                if price <= 0:
                    continue
                target_vol = int(capital * w / price / 100) * 100
                resolved[code] = max(0, target_vol)
        # 生成差量
        all_codes = set(resolved) | set(cur)
        plan = []
        signals = []
        for code in sorted(all_codes):
            target = int(resolved.get(code, 0))
            have = int(cur.get(code, 0))
            diff = target - have
            if abs(diff) < 100:
                continue
            side = "buy" if diff > 0 else "sell"
            vol = abs(diff) // 100 * 100
            if vol <= 0:
                continue
            item = {"code": code, "side": side, "volume": vol,
                    "current": have, "target": target, "diff": diff}
            plan.append(item)
            signals.append(item)
        result = {"ok": True, "dry_run": dry_run, "mode": mode,
                  "current": cur, "target": resolved, "plan": plan,
                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if dry_run or not self._router:
            return result
        # 经 SignalRouter 路由每个差量信号
        from gateway.signal_router import Signal
        executed = []
        for s in signals:
            res = await self._router.route(Signal(
                source="rebalance", code=s["code"], side=s["side"],
                volume=s["volume"], price_type="market", remark="目标持仓同步",
                broker_id=broker_id, payload={"target": s["target"], "current": s["current"]}))
            executed.append({"code": s["code"], "result": res})
        result["executed"] = executed
        return result

    def save_plan(self, name: str, weights: dict) -> int:
        if self._db is None:
            return 0
        nid = self._db.insert("target_portfolios", {
            "name": name, "weights_json": json.dumps(weights, ensure_ascii=False),
            "status": "draft", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return nid

    def list_plans(self) -> list[dict]:
        if self._db is None:
            return []
        rows = self._db.query("SELECT * FROM target_portfolios ORDER BY id DESC")
        for r in rows:
            try:
                r["weights"] = json.loads(r.pop("weights_json", "{}"))
            except Exception:  # noqa: BLE001
                r["weights"] = {}
        return rows

    def get_plan(self, pid: int) -> dict | None:
        if self._db is None:
            return None
        r = self._db.query_one("SELECT * FROM target_portfolios WHERE id=?", (pid,))
        if r:
            try:
                r["weights"] = json.loads(r.pop("weights_json", "{}"))
            except Exception:  # noqa: BLE001
                r["weights"] = {}
        return r

    def delete_plan(self, pid: int):
        if self._db is not None:
            self._db.execute("DELETE FROM target_portfolios WHERE id=?", (pid,))


def _engine():
    from app.state import state
    if state.signal_router is None:
        from xtquant_client.base import BrokerError
        raise BrokerError("信号路由未初始化")
    return TargetPortfolioEngine(state.broker_manager, state.signal_router, state.db)


def register_target_portfolio_tools(mcp):
    @mcp.tool()
    async def target_portfolio_sync(targets_json: str, mode: str = "volume",
                                    total_capital: float = 0.0,
                                    dry_run: bool = False,
                                    broker_id: str = "") -> dict:
        """目标持仓差量同步。targets_json: {"代码": 目标股数或权重}。dry_run=True 只预演。"""
        import json as _json
        try:
            targets = _json.loads(targets_json)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"targets_json 解析失败：{exc}"}
        targets = {k.upper(): v for k, v in targets.items()}
        return await _engine().sync(targets, total_capital, mode, broker_id, dry_run)

    @mcp.tool()
    async def target_portfolio_save(name: str, weights_json: str) -> dict:
        """保存目标持仓方案。"""
        import json as _json
        try:
            w = _json.loads(weights_json)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"weights_json 解析失败：{exc}"}
        nid = _engine().save_plan(name, w)
        return {"ok": True, "id": nid}

    @mcp.tool()
    async def target_portfolio_list() -> list[dict]:
        """列出已保存的目标持仓方案。"""
        return _engine().list_plans()
