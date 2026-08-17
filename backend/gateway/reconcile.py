"""A2 委托对账核销（启动恢复 + 定时巡检）。

崩溃/重启后，WAL 里可能残留一批「已提交但状态未知」的委托。本模块负责：
1. 从 WAL 收集所有 op=order / entity=signal|order 的委托记录（未核销的）；
2. 向券商查询当日委托 `get_orders()` 与成交 `get_deals()`；
3. 按 order_id 比对，标记最终状态（filled / part_filled / canceled / rejected / unknown）；
4. 核销结果写回 WAL（op="reconciled"）+ 审计 + WS 事件 + 差异告警。

跨日的委托在券商侧查不到（当日委托表只保留当天），标记为 `stale`，
不再重复对账（已写入核销记录）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from xtquant_client.order_status import (
    normalize_order_status, is_active, FILLED, CANCELLED, REJECTED, UNKNOWN,
)

log = logging.getLogger("qmt_work.reconcile")


class OrderReconciler:
    def __init__(self, manager, wal=None, db=None, on_event=None, notifier=None):
        self._manager = manager
        self._wal = wal
        self._db = db
        self._on_event = on_event
        self._notifier = notifier
        self._task: asyncio.Task | None = None
        self.last_result: dict = {}
        # 阶段 0-A（F11）：「查无此单」不再立即核销为 stale。记录连续未命中轮数，
        # 仅当连续 missing_rounds 轮查不到（且同日）或跨日才标 stale，避免瞬时断连误销活单。
        self.missing_rounds = 3
        self._missing: dict[str, int] = {}

    # ---------------- WAL 侧：待核销集合 ----------------
    def _pending_from_wal(self) -> dict[str, dict]:
        """返回 {order_id: payload}，已核销（op=reconciled）的剔除。"""
        if self._wal is None:
            return {}
        try:
            records = self._wal.all_records()
        except Exception as exc:  # noqa: BLE001
            log.warning("read wal failed: %s", exc)
            return {}
        pending: dict[str, dict] = {}
        done: set[str] = set()
        for rec in records:
            op = rec.get("op", "")
            entity = rec.get("entity", "")
            oid = str(rec.get("entity_id") or "")
            if op == "reconciled":
                done.add(oid)
                continue
            if entity not in ("signal", "order", "condition"):
                continue
            if op not in ("order", "submit", "create"):
                continue
            payload = rec.get("payload", {}) or {}
            oid = oid or str(payload.get("order_id") or "")
            if not oid or oid in ("None", "0", ""):
                continue
            pending[oid] = {**payload, "wal_ts": rec.get("ts", 0)}
        for oid in done:
            pending.pop(oid, None)
        return pending

    # ---------------- 券商侧：当日委托 / 成交 ----------------
    async def _broker_snapshot(self, conn_id: str | None = None) -> tuple[dict, dict]:
        b = self._manager.bridge(conn_id)
        if b is None:
            return {}, {}
        orders: dict[str, dict] = {}
        deals: dict[str, float] = {}
        try:
            rows = await b.call_locked(b.gateway.get_orders)
            for r in rows or []:
                oid = str(r.get("order_id") or r.get("id") or "")
                if oid:
                    orders[oid] = r
        except Exception as exc:  # noqa: BLE001
            log.warning("get_orders failed: %s", exc)
        try:
            rows = await b.call_locked(b.gateway.get_deals)
            for r in rows or []:
                oid = str(r.get("order_id") or r.get("id") or "")
                if oid:
                    deals[oid] = deals.get(oid, 0.0) + float(r.get("volume") or r.get("traded_volume") or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_deals failed: %s", exc)
        return orders, deals

    # ---------------- 主流程 ----------------
    async def reconcile(self, conn_id: str | None = None) -> dict:
        """执行一次对账。返回 {checked, filled, canceled, stale, mismatched, details}。"""
        pending = self._pending_from_wal()
        if not pending:
            self.last_result = {"checked": 0, "note": "无待核销委托"}
            return self.last_result
        orders, deals = await self._broker_snapshot(conn_id)
        # 状态计数键统一为规范词汇（与 xtquant_client.order_status 对齐）
        summary = {"checked": len(pending), "filled": 0, "partial": 0,
                   "cancelled": 0, "rejected": 0, "open": 0, "stale": 0,
                   "unknown": 0, "mismatched": 0, "details": []}
        for oid, payload in pending.items():
            row = orders.get(oid)
            traded = deals.get(oid, 0.0)
            want = float(payload.get("volume") or 0)
            if row is None:
                # 阶段 0-A（F11）：券商当日委托里没有该单，不立即核销。
                # 连续 missing_rounds 轮查不到（同日）→ 视为 stale；跨日（委托产生日≠今日）
                # 才确认已清算标 stale，避免瞬时断连/分页/会话重置误销活单。
                self._missing[oid] = self._missing.get(oid, 0) + 1
                if self._missing[oid] < self.missing_rounds:
                    summary["open"] += 1
                    continue
                # 跨日判定：wal_ts 是 unix 浮点时间戳，payload.ts 可能是 ISO 字符串，
                # 统一换算成 YYYY-MM-DD 再与今日比较（不能直接取字符串前 10 位，那恒不等于今日）。
                created_raw = payload.get("ts") or payload.get("wal_ts") or ""
                cdate = ""
                if isinstance(created_raw, (int, float)) and created_raw > 1e9:
                    try:
                        cdate = datetime.fromtimestamp(float(created_raw)).strftime("%Y-%m-%d")
                    except (ValueError, OSError):
                        cdate = ""
                elif isinstance(created_raw, str) and created_raw[:10]:
                    cdate = created_raw[:10]
                # 跨日委托（券商当日委托表已清空）→ stale；当日委托连续 N 轮查不到 →
                # 按成交判定（有成交=已清算，无成交=疑似消失/废单）。
                if cdate and cdate != datetime.now().strftime("%Y-%m-%d"):
                    status = "stale"
                else:
                    status = "filled" if traded > 0 else "stale"
            else:
                self._missing.pop(oid, None)
                status = normalize_order_status(row.get("status") or row.get("order_status"))
                if is_active(status):
                    # 仍在挂单（pending/partial），不核销，下次继续跟踪
                    summary["open"] += 1
                    continue
                if status == UNKNOWN and traded > 0:
                    status = FILLED if traded >= want > 0 else "part_filled"
            summary[status] = summary.get(status, 0) + 1
            detail = {"order_id": oid, "code": payload.get("code", ""),
                      "side": payload.get("side", ""), "want": want,
                      "traded": traded, "status": status}
            # 数量不符 → 记为差异
            if status == "filled" and want > 0 and traded > 0 and abs(traded - want) > 1e-6:
                detail["mismatch"] = True
                summary["mismatched"] += 1
                # 阶段 3：对账差异可观测——指标 qmt_reconcile_diffs_total{scope="order"}
                try:
                    from gateway.metrics import get_metrics
                    get_metrics().record_reconcile_diff("order")
                except Exception:  # noqa: BLE001
                    pass
            summary["details"].append(detail)
            self._write_off(oid, detail)
        if summary["details"]:
            self._emit({"type": "reconcile", "data": summary})
        if summary["mismatched"] and self._notifier:
            try:
                await self._notifier.notify(
                    "reconcile.mismatch", "委托对账差异",
                    f"发现 {summary['mismatched']} 笔委托数量与成交不一致，请核查", summary)
            except Exception:  # noqa: BLE001
                pass
        log.info("reconcile done: %s", {k: v for k, v in summary.items() if k != "details"})
        self.last_result = summary
        return summary

    def _write_off(self, order_id: str, detail: dict) -> None:
        if self._wal is not None:
            try:
                self._wal.append("reconciled", "order", order_id,
                                 {**detail, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            except Exception:  # noqa: BLE001
                pass
        if self._db is not None:
            try:
                self._db.audit("reconcile", "order.writeoff", order_id, detail,
                               detail.get("status", ""))
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 定时巡检 ----------------
    async def start(self, interval: float = 300.0) -> None:
        if self._task and not self._task.done():
            return

        async def _loop():
            while True:
                try:
                    iv = interval
                    try:
                        from app.state import state
                        if state.runtime_config is not None:
                            iv = state.runtime_config.reconcile_interval
                    except Exception:  # noqa: BLE001
                        pass
                    # 非交易时段放大对账巡检间隔
                    from gateway.trading_session import default_session
                    await asyncio.sleep(default_session.sleep_seconds(iv, 600.0))
                    await self.reconcile()
                except asyncio.CancelledError:
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("reconcile loop error: %s", exc)

        self._task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
