"""统一信号入口 + 物理旁路（paper / dry_run 模式）。

所有交易信号（策略、算法单、条件单、涨停打板、目标持仓同步）统一经 SignalRouter，
根据 mode 决定路由：
- live    真实下单（经风控 + WAL + 通知）
- paper   物理旁路：不真实下单，**统一交 PaperEngine 撮合**（与策略运行共用同一
          模拟盘账户/持仓/现金/成交），另落一行 paper_orders 作为「信号受理日志」
- dry_run 只返回拟执行计划，不下单不记录

P0-6（2026-09-15）：paper 模式此前直写 paper_orders、与策略运行的 PaperEngine
是两套互不相通的账；现统一为 PaperEngine 单实现，paper 单会真实影响
/paper/account 的持仓与现金（且受 PaperEngine 的现金/T+1/涨跌停校验）。

切换模式：POST /config/signal-mode {mode: "live"|"paper"|"dry_run"}
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from gateway.idempotency import single_flight

# P0-1：WAL 写前日志的操作语义常量（单一真源定义在 gateway.wal，此处复用）
from gateway.wal import WAL_OP_INTENT, WAL_OP_INTENT_FAILED, WAL_OP_RESULT  # noqa: E402
from core.clock import now_iso

log = logging.getLogger("qmt_work.signal")

# P0-3：自动生成幂等键的引擎来源白名单（按 source 首段匹配）。
# 用白名单而非黑名单：未知来源一律不去重，宁可漏去重也绝不误吞正常下单。
_ENGINE_SOURCES = frozenset({
    "algo", "condition", "limitup", "rebalance", "strategy", "strategy_runtime",
    "bot", "position", "target", "target_portfolio",
})


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
                 runtime_config=None, paper_engine=None):
        from core.config import settings
        self._manager = manager
        self._risk = risk
        self._db = db
        self._wal = wal
        self._notifier = notifier
        self._on_event = on_event
        self._runtime_config = runtime_config
        # P0-6：模拟盘统一到 PaperEngine（与策略运行共用同一账户）。
        self._paper_engine = paper_engine
        # P0-1：模式持久化 + 安全默认。启动时从 runtime_config 读取；读取失败或无记录
        # 时默认 paper（而非 live），避免升级/崩溃/自动更新重启后无提示恢复实盘。
        self.mode = self._load_persisted_mode("paper")
        self.threshold = getattr(settings, "signal_confirm_threshold", 100_000.0)
        self.totp_secret = getattr(settings, "totp_secret", "")
        self.totp_digits = getattr(settings, "totp_digits", 6)
        self._pending: dict[str, dict] = {}
        # P2-4：待二次确认令牌 TTL（默认 10 分钟），超时自动清理，防内存泄漏与过期确认
        self._pending_ttl = float(getattr(settings, "signal_confirm_ttl", 600.0) or 600.0)
        # P0-3：幂等窗口（秒）。显式键用长窗；自动内容键用短窗（默认 2s），
        # 否则 TWAP/VWAP 各片参数相同的拆单会被吞成一单。0 = 关闭自动生成。
        self._idem_window = float(getattr(settings, "signal_idem_window", 30.0) or 30.0)
        self._auto_idem_window = float(
            getattr(settings, "signal_auto_idem_window", 2.0) or 0.0)

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
                    "updated_at": now_iso()})
            except Exception:  # noqa: BLE001
                pass
        log.info("signal mode: %s -> %s（已持久化）", old, mode)
        return mode

    async def route(self, sig: Signal, auto_confirm: bool = False,
                    idempotency_key: str = "") -> dict:
        """统一信号入口（幂等包装层）：决定幂等键后委托给 `_route_inner`。

        P0-3：引擎路径强制幂等。幂等键优先级：
        1. 调用方显式传入（如算法单的 ``algo:{aid}:{idx}``，含分片序号，最可靠）；
        2. 引擎来源按内容自动生成（source+code+side+vol+price+时间桶，短窗）。

        人工单（manual / webhook / api）**不自动生成**幂等键：人工重复下单是真实
        意图，去重会挡住合理操作；外部系统需要幂等请显式传 idempotency_key。
        """
        key = (idempotency_key or "").strip()
        window = self._idem_window
        if not key:
            key = self._auto_idem_key(sig)
            window = self._auto_idem_window
        if not key:
            return await self._route_inner(sig, auto_confirm)

        async def _run():
            return await self._route_inner(sig, auto_confirm)

        return await single_flight(f"order:{key}", _run, window=window)

    def _auto_idem_key(self, sig: Signal) -> str:
        """引擎来源按内容自动生成幂等键；非引擎来源返回 ""（不去重）。

        时间桶的必要性：TWAP/VWAP 拆单各片的 code/side/vol/price 可能**完全相同**，
        若用长窗口去重会把整轮拆单吞成一单。短窗 + 时间桶保证「间隔超过窗口的两次
        正常下单」各自成立，只合并极短间隔内的重复投递/并发重试。
        """
        src = (sig.source or "").strip().lower()
        base = src.split(":", 1)[0].split("_", 1)[0]  # bot:xxx / algo_twap -> bot / algo
        if base not in _ENGINE_SOURCES:
            return ""
        if self._auto_idem_window <= 0:
            return ""
        bucket = int(time.time() / self._auto_idem_window)
        return (f"auto:{src}:{sig.code}:{sig.side}:{int(sig.volume)}:"
                f"{float(sig.price or 0):.4f}:{bucket}")

    async def _route_inner(self, sig: Signal, auto_confirm: bool = False) -> dict:
        """统一信号入口：根据 mode 决定真实下单 / 旁路 / 预演 / 二次确认。

        auto_confirm=True 时跳过「大额 TOTP 二次确认挂起」（用于已授权自动化引擎：
        algo/limitup/rebalance/strategy_runtime/condition/position），但仍走完整风控。
        """
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
                    "would_execute": True, "ts": now_iso()}
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
                                                price_type=sig.price_type,
                                                # P0-5：仅实盘要求账户快照就绪 +
                                                # 可用资金校验；模拟盘无券商账户。
                                                require_account=(self.mode == "live"))
            if not ok:
                self._audit("signal.rejected", code, sig.__dict__, reason)
                # V9 §5.3：风控拒绝事件进 WS 实时流（前端事件日志可见）
                self._emit({"type": "risk.blocked", "data": {
                    "code": code, "side": side, "volume": sig.volume,
                    "reason": reason, "source": sig.source, "mode": self.mode}})
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

        return await self._execute(sig, est_price)

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

        P0-3：幂等统一在 `route()` 内处理（此处只做透传），避免 submit + route
        双层 single_flight 造成语义混乱；未传 key 的引擎来源由 route 自动生成。
        """
        sig = Signal(source=source, code=str(code), side=str(side),
                     volume=int(volume), price=float(price or 0),
                     price_type=price_type, remark=remark or "", broker_id=broker_id or "",
                     payload=payload or {})
        return await self.route(sig, auto_confirm=auto_confirm,
                                idempotency_key=idempotency_key)

    async def _execute(self, sig: Signal, est_price: float = 0.0) -> dict:
        """确认后/未超阈值时的实际执行（paper 或 live）。

        est_price：路由层已算好的估价（限价=委托价；市价=最新价）。P0-6 起 paper
        分支需要它——PaperEngine 拒绝 price<=0 的委托，市价单必须用估价成交。
        """
        if self.mode == "paper":
            return await self._paper(sig, est_price)
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
            ok, reason = self._risk.check_order(sig.code, est_price, sig.volume, sig.side,
                                                require_account=(self.mode == "live"))
            if not ok:
                self._audit("signal.rejected", sig.code, sig.__dict__, reason)
                return {"ok": False, "reason": reason, "mode": self.mode}
        res = await self._execute(sig, est_price)
        res["confirmed"] = True
        return res

    def pending_count(self) -> int:
        return len(self._pending)

    async def _paper(self, sig: Signal, est_price: float = 0.0) -> dict:
        """模拟盘旁路（P0-6）：统一交 PaperEngine 撮合，与策略运行共用同一账户。

        统一前：此处直写 paper_orders，既不影响 /paper/account 的持仓/现金，也不受
        PaperEngine 的现金/T+1/涨跌停校验——同一「模拟盘」实际是两套互不相通的账。
        统一后：成交/持仓/现金由 PaperEngine 单一实现维护；paper_orders 仅保留为
        「信号受理日志」（记录 source/remark 等 PaperEngine 不承载的信号元数据），
        且**仅在撮合成功后**落行，绝不把被拒单记成已受理。
        """
        pe = self._paper_engine
        if pe is None:
            # 引擎未注入：绝不静默假装成交（零 mock），明确失败并审计。
            reason = "模拟盘引擎未初始化（paper 模式不可用）"
            self._audit("signal.rejected", sig.code, sig.__dict__, reason)
            return {"ok": False, "reason": reason, "mode": "paper"}
        # 限价单用委托价；市价单用路由层估好的最新价（PaperEngine 拒绝 price<=0）。
        price = float(sig.price or 0) or float(est_price or 0)
        try:
            # submit_order 内含同步 SQLite 写（现金/持仓/成交落库），移出事件循环，
            # 避免高频信号下单时阻塞其他请求（与 strategy_runtime 同口径）。
            fill = await asyncio.to_thread(
                pe.submit_order, sig.code, sig.side, price, sig.volume,
                price_type=sig.price_type or "limit", remark=sig.remark or "")
        except ValueError as exc:
            reason = f"模拟盘拒单：{exc}"
            self._audit("signal.rejected", sig.code, sig.__dict__, reason)
            return {"ok": False, "reason": reason, "mode": "paper"}
        # 信号受理日志（PaperEngine 成功后落行；失败路径不落）
        if self._db is not None:
            try:
                self._db.insert("paper_orders", {
                    "source": sig.source, "code": sig.code, "side": sig.side,
                    "price": fill.get("price", price), "volume": sig.volume,
                    "price_type": sig.price_type, "remark": sig.remark,
                    "created_at": now_iso(),
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("paper order persist failed: %s", exc)
        if self._wal is not None:
            from gateway.wal import WAL
            if isinstance(self._wal, WAL):
                self._wal.append("paper", "signal", sig.code, sig.__dict__)
        payload = {**sig.__dict__, "fill": fill}
        self._emit({"type": "signal_paper", "data": payload})
        log.info("paper signal: %s %s %s@%s -> %s", sig.source, sig.code, sig.side,
                 sig.volume, fill.get("order_id"))
        from gateway.metrics import get_metrics
        get_metrics().record_order(sig.side, "paper")
        return {"ok": True, "mode": "paper", "recorded": True,
                "signal": sig.__dict__, **fill}

    def _wal_append(self, op: str, entity_id: str, payload: dict) -> None:
        """WAL 写入（无 WAL / 类型不符 / 写失败均静默，绝不阻断下单主链路）。"""
        if self._wal is None:
            return
        from gateway.wal import WAL
        if isinstance(self._wal, WAL):
            try:
                self._wal.append(op, "order", entity_id, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("wal append failed: op=%s id=%s err=%s", op, entity_id, exc)

    def _wal_intent(self, intent_id: str, sig: Signal) -> None:
        """P0-1：下单**前**写 intent。

        崩溃窗口（intent 已落盘、柜台结果未知）内的委托，重启后由
        ``WAL.unresolved_intents()`` 发现，对账不再「查无此单」。
        """
        self._wal_append(WAL_OP_INTENT, intent_id, {
            "intent_id": intent_id,
            "source": sig.source, "code": sig.code, "side": sig.side,
            "price": sig.price, "volume": sig.volume,
            "price_type": sig.price_type, "broker_id": sig.broker_id,
            "mode": self.mode, "remark": sig.remark,
            "ts": now_iso(),
        })

    async def _live(self, sig: Signal) -> dict:
        b = self._manager.bridge(sig.broker_id or None)
        if b is None:
            # broker_unavailable：路由据此返回 503 + 「券商连接」引导（而非 400），
            # 语义不再把「没有可用券商」误报成「请求非法」。
            return {"ok": False, "reason": "未连接券商客户端", "mode": "live",
                    "broker_unavailable": True, "error_type": "BrokerNotConnectedError"}
        # P0-1：intent 必须在下单**之前**落盘（顺序不可交换）。
        intent_id = uuid.uuid4().hex
        self._wal_intent(intent_id, sig)
        try:
            from gateway.execution import ExecutionService
            res = await ExecutionService(risk=self._risk, db=self._db).place_order(
                b, sig.code, sig.side, sig.volume, sig.price, sig.price_type,
                sig.source, sig.remark, risk=self._risk,
                # P0-2：route() 已完成风控与计数，此处不再二次校验——
                # 否则同一笔委托会消耗两倍频率窗口与日额度。
                risk_checked=True,
                audit_action="signal.live")
            # 阶段 0-B（F13）：绝不把失败/未确认的下单粉饰成成功。
            # 柜台未返回委托号（超时 unknown）或明确拒单 → ok=False。
            oid = res.get("order_id") if isinstance(res, dict) else None
            rstatus = res.get("status") if isinstance(res, dict) else None
            if not oid or rstatus == "rejected":
                res = res if isinstance(res, dict) else {}
                res["ok"] = False
                res["reason"] = res.get("reason") or "下单未确认（柜台未返回委托号，可能超时或拒单）"
                res["mode"] = "live"
                res["intent_id"] = intent_id
                self._wal_append(WAL_OP_INTENT_FAILED, intent_id,
                                 {"intent_id": intent_id, "reason": res["reason"],
                                  "code": sig.code, "side": sig.side,
                                  "volume": sig.volume, "price": sig.price})
                self._audit("signal.failed", sig.code, sig.__dict__, res["reason"])
                return res
            res["ok"] = True
            res["mode"] = "live"
            res["intent_id"] = intent_id
            from gateway.metrics import get_metrics
            get_metrics().record_order(sig.side, "submitted")
            # P0-1：结果记录携带 intent_id，与 intent 配成对，供对账判定「已完成」。
            self._wal_append(WAL_OP_RESULT, str(res.get("order_id", "")),
                             {"intent_id": intent_id,
                              "source": sig.source, "code": sig.code, "side": sig.side,
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
            # P0-1：异常路径同样要终结 intent，否则对账会误报为「悬而未决」。
            self._wal_append(WAL_OP_INTENT_FAILED, intent_id,
                             {"intent_id": intent_id, "reason": str(exc),
                              "code": sig.code, "side": sig.side,
                              "volume": sig.volume, "price": sig.price,
                              "error_type": type(exc).__name__})
            from gateway.metrics import get_metrics
            get_metrics().record_order(sig.side, "error")
            if self._notifier:
                await self._notifier.notify("order.error", "委托失败",
                                            f"{sig.code} {sig.side} 失败：{exc}", sig.__dict__)
            self._audit("signal.failed", sig.code, sig.__dict__, str(exc))
            # 券商**不可用**（未连接 / SDK 缺失 / 桥接不可用）→ 路由层转 503 引导；
            # 其余（风控拦截 / 柜台拒单）保持真实的执行拒绝语义。
            from xtquant_client.base import BrokerNotConnectedError, BrokerSDKError
            return {"ok": False, "reason": str(exc), "mode": "live",
                    "error_type": type(exc).__name__,
                    "broker_unavailable": isinstance(
                        exc, (BrokerNotConnectedError, BrokerSDKError))}

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
