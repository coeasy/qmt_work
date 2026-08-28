"""统一信号入口 + 物理旁路（paper / dry_run 模式）。

所有交易信号（策略、算法单、条件单、涨停打板、目标持仓同步）统一经 SignalRouter，
根据 mode 决定路由：
- live    真实下单（经风控 + WAL + 通知）
- paper   物理旁路：不真实下单，记录到 paper_orders 表（联调/演练/策略验证）
- dry_run 只返回拟执行计划，不下单不记录

切换模式：POST /config/signal-mode {mode: "live"|"paper"|"dry_run"}
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from gateway.idempotency import single_flight

log = logging.getLogger("qmt_work.signal")


@dataclass
class Signal:
    source: str            # strategy / algo / condition / limitup / rebalance / manual
    code: str
    side: str              # buy / sell
    volume: int
    price: float = 0.0
    price_type: str = "limit"   # limit / market
    remark: str = ""
    broker_id: str = ""
    payload: dict = field(default_factory=dict)


class SignalRouter:
    def __init__(self, manager, risk=None, db=None, wal=None, notifier=None, on_event=None,
                 runtime_config=None):
        from app.config import settings
        self._manager = manager
        self._risk = risk
        self._db = db
        self._wal = wal
        self._notifier = notifier
        self._on_event = on_event
        self._runtime_config = runtime_config
        # P0-1：模式持久化 + 安全默认。启动时从 runtime_config 读取；读取失败或无记录
        # 时默认 paper（而非 live），避免升级/崩溃/自动更新重启后无提示恢复实盘。
        self.mode = self._load_persisted_mode("paper")
        self.threshold = getattr(settings, "signal_confirm_threshold", 100_000.0)
        self.totp_secret = getattr(settings, "totp_secret", "")
        self.totp_digits = getattr(settings, "totp_digits", 6)
        self._pending: dict[str, dict] = {}
        # P2-4：待二次确认令牌 TTL（默认 10 分钟），超时自动清理，防内存泄漏与过期确认
        self._pending_ttl = float(getattr(settings, "signal_confirm_ttl", 600.0) or 600.0)

    def _prune_pending(self) -> None:
        """清理超时未确认的下单令牌（P2-4）。"""
        if not self._pending:
            return
        now = time.time()
        stale = [tok for tok, e in self._pending.items()
                 if now - float(e.get("ts", now)) > self._pending_ttl]
        for tok in stale:
            self._pending.pop(tok, None)

    def _load_persisted_mode(self, fallback: str = "paper") -> str:
        """从 runtime_config 读取持久化信号模式；失败无记录返回安全默认 paper。"""
        rc = self._runtime_config
        if rc is None and self._db is not None:
            # 兼容：未注入 runtime_config 时直接从 runtime_config 表读取
            try:
                row = self._db.query_one("SELECT value FROM runtime_config WHERE key='signal.mode'")
                if row and row.get("value"):
                    val = str(row["value"]).strip().strip("\"")
                    if val in ("live", "paper", "dry_run"):
                        return val
                return fallback
            except Exception:  # noqa: BLE001
                return fallback
        try:
            val = rc.get("signal.mode") if rc is not None else None
        except Exception:  # noqa: BLE001
            val = None
        if val in ("live", "paper", "dry_run"):
            return str(val)
        return fallback

    def set_mode(self, mode: str) -> str:
        if mode not in ("live", "paper", "dry_run"):
            raise ValueError(f"未知信号模式：{mode}")
        old = self.mode
        self.mode = mode
        # P0-1：持久化到 runtime_config，确保重启后保持，绝不静默回退 live。
        if self._runtime_config is not None:
            try:
                self._runtime_config.set_many({"signal.mode": mode})
            except Exception:  # noqa: BLE001
                pass
        elif self._db is not None:
            try:
                import json
                self._db.upsert("runtime_config", {
                    "key": "signal.mode", "value": json.dumps(mode),
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            except Exception:  # noqa: BLE001
                pass
        log.info("signal mode: %s -> %s（已持久化）", old, mode)
        return mode

    async def route(self, sig: Signal, auto_confirm: bool = False) -> dict:
        """统一信号入口：根据 mode 决定真实下单 / 旁路 / 预演 / 二次确认。

        auto_confirm=True 时跳过「大额 TOTP 二次确认挂起」（用于已授权自动化引擎：
        algo/limitup/rebalance/strategy_runtime/condition/position），但仍走完整风控。
        """
        import uuid
        code = (sig.code or "").strip().upper()
        side = (sig.side or "").lower()
        if not code:
            return {"ok": False, "reason": "代码不能为空"}
        if side not in ("buy", "sell"):
            return {"ok": False, "reason": "side 须为 buy/sell"}
        sig.code = code
        sig.side = side
        sig.volume = int(sig.volume)

        # dry_run：只返回计划
        if self.mode == "dry_run":
            plan = {"mode": "dry_run", "signal": sig.__dict__,
                    "would_execute": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self._emit({"type": "signal_dry_run", "data": plan})
            return {"ok": True, **plan}

        # P2-4：清理超时的挂起确认令牌
        self._prune_pending()

        # 阶段 0-B（F13）：市价/无价单用**最新价**估算金额（而非写死的 100.0），
        # 否则大额市价单会绕过 TOTP 二次确认与金额类风控。
        est_price = await self._est_price_async(sig)
        # P0-4：市价单取不到最新行情（est_price=0）时，绝不盲目放行——
        # 手动单挂起人工确认；自动化引擎（auto_confirm=True）直接拒绝。
        is_market = (sig.price_type or "limit").lower() == "market"
        if is_market and est_price <= 0:
            if not auto_confirm:
                import uuid
                token = uuid.uuid4().hex
                self._pending[token] = {"sig": sig.__dict__, "ts": time.time(),
                                        "mode": self.mode, "pending_price_unknown": True}
                self._emit({"type": "signal_pending", "data": {
                    "confirm_token": token, "reason": "无最新行情，无法估算市价单金额",
                    "requires_totp": bool(self.totp_secret), "mode": self.mode}})
                return {"ok": True, "pending_confirmation": True, "confirm_token": token,
                        "pending_price_unknown": True,
                        "reason": "无最新行情，无法估算市价单金额",
                        "mode": self.mode}
            self._audit("signal.rejected", code, sig.__dict__,
                        "无最新行情，无法估算市价单金额")
            return {"ok": False, "reason": "无最新行情，无法估算市价单金额", "mode": self.mode}
        if self._risk is not None:
            ok, reason = self._risk.check_order(code, est_price, sig.volume, side,
                                                price_type=sig.price_type)
            if not ok:
                self._audit("signal.rejected", code, sig.__dict__, reason)
                if self._notifier:
                    await self._notifier.notify("risk.blocked", "风控拦截",
                                                f"{code} {side} {sig.volume} 被拦截：{reason}",
                                                sig.__dict__)
                return {"ok": False, "reason": reason, "mode": self.mode}

        # 大额二次确认：金额超阈值时挂起，待 /signal/confirm 确认
        amount = est_price * sig.volume
        if amount >= self.threshold and self.mode in ("live", "paper") and not auto_confirm:
            token = uuid.uuid4().hex
            self._pending[token] = {"sig": sig.__dict__, "ts": time.time(), "mode": self.mode}
            self._emit({"type": "signal_pending", "data": {
                "confirm_token": token, "amount": round(amount, 2),
                "requires_totp": bool(self.totp_secret), "mode": self.mode}})
            return {"ok": True, "pending_confirmation": True, "confirm_token": token,
                    "amount": round(amount, 2), "requires_totp": bool(self.totp_secret),
                    "mode": self.mode}

        return await self._execute(sig)

    async def _est_price_async(self, sig: Signal) -> float:
        """估算下单金额所用价格：有价用价，市价/无价则取最新行情价。

        P0-4：取不到最新行情时返回 0（此前兜底 100.0，导致大额市价单被低估而绕过
        金额类风控与大额 TOTP 二次确认）。由调用方根据 0 决定「挂起」还是「拒绝」。
        """
        if sig.price and sig.price > 0:
            return float(sig.price)
        b = self._manager.bridge(sig.broker_id or None)
        if b is not None:
            try:
                q = await b.call(b.gateway.get_quote, sig.code)
                if isinstance(q, dict):
                    p = q.get("last") or q.get("ask") or q.get("bid")
                    if p:
                        return float(p)
            except Exception:  # noqa: BLE001
                pass
        return 0.0

    async def submit(self, code: str, side: str, volume: int, price: float = 0.0,
                     price_type: str = "limit", source: str = "manual",
                     broker_id: str = "", remark: str = "", idempotency_key: str = "",
                     auto_confirm: bool = False, payload: dict | None = None) -> dict:
        """统一下单入口（引擎/策略/手动共用）：风控 + 幂等 + TOTP + WAL + 审计 + 真实下单。

        阶段 0-B（F6/F7/F8/F9）：所有引擎（algo/limitup/rebalance/strategy_runtime/
        condition/position）一律经此入口，禁止直接 ``gateway.place_order`` 绕过风控。

        idempotency_key 非空时经单飞（single-flight）幂等：同 key 并发/窗口内重复
        请求只执行一次真实逻辑，其余返回缓存结果并标记 duplicated（阶段 0-B / F1）。
        """
        sig = Signal(source=source, code=str(code), side=str(side),
                     volume=int(volume), price=float(price or 0),
                     price_type=price_type, remark=remark or "", broker_id=broker_id or "",
                     payload=payload or {})
        if idempotency_key:
            async def _run():
                return await self.route(sig, auto_confirm=auto_confirm)
            return await single_flight(f"order:{idempotency_key}", _run)
        return await self.route(sig, auto_confirm=auto_confirm)

    async def _execute(self, sig: Signal) -> dict:
        """确认后/未超阈值时的实际执行（paper 或 live）。"""
        if self.mode == "paper":
            return await self._paper(sig)
        return await self._live(sig)

    async def confirm(self, token: str, totp_code: str = "") -> dict:
        """二次确认：校验 TOTP（若启用）后执行挂起的下单。"""
        from gateway.totp import verify_totp
        self._prune_pending()   # P2-4：先清理超时令牌，避免用过期 entry 误放行
        entry = self._pending.pop(token, None)
        if not entry:
            return {"ok": False, "reason": "确认令牌无效或已过期"}
        if self.totp_secret and not verify_totp(self.totp_secret, totp_code, self.totp_digits):
            return {"ok": False, "reason": "TOTP 校验失败，请重新发起信号"}
        sig = Signal(**entry["sig"])
        # 阶段 0-B（F13）：确认前**重跑风控**——挂起期间风控参数/熔断可能已变化，
        # 不得直接放行。
        est_price = await self._est_price_async(sig)
        if self._risk is not None:
            ok, reason = self._risk.check_order(sig.code, est_price, sig.volume, sig.side)
            if not ok:
                self._audit("signal.rejected", sig.code, sig.__dict__, reason)
                return {"ok": False, "reason": reason, "mode": self.mode}
        res = await self._execute(sig)
        res["confirmed"] = True
        return res

    def pending_count(self) -> int:
        return len(self._pending)

    async def _paper(self, sig: Signal) -> dict:
        if self._db is not None:
            try:
                self._db.insert("paper_orders", {
                    "source": sig.source, "code": sig.code, "side": sig.side,
                    "price": sig.price, "volume": sig.volume,
                    "price_type": sig.price_type, "remark": sig.remark,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("paper order persist failed: %s", exc)
        if self._wal is not None:
            from gateway.wal import WAL
            if isinstance(self._wal, WAL):
                self._wal.append("paper", "signal", sig.code, sig.__dict__)
        self._emit({"type": "signal_paper", "data": sig.__dict__})
        log.info("paper signal: %s %s %s@%s", sig.source, sig.code, sig.side, sig.volume)
        from gateway.metrics import get_metrics
        get_metrics().record_order(sig.side, "paper")
        return {"ok": True, "mode": "paper", "recorded": True, "signal": sig.__dict__}

    async def _live(self, sig: Signal) -> dict:
        b = self._manager.bridge(sig.broker_id or None)
        if b is None:
            return {"ok": False, "reason": "未连接券商客户端", "mode": "live"}
        try:
            res = await b.call_locked(
                b.gateway.place_order, sig.code, sig.side, sig.price_type,
                sig.price, sig.volume, sig.source, sig.remark)
            # 阶段 0-B（F13）：绝不把失败/未确认的下单粉饰成成功。
            # 柜台未返回委托号（超时 unknown）或明确拒单 → ok=False。
            oid = res.get("order_id") if isinstance(res, dict) else None
            rstatus = res.get("status") if isinstance(res, dict) else None
            if not oid or rstatus == "rejected":
                res = res if isinstance(res, dict) else {}
                res["ok"] = False
                res["reason"] = res.get("reason") or "下单未确认（柜台未返回委托号，可能超时或拒单）"
                res["mode"] = "live"
                self._audit("signal.failed", sig.code, sig.__dict__, res["reason"])
                return res
            res["ok"] = True
            res["mode"] = "live"
            from gateway.metrics import get_metrics
            get_metrics().record_order(sig.side, "submitted")
            if self._wal is not None:
                from gateway.wal import WAL
                if isinstance(self._wal, WAL):
                    self._wal.append("order", "signal", str(res.get("order_id", "")),
                                     {"source": sig.source, "code": sig.code, "side": sig.side,
                                      "price": sig.price, "volume": sig.volume,
                                      "order_id": res.get("order_id")})
            if self._notifier:
                await self._notifier.notify("order.filled", "委托已提交",
                                            f"{sig.code} {sig.side} {sig.volume}@{sig.price}",
                                            {"order_id": res.get("order_id"), **sig.__dict__})
            self._emit({"type": "signal_live", "data": res})
            self._audit("signal.live", sig.code, sig.__dict__,
                        f"order_id={res.get('order_id')}")
            return res
        except Exception as exc:  # noqa: BLE001
            from gateway.metrics import get_metrics
            get_metrics().record_order(sig.side, "error")
            if self._notifier:
                await self._notifier.notify("order.error", "委托失败",
                                            f"{sig.code} {sig.side} 失败：{exc}", sig.__dict__)
            self._audit("signal.failed", sig.code, sig.__dict__, str(exc))
            return {"ok": False, "reason": str(exc), "mode": "live"}

    def _emit(self, event: dict):
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                pass

    def _audit(self, action: str, target: str, params: dict, result: str):
        if self._db is not None:
            try:
                self._db.audit("signal", action, target, params, result)
            except Exception:  # noqa: BLE001
                pass
