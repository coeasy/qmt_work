"""回测任务编排：任务队列 + 进度推送 + 可取消。

- 回测基于券商真实历史 K 线（fetch_kline_async），不再使用随机游走假数据
- compare/sensitivity 作为组合任务，进度 = 子任务完成数/总数
- K 线获取依赖主进程内的券商连接，故在事件循环内执行（不在子进程）
"""
import asyncio
import json
import logging
import uuid

from core.db import get_db
from tools.backtest import fetch_kline_async, fetch_kline_async_meta, run_backtest_engine, run_param_sweep
from core.clock import now_iso

log = logging.getLogger("qmt_work.backtest")


#: 内存中保留的**已终态**作业上限。
#:
#: 为什么需要：``self._jobs`` 原实现只增不减 —— ``create()`` 每次插入一条，
#: 终态后也永不移除；而 ``job["result"]`` 里带着完整回测明细（``trades``/``grid``
#: 动辄数千条），于是「批量扫参 + 长时间挂着进程」会让内存单调上涨。
#: 运行中的作业必须留在内存（``cancel()``、进度更新都依赖它），故只淘汰终态。
_MAX_JOBS = 200
#: ``backtest_jobs`` 表保留的历史行上限（按 created_at 倒序保留）。
#: 表本身同样是只增不删：``metrics_json`` / ``trades_json`` 每行都不小。
_MAX_PERSISTED_JOBS = 500


class BacktestQueue:
    def __init__(self, max_workers: int = 2):
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._listeners: list = []
        self._max_workers = max(1, int(max_workers))
        self._sem = asyncio.Semaphore(self._max_workers)
        self._finished = 0            # 已终态作业计数（用于节流持久账本裁剪）

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
                 job.get("error", ""), job["created_at"], now_iso()))
        except Exception as exc:
            log.warning("persist job failed: %s", exc)

    def create(self, kind: str, params: dict) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "params": params,
               "status": "pending", "progress": 0, "result": None, "error": "",
               "created_at": now_iso()}
        self._jobs[job["id"]] = job
        self._persist(job)
        return job

    def get(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    # ---- 保留策略（防止内存 / 表无界增长）----
    def _prune_jobs(self) -> None:
        """把内存里的作业裁剪到 ``_MAX_JOBS`` 以内（按插入序淘汰最旧的**终态**作业）。

        只淘汰 ``done``/``failed``/``cancelled`` 且已不在 ``_tasks`` 中的作业；
        ``pending``/``running`` 一律保留 —— ``cancel()`` 与进度更新都以内存对象为准，
        淘汰运行中的作业会让取消与进度静默失效。
        被淘汰的作业仍留在 ``backtest_jobs`` 表里，``GET /backtest/jobs`` 会回落到 DB。
        """
        if len(self._jobs) <= _MAX_JOBS:
            return
        for jid in list(self._jobs.keys()):
            if len(self._jobs) <= _MAX_JOBS:
                break
            job = self._jobs.get(jid)
            if job is None or job["status"] in ("pending", "running"):
                continue
            if jid in self._tasks:
                continue
            self._jobs.pop(jid, None)

    def _prune_persisted(self) -> None:
        """裁剪 ``backtest_jobs`` 表最旧的历史行（只增不删的持久账本）。

        ``GET /backtest/jobs`` 读表时已 LIMIT 50，因此这里是纯粹的容量保护：
        保留最近 ``_MAX_PERSISTED_JOBS`` 行，其余删除。失败只记日志 ——
        清理是运维动作，绝不能让它影响回测本身。
        """
        try:
            get_db().execute(
                "DELETE FROM backtest_jobs WHERE id NOT IN "
                "(SELECT id FROM backtest_jobs ORDER BY created_at DESC, id DESC LIMIT ?)",
                (_MAX_PERSISTED_JOBS,))
        except Exception as exc:  # noqa: BLE001
            log.debug("backtest_jobs 裁剪失败（已忽略）：%s", exc)

    def _retention_tick(self) -> None:
        """每个作业进入终态后调用一次：裁剪内存 + （节流）裁剪持久账本。"""
        self._prune_jobs()
        self._finished += 1
        if self._finished % 50 == 0:
            self._prune_persisted()

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job["status"] not in ("pending", "running"):
            return False
        job["status"] = "cancelled"
        self._persist(job)
        # 真正停止仍在运行的协程（在 await 点可中断，如 K 线拉取）
        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
        return True

    async def submit(self, kind: str, params: dict):
        job = self.create(kind, params)
        self._tasks[job["id"]] = asyncio.create_task(self._dispatch(job))
        return job

    async def _dispatch(self, job: dict) -> None:
        job["status"] = "running"
        self._persist(job)
        await self._emit(job)
        async with self._sem:
            try:
                if job["kind"] == "backtest":
                    job["result"] = await self._run_backtest(job["params"])
                    job["progress"] = 100
                    _record_backtest_metric("backtest")
                elif job["kind"] == "compare":
                    job["result"] = await self._run_compare(job["params"], job)
                    job["progress"] = 100
                    _record_backtest_metric("compare")
                elif job["kind"] == "sensitivity":
                    job["result"] = await self._run_sensitivity(job["params"], job)
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
        self._tasks.pop(job.get("id", ""), None)
        self._persist(job)
        await self._emit(job)
        self._retention_tick()

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
        kline, meta = await fetch_kline_async_meta(broker_id, symbol, count)
        res = run_backtest_engine(symbol, kline, strategy, pr, capital,
                                  cost["commission_rate"], cost["stamp_tax"], cost["slippage_bps"],
                                  data_meta=meta)
        db = get_db()
        bid = db.insert("backtests", {
            "user_id": 1, "symbol": symbol, "start": params.get("start", ""),
            "end": params.get("end", ""), "strategy": strategy,
            "params_json": json.dumps(pr, ensure_ascii=False),
            "initial_capital": capital,
            "metrics_json": json.dumps(res["metrics"], ensure_ascii=False),
            "trades_json": json.dumps(res["trades"], ensure_ascii=False),
            "report_path": "", "created_at": now_iso(),
        })
        res["id"] = bid
        return res

    async def _run_compare(self, params: dict, job: dict | None = None) -> dict:
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
            kline, meta = await fetch_kline_async_meta(broker_id, symbol, int(cfg.get("count", 250)))
            c = _cost(cfg)
            res = run_backtest_engine(symbol, kline, cfg.get("strategy", "ma_cross"),
                                      cfg.get("params", {"fast": 5, "slow": 20}),
                                      float(cfg.get("initial_capital", 100_000)),
                                      c["commission_rate"], c["stamp_tax"], c["slippage_bps"],
                                      data_meta=meta)
            rows.append({"config": cfg, "metrics": res["metrics"],
                         "data_source": res.get("data_source"),
                         "stale": res.get("stale"), "as_of": res.get("as_of"),
                         "diagnostics": res.get("diagnostics")})
            if job is not None:
                job["progress"] = int((i + 1) / total * 100)
                self._persist(job)
                await self._emit(job)
        return {"rows": sorted(rows, key=lambda r: r["metrics"].get("sharpe", -99), reverse=True)}

    async def _run_sensitivity(self, params: dict, job: dict | None = None) -> dict:
        symbol = params.get("symbol", "600519.SH")
        values = params.get("values", [3, 5, 10, 20, 30])
        param = params.get("param", "fast")
        broker_id = params.get("broker_id", "")
        table = []
        total = max(len(values), 1)
        base = {"fast": 5, "slow": 20}
        kline, meta = await fetch_kline_async_meta(broker_id, symbol, 250)
        for i, v in enumerate(values):
            p = dict(base); p[param] = v
            res = run_backtest_engine(symbol, kline, "ma_cross", p, 100_000.0, data_meta=meta)
            m = res["metrics"]
            table.append({"param": v, "sharpe": m.get("sharpe"),
                          "max_drawdown": m.get("max_drawdown"),
                          "total_return": m.get("total_return"),
                          "data_source": res.get("data_source"),
                          "stale": res.get("stale"), "as_of": res.get("as_of")})
            if job is not None:
                job["progress"] = int((i + 1) / total * 100)
                self._persist(job)
                await self._emit(job)
        return {"symbol": symbol, "param": param, "table": table,
                "data_source": meta,
                "stale": meta.get("source") in ("cache", "cache_stale"),
                "as_of": (kline[-1].get("date") or kline[-1].get("time")) if kline else None}

    async def close(self) -> None:
        """停机：取消仍在排队/运行的作业。

        原实现是空方法 —— 关机时 ``_dispatch`` 协程会被事件循环遗弃，
        若进程继续存活（如开发模式下 uvicorn 热重载、或后端被单独重启），
        这些协程会继续拉 K 线/打券商。取消是幂等的：``_dispatch`` 内已捕获
        ``CancelledError`` 并落终态。
        """
        for task in list(self._tasks.values()):
            if task is not None and not task.done():
                task.cancel()
        self._tasks.clear()

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
                 "", now_iso()))
        except Exception as exc:  # noqa: BLE001
            log.warning("persist sweep failed: %s", exc)
        _record_backtest_metric("sweep")
        return res


def _record_backtest_metric(status: str) -> None:
    """可观测性：回测任务计数（指标收集器缺失时安全跳过）。"""
    try:
        from core.state import state
        m = getattr(state, "metrics", None)
        if m is not None and hasattr(m, "record_backtest"):
            m.record_backtest(status)
    except Exception:  # noqa: BLE001
        pass
