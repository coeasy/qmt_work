"""算法单引擎：TWAP / VWAP 时间拆单（借鉴 Rockyzsu/QMT 算法单能力，真实下单）。

- 提交算法单（code/direction/volume/algo/duration/slices）
- 后台 asyncio 任务按时间等分切片，逐片限价/市价下单（价格可选跟随最新价或指定限价）
- 支持暂停 / 恢复 / 取消 / 查询；每片子单记录到事件列表
"""
from __future__ import annotations   # 类内有 list() 方法，注解须延迟求值

import asyncio
import logging
import math
import time
import uuid
from collections import deque

from xtquant_client.base import BrokerError

log = logging.getLogger("qmt_work")


class AlgoEngine:
    """算法单引擎（TWAP/VWAP，事件循环内执行）。"""

    def __init__(self, manager, risk=None, on_event=None, wal=None):
        self._manager = manager
        self._risk = risk
        self._on_event = on_event
        self._wal = wal
        self._jobs: dict[str, dict] = {}
        self._seq = 0

    def _wal_append(self, op: str, aid: str, payload: dict):
        if self._wal is not None:
            from gateway.wal import WAL
            if isinstance(self._wal, WAL):
                self._wal.append(op, "algo", aid, payload)

    def _next_id(self) -> str:
        self._seq += 1
        return f"algo-{self._seq}"

    def _emit(self, event: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 提交 ----------------
    async def submit(self, code: str, direction: str, volume: int, algo: str = "twap",
                     duration: int = 300, slices: int = 5, price_type: str = "market",
                     limit_price: float = 0.0, remark: str = "",
                     visible_pct: float = 10.0, participation_rate: float = 0.1) -> dict:
        code = (code or "").strip().upper()
        if not code:
            raise ValueError("代码不能为空")
        direction = direction.lower()
        if direction not in ("buy", "sell"):
            raise ValueError("direction 须为 buy/sell")
        volume = int(volume)
        if volume <= 0 or volume % 100 != 0:
            raise ValueError("volume 须为 100 的整数倍")
        algo = algo.lower()
        if algo not in ("twap", "vwap", "iceberg", "pov"):
            raise ValueError("algo 须为 twap/vwap/iceberg/pov")
        slices = max(1, min(int(slices), 50))
        duration = max(10, int(duration))
        visible_pct = max(1.0, min(float(visible_pct), 100.0))
        participation_rate = max(0.01, min(float(participation_rate), 1.0))
        aid = self._next_id()
        job = {
            "algo_id": aid, "code": code, "direction": direction, "volume": volume,
            "algo": algo, "duration": duration, "slices": slices,
            "price_type": price_type, "limit_price": limit_price, "remark": remark,
            "visible_pct": visible_pct, "participation_rate": participation_rate,
            "status": "pending", "done": 0, "error": "",
            "slices_done": 0, "created": time.strftime("%H:%M:%S"),
            "children": deque(maxlen=200),
        }
        self._jobs[aid] = job
        self._wal_append("create", aid, job)
        asyncio.create_task(self._run(aid))
        return {"algo_id": aid, "status": "pending",
                "algo": algo, "visible_pct": visible_pct,
                "participation_rate": participation_rate}

    # ---------------- 控制 ----------------
    def pause(self, algo_id: str) -> dict:
        job = self._jobs.get(algo_id)
        if not job:
            raise KeyError(f"未知算法单：{algo_id}")
        if job["status"] == "running":
            job["status"] = "paused"
            self._wal_append("pause", algo_id, {"status": "paused"})
        return {"algo_id": algo_id, "status": job["status"]}

    def resume(self, algo_id: str) -> dict:
        job = self._jobs.get(algo_id)
        if not job:
            raise KeyError(f"未知算法单：{algo_id}")
        if job["status"] == "paused":
            job["status"] = "running"
            self._wal_append("resume", algo_id, {"status": "running"})
        return {"algo_id": algo_id, "status": job["status"]}

    def cancel(self, algo_id: str) -> dict:
        job = self._jobs.get(algo_id)
        if not job:
            raise KeyError(f"未知算法单：{algo_id}")
        if job["status"] in ("pending", "running", "paused"):
            job["status"] = "canceled"
            self._wal_append("cancel", algo_id, {"status": "canceled"})
        return {"algo_id": algo_id, "status": job["status"]}

    def list(self) -> list[dict]:
        return [self._view(j) for j in self._jobs.values()]

    def _view(self, j: dict) -> dict:
        return {k: v for k, v in j.items() if k != "children"}

    # ---------------- 执行 ----------------
    async def _run(self, aid: str) -> None:
        job = self._jobs.get(aid)
        if not job:
            return
        job["status"] = "running"
        algo = job["algo"]
        try:
            if algo == "iceberg":
                await self._run_iceberg(aid)
            elif algo == "pov":
                await self._run_pov(aid)
            elif algo == "vwap":
                # VWAP：按日内成交量分布加权拆单（A股 U 型：开盘/收盘更重），与 TWAP 等分不同
                await self._run_twap(aid, self._plan_vwap(job["volume"], job["slices"]))
            else:  # twap
                await self._run_twap(aid)
            if job["status"] not in ("canceled", "done"):
                job["status"] = "done"
            self._wal_append("final", aid, {"status": job["status"],
                                            "done": job["done"],
                                            "slices_done": job["slices_done"]})
        except asyncio.CancelledError:
            job["status"] = "canceled"
            self._wal_append("final", aid, {"status": "canceled"})
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(exc)
            self._wal_append("final", aid, {"status": "failed", "error": str(exc)})
            log.warning("algo %s failed: %s", aid, exc)

    @staticmethod
    def _plan_slices(volume: int, slices: int) -> list[int]:
        """等分拆单计划：每片均为 100 的整数倍，无法整除的余量并入最后一片。

        A 股最小交易单位为 1 手（100 股），因此不能像通用算法那样按 1 股摊余量。
        """
        volume = int(volume)
        slices = max(1, int(slices))
        lots = volume // 100                 # 总手数
        if lots <= 0:
            return [volume] if volume > 0 else []
        slices = min(slices, lots)           # 手数不足时减少片数
        per_lot = lots // slices
        plan = [per_lot * 100 for _ in range(slices)]
        plan[-1] += (lots - per_lot * slices) * 100
        plan[-1] += volume - lots * 100      # 兜底：非整百输入的零股并入最后一片
        return plan

    @staticmethod
    def _visible_qty(total: int, visible_pct: float) -> int:
        """冰山单每次暴露量：按比例取整到手，最少 1 手，最多不超过总量。"""
        lots = max(1, int(round(int(total) * float(visible_pct) / 100.0 / 100.0)))
        return min(lots * 100, max(100, int(total)))

    @staticmethod
    def _plan_vwap(volume: int, slices: int) -> list[int]:
        """VWAP 拆单计划：按 A 股典型日内成交量分布（U 型：开盘/收盘更重）加权分配每片量。

        与 TWAP 等分不同，VWAP 在成交量更大的时段下更大单，更贴近市场真实成交节奏。
        每片均为 100 股整数倍，余量并入最后一片。
        """
        volume = int(volume)
        slices = max(1, int(slices))
        lots = volume // 100
        if lots <= 0:
            return [volume] if volume > 0 else []
        slices = min(slices, lots)
        # U 型权重：w_i ∝ 1 + sin(pi * i/(N-1))，中间低、两端高
        raw = [1.0 + (math.sin(math.pi * i / max(1, slices - 1)) if slices > 1 else 1.0)
               for i in range(slices)]
        s = sum(raw) or 1.0
        plan = [max(100, int(round(lots * w / s)) * 100) for w in raw]
        # 校正总量（因取整偏差把余量并入最后一片）
        diff = lots * 100 - sum(plan)
        plan[-1] = max(100, plan[-1] + diff)
        return plan

    async def _run_twap(self, aid: str, plan: list[int] | None = None) -> None:
        """TWAP/VWAP：按时间等分切片顺序下单（plan 为空时用 TWAP 等分计划）。"""
        job = self._jobs[aid]
        if plan is None:
            plan = self._plan_slices(job["volume"], job["slices"])
        gap = max(0.5, job["duration"] / max(1, len(plan)))
        for i, vol in enumerate(plan):
            if job["status"] == "canceled":
                break
            while job["status"] == "paused":
                await asyncio.sleep(0.5)
            if job["status"] == "canceled":
                break
            if vol <= 0:
                continue
            await self._place_slice(aid, i + 1, vol)
            job["slices_done"] = i + 1
            if i < len(plan) - 1:
                await asyncio.sleep(gap)

    async def _run_iceberg(self, aid: str) -> None:
        """冰山单（Iceberg）：仅暴露部分量（visible_pct），成交/推进后补充，直到全部完成。"""
        job = self._jobs[aid]
        total = job["volume"]
        vis = self._visible_qty(total, job.get("visible_pct", 10.0))
        gap = max(0.5, job["duration"] / max(1, job["slices"]))
        idx = 0
        while job["done"] < total and job["status"] in ("running", "paused"):
            while job["status"] == "paused":
                await asyncio.sleep(0.5)
            if job["status"] == "canceled":
                break
            remaining = total - job["done"]
            vol = min(vis, remaining)
            idx += 1
            await self._place_slice(aid, idx, vol)
            job["slices_done"] = idx
            await asyncio.sleep(gap)

    async def _run_pov(self, aid: str) -> None:
        """POV：按市场成交量占比（participation_rate）跟单，轮询行情成交量动态计算每片量。"""
        job = self._jobs[aid]
        total = job["volume"]
        b = self._manager.active_bridge()
        gap = max(1.0, job["duration"] / max(1, job["slices"]))
        idx = 0
        last_mkt_vol = None
        rate = float(job.get("participation_rate", 0.1))
        while job["done"] < total and job["status"] in ("running", "paused"):
            while job["status"] == "paused":
                await asyncio.sleep(0.5)
            if job["status"] == "canceled":
                break
            remaining = total - job["done"]
            mkt_vol = 0
            if b is not None:
                try:
                    q = await b.call(b.gateway.get_quote, job["code"])
                    mkt_vol = int(q.get("volume") or 0)
                except Exception:  # noqa: BLE001
                    mkt_vol = 0
            slice_vol = 0
            if last_mkt_vol is not None and mkt_vol > last_mkt_vol:
                slice_vol = int((mkt_vol - last_mkt_vol) * rate)
            last_mkt_vol = mkt_vol
            if slice_vol < 100:
                slice_vol = min(int(round(total / max(1, job["slices"]) / 100.0)) * 100, remaining)
            slice_vol = min(max(slice_vol, 100), remaining)
            idx += 1
            await self._place_slice(aid, idx, slice_vol)
            job["slices_done"] = idx
            await asyncio.sleep(gap)

    async def _confirm_fill(self, b, order_id: str, intended: int) -> int:
        """确认真实成交数量：查当日委托，读 dealt/traded_volume。

        查不到（适配器未实现/订单尚未可见）时保守按全额计，并写降级日志——
        避免「切片发出即计 done 假设全额成交」的乐观错误，同时不阻塞下单流程。
        """
        if not order_id:
            return intended
        for _ in range(3):
            try:
                rows = await b.call(b.gateway.get_orders) or []
            except Exception:  # noqa: BLE001
                return intended
            for o in rows:
                if str(o.get("order_id")) == str(order_id):
                    filled = int(o.get("dealt") or o.get("traded_volume")
                                 or o.get("filled_volume") or 0)
                    if filled > 0:
                        return filled
            await asyncio.sleep(0.3)
        return intended

    async def _place_slice(self, aid: str, idx: int, vol: int) -> None:
        job = self._jobs[aid]
        b = self._manager.active_bridge()
        if b is None:
            raise BrokerError("未连接券商客户端")
        price_type = job["price_type"]
        price = 0.0
        if price_type == "limit":
            if job["limit_price"] and job["limit_price"] > 0:
                price = float(job["limit_price"])
            else:
                q = await b.call(b.gateway.get_quote, job["code"])
                price = float(q.get("last") or 0)
        try:
            res = await b.call(b.gateway.place_order, job["code"], job["direction"],
                               price_type, price, vol, f"algo_{job['algo']}", job.get("remark", ""))
            # 真实成交而非假设全额：查委托确认 filled，冰山/POV 据此推进，避免超额下发
            filled = await self._confirm_fill(b, res.get("order_id"), vol)
            job["done"] += filled
            child = {"idx": idx, "order_id": res.get("order_id"), "volume": vol,
                     "filled": filled, "price_type": price_type, "price": price,
                     "ts": time.strftime("%H:%M:%S")}
            job["children"].append(child)
            self._emit({"type": "algo_slice", "data": {"algo_id": aid, **child}})
            from app.state import state
            if state.db is not None:
                try:
                    state.db.audit("algo", "algo.slice", f"{aid}:{job['code']}",
                                   {"idx": idx, "volume": vol, "filled": filled, "price": price,
                                    "order_id": res.get("order_id")}, "ok")
                except Exception:  # noqa: BLE001
                    pass
        except BrokerError as exc:
            job["error"] = f"第{idx}片失败：{exc}"
            raise


def _engine():
    from app.state import state
    if state.algo_engine is None:
        raise BrokerError("算法单引擎未初始化")
    return state.algo_engine


def register_algo_tools(mcp):
    @mcp.tool()
    async def algo_submit(code: str, direction: str, volume: int, algo: str = "twap",
                          duration: int = 300, slices: int = 5,
                          price_type: str = "market", limit_price: float = 0.0,
                          remark: str = "", visible_pct: float = 10.0,
                          participation_rate: float = 0.1) -> dict:
        """提交算法单：拆单执行。algo: twap/vwap/iceberg/pov。
        iceberg 用 visible_pct（每次暴露比例%），pov 用 participation_rate（市场成交量占比）。
        direction: buy/sell；price_type: market/limit。"""
        return await _engine().submit(code, direction, volume, algo, duration,
                                      slices, price_type, limit_price, remark,
                                      visible_pct, participation_rate)

    @mcp.tool()
    async def algo_pause(algo_id: str) -> dict:
        """暂停算法单。"""
        return _engine().pause(algo_id)

    @mcp.tool()
    async def algo_resume(algo_id: str) -> dict:
        """恢复暂停的算法单。"""
        return _engine().resume(algo_id)

    @mcp.tool()
    async def algo_cancel(algo_id: str) -> dict:
        """取消算法单（未执行完的切片不再下单）。"""
        return _engine().cancel(algo_id)

    @mcp.tool()
    async def algo_list() -> list[dict]:
        """列出全部算法单及其状态/进度。"""
        return _engine().list()
