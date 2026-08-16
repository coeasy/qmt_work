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
    def __init__(self, manager, risk=None, db=None, wal=None, notifier=None, on_event=None):
        from app.config import settings
        self._manager = manager
        self._risk = risk
        self._db = db
        self._wal = wal
        self._notifier = notifier
        self._on_event = on_event
        self.mode = "live"   # live / paper / dry_run
        self.threshold = getattr(settings, "signal_confirm_threshold", 100_000.0)
        self.totp_secret = getattr(settings, "totp_secret", "")
        self.totp_digits = getattr(settings, "totp_digits", 6)
        self._pending: dict[str, dict] = {}

    def set_mode(self, mode: str) -> str:
        if mode not in ("live", "paper", "dry_run"):
            raise ValueError(f"未知信号模式：{mode}")
        old = self.mode
        self.mode = mode
        log.info("signal mode: %s -> %s", old, mode)
        return mode

    async def route(self, sig: Signal) -> dict:
        """统一信号入口：根据 mode 决定真实下单 / 旁路 / 预演 / 二次确认。"""
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

        # 风控检查
        if self._risk is not None:
            ok, reason = self._risk.check_order(code, sig.price if sig.price > 0 else 100.0,
                                                sig.volume, side)
            if not ok:
                self._audit("signal.rejected", code, sig.__dict__, reason)
                if self._notifier:
                    await self._notifier.notify("risk.blocked", "风控拦截",
                                                f"{code} {side} {sig.volume} 被拦截：{reason}",
                                                sig.__dict__)
                return {"ok": False, "reason": reason, "mode": self.mode}

        # 大额二次确认：金额超阈值时挂起，待 /signal/confirm 确认
        est_price = sig.price if sig.price > 0 else 100.0
        amount = est_price * sig.volume
        if amount >= self.threshold and self.mode in ("live", "paper"):
            token = uuid.uuid4().hex
            self._pending[token] = {"sig": sig.__dict__, "ts": time.time(), "mode": self.mode}
            self._emit({"type": "signal_pending", "data": {
                "confirm_token": token, "amount": round(amount, 2),
                "requires_totp": bool(self.totp_secret), "mode": self.mode}})
            return {"ok": True, "pending_confirmation": True, "confirm_token": token,
                    "amount": round(amount, 2), "requires_totp": bool(self.totp_secret),
                    "mode": self.mode}

        return await self._execute(sig)

    async def _execute(self, sig: Signal) -> dict:
        """确认后/未超阈值时的实际执行（paper 或 live）。"""
        if self.mode == "paper":
            return await self._paper(sig)
        return await self._live(sig)

    async def confirm(self, token: str, totp_code: str = "") -> dict:
        """二次确认：校验 TOTP（若启用）后执行挂起的下单。"""
        from gateway.totp import verify_totp
        entry = self._pending.pop(token, None)
        if not entry:
            return {"ok": False, "reason": "确认令牌无效或已过期"}
        if self.totp_secret and not verify_totp(self.totp_secret, totp_code, self.totp_digits):
            return {"ok": False, "reason": "TOTP 校验失败，请重新发起信号"}
        sig = Signal(**entry["sig"])
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
