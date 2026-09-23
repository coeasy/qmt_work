"""目标持仓差量同步（借鉴 jq2qmt 目标持仓算法）。

- 提交目标持仓（code -> target_volume 或 code -> weight）
- 对比当前真实持仓，生成买卖差量信号
- 经 SignalRouter 统一路由（支持 paper/dry_run 旁路预演）
- 支持按权重 + 总资产自动计算目标股数
"""
from __future__ import annotations

import json
import logging
from typing import Any
from core.clock import now_iso
from core.quote_fields import pick_last_price

log = logging.getLogger("qmt_work.target")

#: mode 别名归一化表。
#:
#: 前端界面提供「股数 / 金额 / 比例」三个选项，实际发出的是
#: ``shares`` / ``amount`` / ``ratio``；而 MCP 工具与历史调用用 ``volume`` / ``weight``。
#: ⚠️ 不做归一化的后果（R25 实测缺陷）：``ratio`` 会掉进「按股数」分支 ——
#: 权重 0.3 被当作 **0.3 股**，``int(0.3) == 0`` ⇒ 对全部现有持仓生成
#: **卖出清仓**信号；``amount`` 则把「金额」当「股数」，下单量级直接错几个数量级。
_MODE_ALIASES: dict[str, str] = {
    "volume": "volume", "shares": "volume",
    "weight": "weight", "ratio": "weight",
    "amount": "amount",
}


class TargetPortfolioEngine:
    def __init__(self, manager, signal_router=None, db=None):
        self._manager = manager
        self._router = signal_router
        self._db = db

    async def sync(self, targets: dict[str, Any], total_capital: float = 0.0,
                   mode: str = "volume", broker_id: str = "",
                   dry_run: bool = False) -> dict:
        """差量同步。

        targets: {code: target_volume} / {code: weight(0~1)} / {code: 目标金额}
        mode: 三种语义（含别名，见 ``_MODE_ALIASES``）

            - 股数：``shares`` / ``volume`` —— targets 的值是目标**股数**
            - 比例：``ratio``  / ``weight`` —— targets 的值是**权重**（0~1），
              按 ``total_capital``（缺省取账户总资产）折算目标股数
            - 金额：``amount``              —— targets 的值是目标**金额**，按最新价折算

        dry_run: True 时只返回计划，不触发信号

        ⚠️ 未知 mode **直接报错**，不再静默按股数处理 —— 静默兜底正是
        「比例被当成股数 ⇒ 权重 0.3 → 0 股 → 全仓清仓」这类事故的成因。
        """
        norm = _MODE_ALIASES.get(str(mode or "").strip().lower())
        if norm is None:
            return {"ok": False,
                    "reason": f"未知 mode：{mode!r}（可选 shares/amount/ratio，"
                              f"或 volume/weight）"}
        mode = norm

        b = self._manager.bridge(broker_id or None)
        if b is None:
            # ★ broker_unavailable：路由据此返 **503 + 「去连接券商」引导**（而不是 400）。
            #   与 `gateway/signal_router.py` 同一套约定 —— 归因边界只认这一个标志。
            #   缺了它，路由只能「一律 400 或一律 503」，两者都会把用户带偏。
            return {"ok": False, "reason": "未连接券商客户端",
                    "broker_unavailable": True}
        # 当前持仓
        positions = await b.call(b.gateway.get_positions)
        cur: dict[str, int] = {}
        for p in positions or []:
            code = (p.get("code") or "").upper()
            vol = int(p.get("volume", 0) or 0)
            if code:
                cur[code] = vol
        resolved = dict(targets)
        if mode == "weight":
            # 权重模式：按总资产 + 最新价折算目标股数
            cash = await b.call(b.gateway.get_cash) or {}
            assets = float(cash.get("assets", 0) or 0)
            capital = total_capital or assets
            for code, w in list(resolved.items()):
                w = float(w)
                if w <= 0:
                    resolved[code] = 0
                    continue
                # ★ 取价必须走唯一入口（core.quote_fields）：此前内联
                #   `q.get("last")` 在只给 lastPrice 的源上恒取不到价 ⇒ 静默
                #   `continue` ⇒ 该标的**被悄悄跳过**（表现为「有些票根本没调仓」）。
                price = pick_last_price(await b.call(b.gateway.get_quote, code) or {})
                if not price:
                    continue
                target_vol = int(capital * w / price / 100) * 100
                resolved[code] = max(0, target_vol)
        elif mode == "amount":
            # 金额模式：targets 的值是目标金额，按最新价折算股数
            for code, amt in list(resolved.items()):
                amt = float(amt)
                if amt <= 0:
                    resolved[code] = 0
                    continue
                price = pick_last_price(await b.call(b.gateway.get_quote, code) or {})
                if not price:
                    continue
                resolved[code] = max(0, int(amt / price / 100) * 100)
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
                  "ts": now_iso()}
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

    def save_plan(self, name: str, weights: dict, pid: int = 0) -> int:
        """新建或更新计划；``pid`` 非空即更新（与 notifications / alerts / webhooks
        同一套「带 id 即更新」约定 —— 不新增路由就不会动到契约基线）。

        ⚠️ 更新时**绝不**覆盖 ``status``：计划可能是 ``active``（已启用/已下发），
        一次改名把它打回 ``draft`` 会让「正在按这个计划调仓」的事实悄悄消失。
        """
        if self._db is None:
            return 0
        payload = {
            "name": name,
            "weights_json": json.dumps(weights, ensure_ascii=False),
            "updated_at": now_iso(),
        }
        pid = int(pid or 0)
        if pid:
            row = self._db.query_one("SELECT id FROM target_portfolios WHERE id=?", (pid,))
            if row is None:
                return 0        # 更新不存在的计划 ⇒ 返回 0，不静默新建
            fields = [f"{k}=?" for k in payload]
            self._db.execute(
                f"UPDATE target_portfolios SET {','.join(fields)} WHERE id=?",
                (*payload.values(), pid))
            return pid
        payload["status"] = "draft"
        payload["created_at"] = now_iso()
        return self._db.insert("target_portfolios", payload)

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
    from core.state import state
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
