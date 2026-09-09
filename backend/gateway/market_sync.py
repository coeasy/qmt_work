"""行情缓存定时维护：收盘后刷新今年热数据 + 跨年归档（行情基础设施专用）。

区别于被移除的通用「Scheduler / 定时任务」特性：这里是行情数据自维护的、
轻量的进程内异步循环，仅维护 K 线缓存，不做任意任务调度。

- 跨年归档：每天兜底把热表中早于今年的行搬入归档表（幂等）。
- 定时刷新：每日收盘后（配置 market.sync.time）对本地已缓存的今年热序列
  force 回源刷新一次，保证今年的数据新而全。
- 全部经 runtime_config 热更新（market.sync.*），默认关闭。

挂载：main.py lifespan 内 start，停机 finally 内 stop。
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("qmt_work.market_sync")


class MarketSync:
    def __init__(self, state, runtime_config, interval: float = 60.0):
        self.state = state
        self.cfg = runtime_config
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._last_run_date: str | None = None
        self._eod_last_run_date: str | None = getattr(state, "_eod_last_run_date", None)
        db = getattr(state, "db", None)
        if not self._eod_last_run_date and db is not None:
            row = db.query_one("SELECT value FROM local_sync_meta WHERE key=?",
                               ("eod_last_run_date",))
            self._eod_last_run_date = row.get("value") if row else None
        self._eod_job_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("market.sync.enabled"))

    @property
    def sync_time(self) -> str:
        return str(self.cfg.get("market.sync.time") or "16:00")

    @property
    def eod_enabled(self) -> bool:
        return bool(self.cfg.get("market.eod.enabled"))

    @property
    def eod_time(self) -> str:
        return str(self.cfg.get("market.eod.time") or "18:00")

    async def start(self):
        # 启动即做一次跨年归档维护（幂等，热表小，成本可忽略）
        await self._rollover()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        return self._task

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _rollover(self) -> dict:
        kc = getattr(self.state, "kline_cache", None)
        if kc is None:
            return {"moved": 0, "deleted": 0}
        try:
            res = await asyncio.to_thread(kc.archive_rollover)
            if res.get("moved"):
                log.info("kline archive rollover: moved=%s deleted=%s",
                         res["moved"], res["deleted"])
            return res
        except Exception as exc:  # noqa: BLE001
            log.warning("kline archive rollover failed: %s", exc)
            return {"moved": 0, "deleted": 0}

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("market sync tick failed: %s", exc)
            interval = float(self.cfg.get("market.sync.interval") or self.interval)
            await asyncio.sleep(max(10.0, interval))

    async def _tick(self):
        await self._rollover()
        today = time.strftime("%Y-%m-%d")
        now_hm = time.strftime("%H:%M")
        hot_due = self.enabled and self._last_run_date != today and now_hm >= self.sync_time
        eod_due = self.eod_enabled and self._eod_last_run_date != today and now_hm >= self.eod_time
        if not hot_due and not eod_due:
            return
        # 仅交易日触发（用券商真实日历，失败静默回退为周末规则）
        try:
            from gateway.trading_session import default_session
            if not default_session.is_trading_day():
                return
        except Exception:  # noqa: BLE001
            pass
        if hot_due:
            self._last_run_date = today
            await self._refresh_hot()
        if eod_due:
            await self._schedule_eod(today, now_hm)

    async def _schedule_eod(self, today: str, now_hm: str) -> None:
        """提交 durable EOD 任务；超过触发时间的启动属于 misfire catch-up。"""
        if not self.eod_enabled or now_hm < self.eod_time:
            return
        if self._eod_last_run_date == today:
            return
        from app.runtime.jobs import JobSpec, get_runtime, sync_runner
        current = get_runtime().get(self._eod_job_id) if self._eod_job_id else None
        if current and current["status"] in ("queued", "running"):
            return
        params = {
            "limit": int(self.cfg.get("market.eod.limit") or 0),
            "concurrency": 8, "lookback": 320, "adjust": "qfq",
            "provider_id": "auto", "batch_id": f"eod-{today}",
            "max_attempts": int(self.cfg.get("market.eod.retry") or 3),
        }
        self._eod_job_id = get_runtime().submit(JobSpec(
            kind="sync", name=f"EOD 全市场同步 {today}",
            runner=sync_runner(params), priority=2, params=params))
        self._eod_last_run_date = today
        self.state._eod_last_run_date = today
        db = getattr(self.state, "db", None)
        if db is not None:
            db.execute("INSERT OR REPLACE INTO local_sync_meta(key,value,updated_at) "
                       "VALUES (?,?,datetime('now'))", ("eod_last_run_date", today))
        log.info("EOD sync scheduled: %s", self._eod_job_id)

    async def _refresh_hot(self):
        kc = getattr(self.state, "kline_cache", None)
        if kc is None:
            return
        try:
            series = await asyncio.to_thread(kc.all_series)
        except Exception as exc:  # noqa: BLE001
            log.warning("market sync list series failed: %s", exc)
            return
        codes = sorted({s["code"] for s in series if s.get("period") in ("1d", "1w", "1mo")})
        if not codes:
            log.info("market sync: no cached series to refresh")
            return
        from tools import fetch_kline_cached

        results = {"ok": 0, "fail": 0}

        async def _one(code: str):
            try:
                await fetch_kline_cached(code, "1d", 250, force=True)
                results["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                results["fail"] += 1
                log.info("market sync skip %s: %s", code, exc)

        # 分批并发刷新，控制瞬时负载
        for i in range(0, len(codes), 30):
            batch = codes[i:i + 30]
            await asyncio.gather(*(_one(c) for c in batch))
        # 停机前记录到日志（含 last_run 供前端状态展示）
        self.state._market_sync_last = {"date": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "codes": len(codes), "ok": results["ok"],
                                        "fail": results["fail"]}
        log.info("market sync refresh: codes=%s ok=%s fail=%s",
                 len(codes), results["ok"], results["fail"])
