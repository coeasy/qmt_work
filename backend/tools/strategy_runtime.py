"""策略运行容器（P0 工业级）：把生成的策略当作实盘/模拟机器人运行。

设计要点：
- 信号逻辑与 strategy_gen 模板一一对应（ma_cross/macd/rsi/limitup），但**在平台进程内**运行，
  不再依赖「生成代码 → 写盘 → 交给 QMT 客户端另起进程」的割裂流程。
- 行情全部走真实券商 K 线（fetch_kline_cached + kline_cache），绝不伪造。
- 下单路径：实盘模式先过 RiskManager.check_order 再经活跃连接的 broker 适配器真实下单；
  模拟模式走 PaperEngine（同样真实行情盯市）。两种模式都不返回任何假数据。
- 生命周期：create → start（异步循环）/ stop / delete，状态持久化到 strategy_runs；
  进程崩溃重启后自动恢复 status='running' 的实例。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger("qmt_work.strategy_runtime")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _now_minutes() -> int:
    """当前时刻的「时:分」整数分钟数（距当日 00:00 的分钟数）。"""
    from tools.ashare import now_minutes
    return now_minutes()


def _parse_minutes(s: str) -> Optional[int]:
    """把 '9:30'/'09:30'/'930' 解析为距当日 00:00 的分钟数；非法返回 None。P1-2。"""
    from tools.ashare import parse_minutes
    return parse_minutes(s)


# ---------------- 信号计算（纯函数，复用 strategy_gen 模板逻辑） ----------------

# ---------------- 信号计算（统一委托 tools.indicators） ----------------
# 历史：2026-09-07 重构 R2，把分散在 strategy_runtime.py + backtest.py 的
# MA/MACD/RSI 实现统一到 tools/indicators.py。下面这些函数保留为薄包装，
# 既不破坏本地 import 路径，也方便旧测试/调用方继续使用。

from .indicators import last_signal_for as _last_signal_for  # noqa: F401
from .indicators import ma_cross_last as _ma_signal  # noqa: F401
from .indicators import macd_last as _macd_signal  # noqa: F401
from .indicators import rsi_last as _rsi_signal  # noqa: F401


def _ema(vals, n: int):
    """deprecated: 委托 tools.indicators.ema（保留为兼容本地 import）"""
    from .indicators import ema as _ema_impl
    return _ema_impl(vals, n)


def _rsi(closes, n: int) -> float:
    """deprecated: 取 tools.indicators.rsi 最后一根；窗口不足时按 50.0 平局值（与原实现对齐）"""
    from .indicators import rsi as _rsi_impl
    arr = _rsi_impl(list(closes), n)
    if len(arr) == 0 or (len(arr) > 0 and (arr[-1] != arr[-1])):  # NaN check
        return 50.0
    return float(arr[-1])


_DDL = (
    """CREATE TABLE IF NOT EXISTS strategy_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        strategy_type TEXT NOT NULL,
        codes_json TEXT DEFAULT '[]',
        params_json TEXT DEFAULT '{}',
        mode TEXT NOT NULL DEFAULT 'paper',
        conn_id TEXT DEFAULT '',
        account_id TEXT DEFAULT '',
        period TEXT DEFAULT '1d',
        interval_seconds REAL DEFAULT 60,
        volume INTEGER DEFAULT 100,
        max_positions INTEGER DEFAULT 1,
        enabled INTEGER DEFAULT 1,
        status TEXT DEFAULT 'stopped',
        last_signal TEXT DEFAULT '',
        last_action TEXT DEFAULT '',
        last_eval_at TEXT DEFAULT '',
        held_volume REAL DEFAULT 0,
        pnl REAL DEFAULT 0,
        error TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS strategy_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        ts TEXT NOT NULL,
        level TEXT DEFAULT 'info',
        signal TEXT DEFAULT '',
        action TEXT DEFAULT '',
        message TEXT DEFAULT ''
    )""",
)


class StrategyRuntime:
    """在平台进程内管理多个策略机器人（异步循环）。"""

    def __init__(self, state):
        self.state = state
        self._tasks: Dict[int, asyncio.Task] = {}
        self._bought: Dict[int, set] = {}   # run_id -> 已买入标的（limitup 去重）
        # P0-2：在途委托跟踪。run_id -> code -> {order_id, side, ts}
        # 下单成功后登记；同 code 同向存在在途单则跳过本轮（防状态持续满足重复下单）；
        # 超过 inflight_ttl 未成交则撤单并清除登记，允许下轮重发。
        self._inflight: Dict[int, Dict[str, dict]] = {}
        # 上一轮持仓快照：持仓增加视为在途买单已成交，清除对应在途登记。
        self._prev_held: Dict[int, Dict[str, float]] = {}
        self._ensure_tables()

    # ---------------- 表 ----------------
    def _db(self):
        return self.state.db

    def _ensure_tables(self) -> None:
        # M4 迁移账本收编：strategy_runs/strategy_logs 的权威 DDL 在
        # app/db_migrations.py v10（Database.__init__ 必然执行）。
        # 此处保留幂等兜底执行，防止账本外建库（内存测试库等）缺表。
        db = self._db()
        for sql in _DDL:
            db.execute(sql)

    # ---------------- 参数规范化 ----------------
    @staticmethod
    def _json_list(s) -> List[str]:
        try:
            v = json.loads(s)
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _json_obj(s) -> dict:
        try:
            return json.loads(s) if isinstance(json.loads(s), dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _normalize_params(self, st: str, params: dict, codes: List[str]) -> dict:
        p = dict(params or {})
        if st == "ma_cross":
            return {"fast": int(p.get("fast", 5)), "slow": int(p.get("slow", 20)),
                    "volume": int(p.get("volume", 100)), "kline_period": p.get("period", "1d")}
        if st == "macd":
            return {"fast": int(p.get("fast", 12)), "slow": int(p.get("slow", 26)),
                    "signal": int(p.get("signal", 9)), "volume": int(p.get("volume", 100)),
                    "kline_period": p.get("period", "1d")}
        if st == "rsi":
            return {"period": int(p.get("period", 14)), "buy_at": float(p.get("buy_at", 30)),
                    "sell_at": float(p.get("sell_at", 70)), "volume": int(p.get("volume", 100)),
                    "kline_period": p.get("period", "1d")}
        if st == "limitup":
            return {"limit_pct": float(p.get("limit_pct", 0.1)),
                    "cutoff": str(p.get("cutoff", "10:00")),
                    "volume": int(p.get("buy_volume", p.get("volume", 100)))}
        return p

    # ---------------- CRUD ----------------
    def list_runs(self) -> List[dict]:
        rows = self._db().query("SELECT * FROM strategy_runs ORDER BY id DESC")
        for r in rows:
            self._attach(r)
        return rows

    def get_run(self, run_id: int) -> Optional[dict]:
        r = self._db().query_one("SELECT * FROM strategy_runs WHERE id=?", (run_id,))
        if r:
            self._attach(r)
        return r

    def _attach(self, r: dict) -> dict:
        r["codes"] = self._json_list(r.get("codes_json") or "[]")
        r["params"] = self._json_obj(r.get("params_json") or "{}")
        task = self._tasks.get(r["id"])
        r["running"] = task is not None and not task.done()
        return r

    def create(self, body: dict) -> dict:
        st = (body.get("strategy_type") or "").strip().lower()
        codes = body.get("codes") or []
        if isinstance(codes, str):
            codes = [c.strip() for c in codes.replace("，", ",").split(",") if c.strip()]
        if not codes:
            code = (body.get("code") or "").strip()
            if code:
                codes = [code]
        if not st:
            raise ValueError("strategy_type 必填")
        if st not in ("ma_cross", "macd", "rsi", "limitup"):
            raise ValueError(f"未知策略类型：{st}")
        if not codes:
            raise ValueError("codes/code 必填")
        norm = self._normalize_params(st, body.get("params") or {}, codes)
        row = {
            "name": body.get("name") or f"{st}-{codes[0]}",
            "strategy_type": st,
            "codes_json": json.dumps(codes, ensure_ascii=False),
            "params_json": json.dumps(norm, ensure_ascii=False),
            "mode": body.get("mode") or "paper",
            "conn_id": body.get("conn_id") or "",
            "account_id": body.get("account_id") or "",
            "period": norm.get("kline_period", "1d") if st != "limitup" else "tick",
            "interval_seconds": float(body.get("interval_seconds") or 60),
            "volume": int(body.get("volume") or norm.get("volume") or 100),
            "max_positions": int(body.get("max_positions") or 1),
            "enabled": 1 if body.get("enabled", True) else 0,
            "status": "stopped",
            "created_at": _now(),
        }
        rid = self._db().insert("strategy_runs", row)
        return self.get_run(rid)

    def start(self, run_id: int) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"未知运行实例：{run_id}")
        if run["id"] in self._tasks and not self._tasks[run["id"]].done():
            return run
        self._bought.setdefault(run_id, set())
        self._db().execute(
            "UPDATE strategy_runs SET status='running', started_at=?, error='' WHERE id=?",
            (_now(), run_id))
        self._tasks[run_id] = asyncio.create_task(self._run_loop(run_id))
        return self.get_run(run_id)

    def stop(self, run_id: int) -> dict:
        task = self._tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._inflight.pop(run_id, None)   # P0-2：停止即清空该 run 在途登记
        self._prev_held.pop(run_id, None)
        self._db().execute(
            "UPDATE strategy_runs SET status='stopped', last_eval_at=? WHERE id=?",
            (_now(), run_id))
        return self.get_run(run_id)

    def delete(self, run_id: int) -> None:
        self.stop(run_id)
        self._db().execute("DELETE FROM strategy_runs WHERE id=?", (run_id,))
        self._db().execute("DELETE FROM strategy_logs WHERE run_id=?", (run_id,))
        self._bought.pop(run_id, None)

    def restore(self) -> int:
        """进程重启后恢复仍在运行状态的实例（enabled=1 才恢复）。"""
        count = 0
        for run in self.list_runs():
            if run.get("status") == "running" and int(run.get("enabled", 1)) == 1:
                try:
                    self.start(run["id"])
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("restore run %s failed: %s", run["id"], exc)
        return count

    def logs(self, run_id: int, limit: int = 100) -> List[dict]:
        return self._db().query(
            "SELECT * FROM strategy_logs WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit))

    # ---------------- 运行循环 ----------------
    async def _run_loop(self, run_id: int) -> None:
        log.info("strategy run %s started", run_id)
        while True:
            try:
                await self._eval_once(run_id)
            except asyncio.CancelledError:
                log.info("strategy run %s cancelled", run_id)
                raise
            except Exception as exc:  # noqa: BLE001
                self._log(run_id, "error", f"eval error: {exc}", "")
                self._set(run_id, error=str(exc)[:300])
                log.warning("strategy run %s error: %s", run_id, exc)
            run = self.get_run(run_id)
            interval = float((run or {}).get("interval_seconds") or 60)
            await asyncio.sleep(max(1.0, interval))

    async def _eval_once(self, run_id: int) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        st = run["strategy_type"]
        params = run["params"]
        codes = run["codes"]
        mode = run["mode"]
        conn_id = run.get("conn_id") or None
        bridge = self.state.broker_manager.bridge(conn_id)
        if bridge is None and mode == "live":
            self._log(run_id, "warn", "实盘模式但未连接券商，等待连接…", "")
            return

        if st == "limitup":
            await self._eval_limitup(run, bridge, codes, params)
            return

        # P1-3：非 limitup 策略遍历全部标的，每只独立算信号/查持仓/下单；
        # 单只异常不影响其余标的。
        kp = params.get("kline_period", "1d")
        count = max(int(params.get("slow", 26)) + int(params.get("signal", 9)) + 10, 80)
        for code in codes:
            try:
                await self._eval_code(run, bridge, code, st, params, kp, count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log(run_id, "error", f"{code} 评估失败：{exc}", "")

    async def _eval_code(self, run, bridge, code, st, params, kp, count) -> None:
        """对单只标的完成：取 K 线 → 算信号 → 查持仓 → 下单。P1-3 拆分复用。"""
        run_id = run["id"]
        bars = await self._fetch_kline(bridge, code, kp, count)
        if not bars:
            self._log(run_id, "warn", f"无 {code} 的 {kp} K 线数据（未连接券商或无历史）", "")
            return
        closes = np.array([float(b["close"]) for b in bars], dtype=float)
        if st == "ma_cross":
            signal, _ = _ma_signal(closes, int(params["fast"]), int(params["slow"]))
        elif st == "macd":
            signal, _ = _macd_signal(closes, int(params["fast"]), int(params["slow"]),
                                     int(params["signal"]))
        elif st == "rsi":
            signal, _ = _rsi_signal(closes, int(params["period"]),
                                    float(params["buy_at"]), float(params["sell_at"]))
        else:
            self._log(run_id, "warn", f"未知策略类型 {st}", "")
            return

        held = await self._held_volume(run, code, bridge)
        # P0-2：持仓增加 → 在途买入已成交，清除对应在途登记，允许后续方向信号重新下单。
        prev = self._prev_held.get(run_id, {}).get(code, 0.0)
        if held > prev:
            self._inflight.get(run_id, {}).pop(code, None)
        self._prev_held.setdefault(run_id, {})[code] = held
        # 注意：strategy_runs 表无 code 列（多标的状态存 codes_json），
        # 不能向 _set 传 code=…（否则 sqlite 报 no such column 且被外层吞掉，下单不执行）。
        self._set(run_id, held_volume=held, last_eval_at=_now(), last_signal=signal)

        if signal == "buy" and held <= 0:
            price = await self._latest_price(bridge, code, None)
            if price and price > 0:
                await self._maybe_order(run, code, "buy", price, int(params.get("volume", 100)))
        elif signal == "sell" and held > 0:
            price = await self._latest_price(bridge, code, None)
            if price and price > 0:
                await self._maybe_order(run, code, "sell", price, int(held))

    async def _eval_limitup(self, run, bridge, codes, params) -> None:
        run_id = run["id"]
        if bridge is None:
            self._log(run_id, "warn", "未连接券商，无法获取逐笔行情", "")
            return
        # P1-2：cutoff 解析为「时:分」整数分钟数比较（兼容 9:30/09:30），
        # 不再做字符串比较（9:30 无前导零时恒不执行）。
        now_min = _now_minutes()
        cutoff_min = _parse_minutes(str(params.get("cutoff", "10:00")))
        if cutoff_min is None:
            self._log(run_id, "warn", f"cutoff {params.get('cutoff')} 格式非法，忽略截止时间", "")
        elif now_min > cutoff_min:
            return
        try:
            ticks = await bridge.call(bridge.gateway.get_full_tick, codes)
        except Exception as exc:  # noqa: BLE001
            self._log(run_id, "warn", f"获取逐笔失败：{exc}", "")
            return
        ticks = ticks or {}
        for code in codes:
            if code in self._bought.get(run_id, set()):
                continue
            t = ticks.get(code)
            if not isinstance(t, dict):
                continue
            last = t.get("lastPrice") or t.get("last") or t.get("price")
            lc = t.get("lastClose") or t.get("preClose")
            if not last or not lc:
                continue
            try:
                last = float(last); lc = float(lc)
            except (TypeError, ValueError):
                continue
            # P1-2：复用统一涨停判定 tools.limitup._limit_factor（主板10/创业科创20/北交30/ST5），
            # 不再用固定 limit_pct=0.1——否则 20cm/30cm 标的会被误判为未涨停。
            from tools.limitup import _limit_factor
            factor = _limit_factor(code)
            limit_price = round(lc * (1 + factor), 2)
            if last >= limit_price:
                vol = int(params.get("volume", 100))
                await self._maybe_order(run, code, "buy", last, vol)
                self._bought.setdefault(run_id, set()).add(code)

    async def _maybe_order(self, run, code, direction, price, volume) -> None:
        run_id = run["id"]
        mode = run["mode"]
        volume = int(volume)
        if volume <= 0 or not price or price <= 0:
            return
        mode = str(mode).lower()

        # P0-2 前置检查：在途委托跟踪，防"状态持续满足"信号每轮重复下单（超额建仓）。
        inflight = self._inflight.setdefault(run_id, {})
        existing = inflight.get(code)
        if existing is not None and existing.get("side") == direction:
            if existing.get("order_id"):
                ttl = self._ttl_seconds(run)
                if time.time() - float(existing.get("ts") or 0) > ttl:
                    # 在途单超时未成交 → 撤单并清除，允许本轮重新发起
                    await self._cancel_inflight(run, code)
                else:
                    self._log(run_id, "info",
                              f"{code} 存在在途{direction}单 {existing['order_id']}，跳过本轮"
                              f"（防重复下单）", direction)
                    return

        # 阶段 0-B（F8）：mode 不区分大小写（原 "Live"/拼写错误落入 else 既绕过风控又实盘下单）。
        # 全局 signal_mode 是交易主闸门：paper 下任何引擎都不真实下单（安全）。
        try:
            oid = None
            if mode == "paper":
                pe = self.state.paper_engine
                if pe is None:
                    self._log(run_id, "error", "模拟盘引擎未初始化", direction)
                    return
                order = pe.submit_order(code, direction, price, volume,
                                        price_type="limit", remark=f"bot:{run_id}")
                oid = order.get("order_id")
                self._log(run_id, "order",
                          f"[模拟] {direction} {code} {volume}@{price:.2f} -> {oid}", direction)
            else:
                # 实盘：经统一下单入口，携带完整风控（熔断/单笔/频率/黑白名单/日额度）。
                # auto_confirm=True：已授权策略单跳过人工 TOTP 挂起，但仍过风控。
                from core.state import state
                res = await state.signal_router.submit(
                    code, direction, volume, price, "limit",
                    source=f"bot:{run_id}", broker_id=run.get("conn_id") or "",
                    auto_confirm=True)
                oid = (res or {}).get("order_id") if isinstance(res, dict) else None
                if res and isinstance(res, dict) and not res.get("ok"):
                    self._log(run_id, "reject",
                              f"{direction} {code} 被风控拦截：{res.get('reason')}", direction)
                    self._set(run_id, last_action=f"reject {code}")
                    return
                self._log(run_id, "order",
                          f"[实盘] {direction} {code} {volume}@{price:.2f} -> {oid}", direction)
            # P0-2：下单成功后登记在途（拿不到 order_id 则登记 0 占位，防同向重复发起）。
            inflight[code] = {"order_id": oid or "", "side": direction, "ts": time.time()}
            self._set(run_id, last_action=f"{direction} {code} {volume}@{price:.2f}")
        except Exception as exc:  # noqa: BLE001
            self._log(run_id, "error", f"{direction} {code} 下单失败：{exc}", direction)

    def _ttl_seconds(self, run) -> float:
        """在途委托 TTL（秒）：strategy.inflight_ttl（倍）× interval_seconds，默认 2×interval。"""
        interval = float((run or {}).get("interval_seconds") or 60)
        mult = 2.0
        rc = getattr(self.state, "runtime_config", None)
        if rc is not None:
            try:
                mult = float(rc.get("strategy.inflight_ttl") or 2.0)
            except (TypeError, ValueError):
                mult = 2.0
        return max(1.0, interval * mult)

    async def _cancel_inflight(self, run, code) -> None:
        """撤单并清除在途登记（超时未成交）。清登记放 finally，保证后续不残留占位。"""
        run_id = run["id"]
        rec = self._inflight.get(run_id, {}).get(code)
        oid = (rec or {}).get("order_id") or ""
        mode = str(run["mode"]).lower()
        try:
            if mode == "paper" or not oid:
                self._log(run_id, "info", f"{code} 在途单{oid or ''}超时撤单（模拟/无ID）", "")
                return
            bridge = self.state.broker_manager.bridge(run.get("conn_id") or None)
            if bridge is None:
                self._log(run_id, "warn", f"{code} 撤单失败：未连接券商", "")
                return
            await bridge.call(bridge.gateway.cancel_order, oid)
            self._log(run_id, "info", f"{code} 在途单 {oid} 超时撤单", "")
        except Exception as exc:  # noqa: BLE001
            self._log(run_id, "warn", f"{code} 撤单失败：{exc}", "")
        finally:
            self._inflight.get(run_id, {}).pop(code, None)

    # ---------------- 行情/持仓辅助 ----------------
    async def _fetch_kline(self, bridge, code, period, count):
        try:
            from tools import fetch_kline_cached
            res = await fetch_kline_cached(code, period, count)
            return (res or {}).get("bars") or []
        except Exception as exc:  # noqa: BLE001
            self._log(-1, "warn", f"kline fetch {code} failed: {exc}", "")
            return []

    async def _latest_price(self, bridge, code, fallback):
        if bridge is None:
            return float(fallback) if fallback else 0.0
        try:
            q = await bridge.call(bridge.gateway.get_quote, code)
            if isinstance(q, dict):
                for k in ("lastPrice", "last", "price", "close"):
                    v = q.get(k)
                    if v:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
        except Exception:  # noqa: BLE001
            pass
        return float(fallback) if fallback else 0.0

    async def _held_volume(self, run, code, bridge) -> float:
        mode = run["mode"]
        try:
            if mode == "paper":
                pe = self.state.paper_engine
                if pe is None:
                    return 0.0
                for p in pe.get_positions():
                    if (p.get("code") or "").upper() == code.upper():
                        return float(p.get("volume") or 0.0)
                return 0.0
            if bridge is None:
                return 0.0
            positions = await bridge.call(bridge.gateway.get_positions) or []
            for p in positions:
                if (p.get("code") or p.get("stock_code") or "").upper() == code.upper():
                    return float(p.get("volume") or p.get("avail") or 0.0)
            return 0.0
        except Exception as exc:  # noqa: BLE001
            self._log(run["id"], "warn", f"查询持仓失败：{exc}", "")
            return 0.0

    # ---------------- DB 辅助 ----------------
    def _set(self, run_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._db().execute(
            f"UPDATE strategy_runs SET {cols} WHERE id=?",
            (*fields.values(), run_id))

    def _log(self, run_id: int, level: str, message: str, signal: str = "") -> None:
        if run_id and run_id > 0:
            self._db().insert("strategy_logs",
                              {"run_id": run_id, "ts": _now(), "level": level,
                               "signal": signal or "", "message": str(message)[:2000]})
        log.info("[strategy_run %s] %s: %s", run_id, level, message)
