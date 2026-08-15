"""回测任务编排：任务队列 + 进度推送 + 可取消。

- 回测基于券商真实历史 K 线（fetch_kline_async），不再使用随机游走假数据
- compare/sensitivity 作为组合任务，进度 = 子任务完成数/总数
- K 线获取依赖主进程内的券商连接，故在事件循环内执行（不在子进程）
"""
import asyncio
import json
import logging
import time
import uuid

from app.db import get_db
from tools.backtest import (fetch_kline_async, run_backtest_engine,
                            run_backtest_vectorized, run_param_sweep)

log = logging.getLogger("qmt_work.backtest")


class BacktestQueue:
    def __init__(self, max_workers: int = 2):
        self._jobs: dict[str, dict] = {}
        self._listeners: list = []

    def on_event(self, handler) -> None:
        self._listeners.append(handler)

    async def _emit(self, job: dict) -> None:
        for h in self._listeners:
            try:
                await h(job)
            except Exception:
                pass

    def _persist(self, job: dict) -> None:
        try:
            db = get_db()
            db.execute(
                "INSERT OR REPLACE INTO backtest_jobs "
                "(id, kind, params_json, status, progress, result_json, error, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (job["id"], job["kind"], json.dumps(job.get("params", {}), ensure_ascii=False),
                 job["status"], job.get("progress", 0),
                 json.dumps(job.get("result", {}), ensure_ascii=False, default=str),
                 job.get("error", ""), job["created_at"], time.strftime("%Y-%m-%dT%H:%M:%S")))
        except Exception as exc:
            log.warning("persist job failed: %s", exc)

    def create(self, kind: str, params: dict) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "params": params,
               "status": "pending", "progress": 0, "result": None, "error": "",
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._jobs[job["id"]] = job
        self._persist(job)
        return job

    def get(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job["status"] not in ("pending", "running"):
            return False
        job["status"] = "cancelled"
        self._persist(job)
        return True

    async def submit(self, kind: str, params: dict):
        job = self.create(kind, params)
        asyncio.create_task(self._dispatch(job))
        return job

    async def _dispatch(self, job: dict) -> None:
        job["status"] = "running"
        self._persist(job)
        await self._emit(job)
        try:
            if job["kind"] == "backtest":
                job["result"] = await self._run_backtest(job["params"])
                job["progress"] = 100
                _record_backtest_metric("backtest")
            elif job["kind"] == "compare":
                job["result"] = await self._run_compare(job["params"])
                job["progress"] = 100
                _record_backtest_metric("compare")
            elif job["kind"] == "sensitivity":
                job["result"] = await self._run_sensitivity(job["params"])
                job["progress"] = 100
                _record_backtest_metric("sensitivity")
            elif job["kind"] == "sweep":
                job["result"] = await self._run_sweep(job["params"])
                job["progress"] = 100
            job["status"] = "done"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        self._persist(job)
        await self._emit(job)

    # ---- 子任务（在事件循环内执行，依赖主进程券商连接）----
    async def _run_backtest(self, params: dict) -> dict:
        symbol = params.get("symbol", "600519.SH")
        strategy = params.get("strategy", "ma_cross")
        pr = params.get("params", {"fast": 5, "slow": 20})
        capital = float(params.get("initial_capital", 100_000))
        count = int(params.get("count", 250))
        broker_id = params.get("broker_id", "")
        cost = {"commission_rate": float(params.get("commission_rate", 0.0003)),
                "stamp_tax": float(params.get("stamp_tax", 0.001)),
                "slippage_bps": float(params.get("slippage_bps", 5.0))}
        kline = await fetch_kline_async(broker_id, symbol, count)
        res = run_backtest_engine(symbol, kline, strategy, pr, capital,
                                  cost["commission_rate"], cost["stamp_tax"], cost["slippage_bps"])
        db = get_db()
        bid = db.insert("backtests", {
            "user_id": 1, "symbol": symbol, "start": params.get("start", ""),
            "end": params.get("end", ""), "strategy": strategy,
            "params_json": json.dumps(pr, ensure_ascii=False),
            "initial_capital": capital,
            "metrics_json": json.dumps(res["metrics"], ensure_ascii=False),
            "trades_json": json.dumps(res["trades"], ensure_ascii=False),
            "report_path": "", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        res["id"] = bid
        return res

    async def _run_compare(self, params: dict) -> dict:
        configs = params.get("configs", [])
        broker_id = params.get("broker_id", "")
        def _cost(cfg):
            return {"commission_rate": float(cfg.get("commission_rate", params.get("commission_rate", 0.0003))),
                    "stamp_tax": float(cfg.get("stamp_tax", params.get("stamp_tax", 0.001))),
                    "slippage_bps": float(cfg.get("slippage_bps", params.get("slippage_bps", 5.0)))}
        rows = []
        total = max(len(configs), 1)
        for i, cfg in enumerate(configs):
            symbol = cfg.get("symbol", "600519.SH")
            kline = await fetch_kline_async(broker_id, symbol, int(cfg.get("count", 250)))
            c = _cost(cfg)
            res = run_backtest_engine(symbol, kline, cfg.get("strategy", "ma_cross"),
                                      cfg.get("params", {"fast": 5, "slow": 20}),
                                      float(cfg.get("initial_capital", 100_000)),
                                      c["commission_rate"], c["stamp_tax"], c["slippage_bps"])
            rows.append({"config": cfg, "metrics": res["metrics"]})
            job = self._jobs.get(params.get("_job_id", ""))
            if job:
                job["progress"] = int((i + 1) / total * 100)
                self._persist(job)
                await self._emit(job)
        return {"rows": sorted(rows, key=lambda r: r["metrics"].get("sharpe", -99), reverse=True)}

    async def _run_sensitivity(self, params: dict) -> dict:
        symbol = params.get("symbol", "600519.SH")
        values = params.get("values", [3, 5, 10, 20, 30])
        param = params.get("param", "fast")
        broker_id = params.get("broker_id", "")
        table = []
        total = max(len(values), 1)
        base = {"fast": 5, "slow": 20}
        for i, v in enumerate(values):
            kline = await fetch_kline_async(broker_id, symbol, 250)
            p = dict(base); p[param] = v
            res = run_backtest_engine(symbol, kline, "ma_cross", p, 100_000.0)
            m = res["metrics"]
            table.append({"param": v, "sharpe": m.get("sharpe"),
                          "max_drawdown": m.get("max_drawdown"),
                          "total_return": m.get("total_return")})
            job = self._jobs.get(params.get("_job_id", ""))
            if job:
                job["progress"] = int((i + 1) / total * 100)
                self._persist(job)
                await self._emit(job)
        return {"symbol": symbol, "param": param, "table": table}

    async def close(self) -> None:
        pass

    async def _run_sweep(self, params: dict) -> dict:
        symbol = params.get("symbol", "600519.SH")
        strategy = params.get("strategy", "ma_cross")
        param_grid = params.get("param_grid", {})
        broker_id = params.get("broker_id", "")
        capital = float(params.get("initial_capital", 100_000))
        if not param_grid:
            return {"error": "param_grid 不能为空"}
        kline = await fetch_kline_async(broker_id, symbol, int(params.get("count", 500)))
        res = run_param_sweep(symbol, kline, strategy, param_grid, capital,
                              float(params.get("commission_rate", 0.0003)),
                              float(params.get("stamp_tax", 0.001)),
                              float(params.get("slippage_bps", 5.0)))
        try:
            get_db().execute(
                "INSERT INTO backtests (user_id, symbol, start, end, strategy, "
                "params_json, initial_capital, metrics_json, trades_json, "
                "report_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (1, symbol, params.get("start", ""), params.get("end", ""),
                 f"{strategy}:sweep",
                 json.dumps({"param_grid": param_grid}, ensure_ascii=False),
                 capital, json.dumps(res.get("best", {}), ensure_ascii=False, default=str),
                 json.dumps(res.get("grid", []), ensure_ascii=False, default=str),
                 "", time.strftime("%Y-%m-%dT%H:%M:%S")))
        except Exception as exc:  # noqa: BLE001
            log.warning("persist sweep failed: %s", exc)
        _record_backtest_metric("sweep")
        return res


def _record_backtest_metric(status: str) -> None:
    """可观测性：回测任务计数（指标收集器缺失时安全跳过）。"""
    try:
        from app.state import state
        m = getattr(state, "metrics", None)
        if m is not None and hasattr(m, "record_backtest"):
            m.record_backtest(status)
    except Exception:  # noqa: BLE001
        pass
