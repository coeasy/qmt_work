"""行情缓存定时维护：每日刷新**热窗口**数据 + 冷数据分层（行情基础设施专用）。

区别于被移除的通用「Scheduler / 定时任务」特性：这里是行情数据自维护的、
轻量的进程内异步循环，仅维护 K 线缓存，不做任意任务调度。

冷热分层（2026-09-17）：
- **热窗口** = 最近 ``market.hot_days`` 天（默认 92 ≈ 3 个月），落在主库热表；
- **冷仓** = 更早的历史，放在**独立 SQLite 文件**（``bars_cold.db``），
  每日同步**不再更新**它 —— 老数据既不用重下（省带宽），也不用重写（省写放大）；
- 读路径对冷热透明（``KlineCache.get`` 先取热表、不足从冷仓补足）。

每日维护三件事：
- **热窗口滚动**：把热表里早于窗口的行搬入冷仓（幂等，每次 tick 兜底执行）。
- **定时刷新**：每日 ``market.sync.time``（默认 **16:00**）对**热表内**的日线序列
  force 回源刷新一次，取数根数按热窗口换算（见 :func:`hot_fetch_count`）。
- **过点补跑**：触发时间已过而当天还没跑过 ⇒ 启动后立刻补跑（判据是
  ``_last_run_date != today`` 且 ``now >= sync_time``，与是否刚启动无关）。
- 全部经 runtime_config 热更新（``market.sync.*`` / ``market.hot_days``）；
  ``market.sync.enabled`` 默认**开**。

挂载：main.py lifespan 内 start，停机 finally 内 stop。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from core.clock import to_iso

log = logging.getLogger("qmt_work.market_sync")


def _sh_now() -> datetime:
    """统一按 Asia/Shanghai（交易所所在地）取当前本地时间（naive datetime）。

    V9 §14.4：EOD 触发必须锚定交易所日历时区，而非宿主机本地时区——
    否则跨时区部署（UTC 服务器/海外 VPS）会在错误的时刻触发收盘同步。
    tzdata 缺失时回退主机本地时间（行为与旧版一致，不阻断）。
    测试打桩点：monkeypatch `gateway.market_sync._sh_now`。
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return datetime.now()


#: 单次回源多取的余量（根）：覆盖「窗口边界那几天正好是长假」与最后一根未收盘的情况。
_HOT_FETCH_MARGIN = 15


def hot_fetch_count(hot_days: int) -> int:
    """把「热窗口天数（自然日）」换算成回源要取的 **K 线根数（交易日）**。

    热窗口按自然日定义（``bars_hot_days``，默认 92 天 ≈ 3 个月），而回源接口按
    「最近 N 根」取数，两者单位不同 —— 直接用 92 会少取（92 个交易日 ≈ 4.5 个月），
    用 250 会多取（≈ 1 年，把冷数据天天重拉一遍）。

    换算：一年约 250 个交易日 / 365 个自然日 ≈ 0.685，再留 ``_HOT_FETCH_MARGIN``
    根余量兜住长假与未收盘的最后一根。取够即可：多取的根由 ``KlineCache.put``
    按日期路由，落在窗口外的自然进冷仓，不会污染热表。
    """
    days = max(1, int(hot_days or 1))
    return int(days * 250 / 365) + _HOT_FETCH_MARGIN


class MarketSync:
    def __init__(self, state, runtime_config, interval: float = 60.0,
                 job_runtime=None, sync_job_factory=None, state_recorder=None):
        """V10 A3：删除 gateway→app 的反向 import。

        ``job_runtime``（JobRuntime store）与 ``sync_job_factory(params)``（返回 JobSpec
        的工厂）由 app 层装配时注入；未注入则 EOD 自动触发降级为 no-op（非致命）。

        ``state_recorder``（V11 §5.3 F）：形如 ``recorder(*, status, detail)`` 的回调，
        由 app 层绑定到 ``app.sync.state.record_run``。**同样用注入而非直接 import** ——
        ``gateway`` 是顶层包，直接 import app 会重新引入刚被消除的反向依赖。
        未注入则静默降级（状态落库是观测增强项，不该把同步本身带崩）。
        """
        self.state = state
        self.cfg = runtime_config
        self.interval = interval
        self._job_runtime = job_runtime
        self._sync_job_factory = sync_job_factory
        self._state_recorder = state_recorder
        self._task: asyncio.Task | None = None
        self._last_run_date: str | None = None
        self._eod_last_run_date: str | None = getattr(state, "_eod_last_run_date", None)
        db = getattr(state, "db", None)
        if not self._eod_last_run_date and db is not None:
            row = db.query_one("SELECT value FROM local_sync_meta WHERE key=?",
                               ("eod_last_run_date",))
            self._eod_last_run_date = row.get("value") if row else None
        self._eod_job_id: str | None = None
        #: 上次滚动搬移时的热窗口边界（None = 本次进程还没搬过 ⇒ 启动必搬一次）
        self._rollover_cutoff: str | None = None

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

    def _record(self, status: str, detail: dict) -> None:
        """把本次热窗口刷新的结果落库（回调由 app 层注入，见 ``__init__``）。

        ★ **失败/跳过也必须记**：只记成功的运行，等于把「今天没跑」与
        「跑了但失败」在界面上混成一件事 —— 而这正是用户要区分的那件事。
        """
        fn = self._state_recorder
        if fn is None:
            return
        try:
            fn(status=status, detail=detail)
        except Exception as exc:  # noqa: BLE001 — 记录失败绝不影响同步
            log.debug("market sync state record failed: %s", exc)

    async def start(self):
        # 启动即做一次热窗口滚动维护（幂等；首次调用必定执行，因为
        # `_rollover_cutoff` 初值为 None）。热表已被窗口约束到 ~3 个月，
        # 扫描成本可忽略。
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
        """把热表中早于热窗口的行搬入冷仓（幂等）。

        **按边界变化去重**：滚动搬移的结果只取决于「热窗口边界」这一个变量，
        而边界每天才变一次（``hot_days`` 天前的那一天）。原实现每个 tick（默认
        60s）都全表扫一遍热表 —— 一天 1440 次无意义的全扫。现在边界没变就直接
        跳过，边界一变立刻执行；``start()`` 里额外无条件跑一次兜底
        （覆盖「进程跨天运行但边界刚好在启动前已变」的情形）。
        """
        kc = getattr(self.state, "kline_cache", None)
        if kc is None:
            return {"moved": 0, "deleted": 0}
        try:
            cutoff = kc.hot_cutoff()
        except Exception as exc:  # noqa: BLE001
            log.warning("hot cutoff unavailable: %s", exc)
            return {"moved": 0, "deleted": 0}
        if cutoff == self._rollover_cutoff:
            return {"moved": 0, "deleted": 0}
        try:
            res = await asyncio.to_thread(kc.archive_rollover)
            self._rollover_cutoff = cutoff
            if res.get("moved"):
                log.info("kline hot-window rollover: moved=%s deleted=%s (cutoff=%s)",
                         res["moved"], res["deleted"], cutoff)
            return res
        except Exception as exc:  # noqa: BLE001
            log.warning("kline hot-window rollover failed: %s", exc)
            return {"moved": 0, "deleted": 0}

    def _apply_hot_days(self) -> None:
        """把 runtime_config 的 ``market.hot_days`` 热更新到 K 线缓存。

        「热窗口多久」是运维想随时调的参数（磁盘紧张就调小），不该要求重启客户端。
        """
        kc = getattr(self.state, "kline_cache", None)
        if kc is None:
            return
        try:
            want = int(self.cfg.get("market.hot_days") or 0)
        except (TypeError, ValueError):
            return
        if want > 0 and want != getattr(kc, "hot_days", None):
            log.info("hot_days 热更新：%s → %s", getattr(kc, "hot_days", None), want)
            kc.hot_days = want

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
        self._apply_hot_days()
        await self._rollover()
        sh = _sh_now()
        today = sh.strftime("%Y-%m-%d")
        now_hm = sh.strftime("%H:%M")
        # 到点判据 = 开关开 + 今天没跑过 + 触发时刻**已过**。
        # 「已过」这一个条件同时覆盖两种情形：① 到点触发；② **启动时已经过了 16:00**
        # —— 后者正是需求里的「超过 16 点未同步，启动之后继续检查同步」。
        # 刻意不为补跑单开分支：两套判据迟早漂移，一套天然包含它更稳。
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
            if now_hm > self.sync_time:
                log.info("行情缓存同步补跑：触发时刻 %s 已过（当前 %s），立即刷新热数据",
                         self.sync_time, now_hm)
            await self._refresh_hot()
        if eod_due:
            await self._schedule_eod(today, now_hm)

    async def _schedule_eod(self, today: str, now_hm: str) -> None:
        """提交 durable EOD 任务；超过触发时间的启动属于 misfire catch-up。"""
        if not self.eod_enabled or now_hm < self.eod_time:
            return
        if self._eod_last_run_date == today:
            return
        # V10 A3：不再反向 import app.runtime.jobs；依赖注入
        if self._job_runtime is None or self._sync_job_factory is None:
            log.warning("EOD 自动触发未接线（job_runtime/sync_job_factory 缺失），跳过")
            return
        current = self._job_runtime.get(self._eod_job_id) if self._eod_job_id else None
        if current and current["status"] in ("queued", "running"):
            return
        params = {
            "limit": int(self.cfg.get("market.eod.limit") or 0),
            "concurrency": 8, "lookback": 320, "adjust": "qfq",
            "provider_id": "auto", "batch_id": f"eod-{today}",
            "max_attempts": int(self.cfg.get("market.eod.retry") or 3),
        }
        self._eod_job_id = self._job_runtime.submit(self._sync_job_factory(params))
        self._eod_last_run_date = today
        self.state._eod_last_run_date = today
        db = getattr(self.state, "db", None)
        if db is not None:
            db.execute("INSERT OR REPLACE INTO local_sync_meta(key,value,updated_at) "
                       "VALUES (?,?,datetime('now'))", ("eod_last_run_date", today))
        log.info("EOD sync scheduled: %s", self._eod_job_id)

    async def _refresh_hot(self):
        """刷新**热窗口**内的日线序列（冷仓历史一概不碰）。

        两条与「冷热分层」配套的关键点：

        1. **清单取热表**（``kc.hot_series()``）而不是 ``all_series()``：
           冷仓里的老标的不会被每日重新下载 —— 这正是「只更新热数据」。
        2. **取数根数按热窗口算**（不再是固定 250 根 ≈ 1 年）：
           固定 250 根时，每次刷新都会把窗口外的历史重新拉一遍并写进冷仓，
           既浪费带宽，又把冷仓天天重写一遍。现在只取覆盖热窗口 + 余量的根数。
        """
        kc = getattr(self.state, "kline_cache", None)
        if kc is None:
            self._record("skipped", {"mode": "market.sync", "reason": "K 线缓存未初始化"})
            return
        try:
            series = await asyncio.to_thread(kc.hot_series)
        except Exception as exc:  # noqa: BLE001
            log.warning("market sync list series failed: %s", exc)
            self._record("error", {"mode": "market.sync",
                                   "reason": f"读取热表序列失败：{exc}"})
            return
        codes = sorted({s["code"] for s in series if s.get("period") in ("1d", "1w", "1mo")})
        if not codes:
            log.info("market sync: no cached hot series to refresh")
            # ★ 这不是「行情不好」，是**热表里根本没有日线序列**（从没同步过，
            #   或全部已被滚动进冷仓）。必须让它在界面上显示为「无可刷新标的」，
            #   而不是一片沉默。
            self._record("skipped", {
                "mode": "market.sync",
                "reason": "热表内无日线序列（从未同步过，或序列已全部滚动进冷仓）",
                "codes": 0})
            return
        from tools import fetch_kline_cached

        count = hot_fetch_count(getattr(kc, "hot_days", 92))
        results = {"ok": 0, "fail": 0}

        async def _one(code: str):
            try:
                await fetch_kline_cached(code, "1d", count, force=True)
                results["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                results["fail"] += 1
                log.info("market sync skip %s: %s", code, exc)

        # 分批并发刷新，控制瞬时负载
        for i in range(0, len(codes), 30):
            batch = codes[i:i + 30]
            await asyncio.gather(*(_one(c) for c in batch))
        # 停机前记录到日志（含 last_run 供前端状态展示）
        self.state._market_sync_last = {"date": to_iso(_sh_now()),
                                        "codes": len(codes), "ok": results["ok"],
                                        "fail": results["fail"],
                                        "hot_days": getattr(kc, "hot_days", None),
                                        "count_per_code": count}
        log.info("market sync refresh: codes=%s ok=%s fail=%s (hot_days=%s count=%s)",
                 len(codes), results["ok"], results["fail"],
                 getattr(kc, "hot_days", None), count)

        # ★ 落库（重启后仍可见）。状态三分：全成 / 部分失败 / 全失败。
        # ⚠️ ``ok`` 只表示「回源调用没抛异常」，**不等于数据已更新到最新交易日** ——
        # 源链「第一个非空即返回」的老毛病仍在（详见 docs 硬约束清单「非空≠够新」）。
        # 所以这里不把 ok 说成「数据已更新」，只如实报调用成败，界面另外展示
        # 覆盖度报表（GET /market/coverage）让用户自己看数据到底到哪天。
        if results["fail"] == 0:
            status = "ok"
        elif results["ok"] == 0:
            status = "error"
        else:
            status = "partial"
        self._record(status, {
            "mode": "market.sync",
            "date": to_iso(_sh_now()),
            "codes": len(codes), "ok": results["ok"], "fail": results["fail"],
            "hot_days": getattr(kc, "hot_days", None), "count_per_code": count,
            "summary": (f"热窗口刷新 {len(codes)} 只：成功 {results['ok']}、"
                        f"失败 {results['fail']}"),
        })
