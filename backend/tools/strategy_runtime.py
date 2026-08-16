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
from typing import Any, Dict, List, Optional

import numpy as np

log = logging.getLogger("qmt_work.strategy_runtime")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 信号计算（纯函数，复用 strategy_gen 模板逻辑） ----------------

def _ma_signal(closes, fast: int, slow: int):
    if len(closes) < slow + 1:
        return "hold", None
    fast_ma = float(np.mean(closes[-fast:]))
    slow_ma = float(np.mean(closes[-slow:]))
    prev_fast = float(np.mean(closes[-fast - 1:-1]))
    prev_slow = float(np.mean(closes[-slow - 1:-1]))
    if prev_fast <= prev_slow and fast_ma > slow_ma:
        return "buy", fast_ma
    if prev_fast >= prev_slow and fast_ma < slow_ma:
        return "sell", fast_ma
    return "hold", fast_ma


def _ema(vals, n: int):
    vals = np.asarray(vals, dtype=float)
    if len(vals) == 0:
        return vals
    w = 2.0 / (n + 1)
    out = np.empty_like(vals)
    out[0] = vals[0]
    for i in range(1, len(vals)):
        out[i] = vals[i] * w + out[i - 1] * (1 - w)
    return out


def _macd_signal(closes, fast: int, slow: int, signal: int):
    need = slow + signal + 2
    if len(closes) < need:
        return "hold", None
    dif = _ema(closes, fast) - _ema(closes, slow)
    dea = _ema(dif, signal)
    cross_up = dea[-2] <= dif[-2] and dea[-1] > dif[-1]
    cross_dn = dea[-2] >= dif[-2] and dea[-1] < dif[-1]
    if cross_up:
        return "buy", float(dif[-1])
    if cross_dn:
        return "sell", float(dif[-1])
    return "hold", float(dif[-1])


def _rsi(closes, n: int) -> float:
    if len(closes) < n + 2:
        return 50.0
    diffs = np.diff(closes[-n - 1:])
    gain = float(np.mean(np.clip(diffs, 0, None)))
    loss = float(-np.mean(np.clip(diffs, None, 0)))
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def _rsi_signal(closes, period: int, buy_at: float, sell_at: float):
    r = _rsi(closes, period)
    if r < buy_at:
        return "buy", r
    if r > sell_at:
        return "sell", r
    return "hold", r


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
        self._ensure_tables()

    # ---------------- 表 ----------------
    def _db(self):
        return self.state.db

    def _ensure_tables(self) -> None:
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

        code = codes[0]
        kp = params.get("kline_period", "1d")
        count = max(int(params.get("slow", 26)) + int(params.get("signal", 9)) + 10, 80)
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
        cutoff = str(params.get("cutoff", "10:00"))
        now = time.strftime("%H:%M")
        if now > cutoff:
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
            limit_pct = float(params.get("limit_pct", 0.1))
            if last >= round(lc * (1 + limit_pct), 2):
                vol = int(params.get("volume", 100))
                await self._maybe_order(run, code, "buy", last, vol)
                self._bought.setdefault(run_id, set()).add(code)

    async def _maybe_order(self, run, code, direction, price, volume) -> None:
        run_id = run["id"]
        mode = run["mode"]
        volume = int(volume)
        if volume <= 0 or not price or price <= 0:
            return
        # 实盘模式过真实风控（计入日级计数）；模拟盘不污染真实风控计数
        if mode == "live":
            okc, reason = self.state.risk.check_order(code, price, volume, direction)
            if not okc:
                self._log(run_id, "reject", f"{direction} {code} 被风控拦截：{reason}", direction)
                self._set(run_id, last_action=f"reject {code}")
                return
        try:
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
                bridge = self.state.broker_manager.bridge(run.get("conn_id") or None)
                if bridge is None:
                    self._log(run_id, "error", "未连接券商，无法实盘下单", direction)
                    return
                res = await bridge.call(bridge.gateway.place_order, code, direction,
                                        "limit", price, volume, f"bot:{run_id}", "")
                oid = (res or {}).get("order_id") if isinstance(res, dict) else None
                self._log(run_id, "order",
                          f"[实盘] {direction} {code} {volume}@{price:.2f} -> {oid}", direction)
            self._set(run_id, last_action=f"{direction} {code} {volume}@{price:.2f}")
        except Exception as exc:  # noqa: BLE001
            self._log(run_id, "error", f"{direction} {code} 下单失败：{exc}", direction)

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
