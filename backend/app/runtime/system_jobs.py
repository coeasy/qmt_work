"""V9 Phase 7：system.* JobKind 注册表（9 个真实 runner 工厂 + 默认调度）。

调度器（ScheduleRunner）与 REST 均可按 kind 提交；全部委托既有真实服务
（BarsSyncer / DatasetSnapshotStore / quality / screener），零 mock：
    system.eod                 EOD 全流程管线（app/runtime/eod.py）
    system.sync_bars           全市场日线同步（复用 sync_runner）
    system.sync_fundamentals   基本面因子预取（选股加速缓存）
    system.refresh_universe    股票池刷新（本地列表重建）
    system.reconcile_bars      跨源对账（quality.reconcile_bars）
    system.rolling_repair      缺失区间滚动修复（按链补数）
    system.coverage_report     覆盖率 + 各源贡献占比报表
    system.publish_snapshot    Dataset Snapshot 发布
    system.classic_screen      经典策略选股（app/screener/classic.py，复刻 Sequoia-X）

另外 ``register_all`` 会播种两条默认调度（见 ``DEFAULT_SCHEDULES``），
让「定时更新日线 / 定时自动选股」开箱即用——用户不必自己写 cron。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("qmt_work.runtime.system_jobs")

SYSTEM_JOB_KINDS: tuple[str, ...] = (
    "system.eod", "system.sync_bars", "system.sync_fundamentals",
    "system.refresh_universe", "system.reconcile_bars",
    "system.rolling_repair", "system.coverage_report", "system.publish_snapshot",
    "system.classic_screen",
)

Runner = Callable[[dict], Any]


def _db():
    from core.db import get_db
    return get_db()


# ---------------------------------------------------------------- sync_bars
def _sync_bars_runner(params: dict) -> Runner:
    """全市场日线同步 + **结果落库**（``sync_state``，V11 §5.3 F）。

    ★ 为什么要包一层：``sync_runner`` 的结果原本只落在 ``runtime_jobs.result_json``，
    而作业表会被裁剪 —— 「上一次同步跑成什么样」跨重启就查不到了，可用户判断
    「今天的数据到底同步了没有」靠的正是它。
    ★ **失败也写**：只记成功的运行，等于把「今天没跑」与「跑了但失败」在界面上
    混成一件事 —— 而这正是用户要区分的那件事。
    """
    from app.runtime.jobs import sync_runner
    inner = sync_runner(params)
    _keys = ("period", "adjust", "lookback", "concurrency", "limit",
             "mode", "full_years")

    def _detail(job: dict, summary: dict) -> dict:
        """只留可展示的摘要 —— ``errors`` 在全市场量级可能上千条，不该整条塞进库里。"""
        errs = list(summary.get("errors") or [])
        return {
            # ⚠️ ``mode`` 在这里是**流的判别标签**（与 ``market_sync`` 写的
            #    "market.sync" 同构），不是同步模式 —— 同步模式另见 ``sync_mode``。
            #    两者混用会让界面把「全量回补」误判成普通同步。
            "mode": "sync_bars",
            "job_id": str(job.get("id") or ""),
            "params": {k: params.get(k) for k in _keys},
            "total": int(summary.get("total") or 0),
            "ok": int(summary.get("ok") or 0),
            "failed": int(summary.get("failed") or 0),
            "stale": int(summary.get("stale") or 0),
            "bars_written": int(summary.get("bars_written") or 0),
            "as_of_max": summary.get("as_of_max") or "",
            "elapsed_ms": int(summary.get("elapsed_ms") or 0),
            # ---- 全量回补（V11 §5.3 P0-3 III）：这三项缺一不可 ----
            # ``sync_mode``：incremental / full
            "sync_mode": summary.get("mode") or "incremental",
            # ``paged``：是否**真的**按日期区间向前翻页。full 而 paged=False
            # ⇒ 历史并未补齐，界面必须能看见（否则「全量完成」是句谎话）。
            "paged": bool(summary.get("paged")),
            # 写入的最早一根 = 历史推到了哪一年
            "as_of_min": summary.get("as_of_min") or "",
            # 断点续传跳过数：中断后重跑这个数会明显变大
            "skipped_complete": int(summary.get("skipped_complete") or 0),
            "errors": errs[:20],
            "errors_truncated": max(0, len(errs) - 20),
        }

    async def _run(job: dict) -> dict:
        from app.sync.state import STREAM_SYNC_BARS, record_run
        try:
            result = await inner(job)
        except Exception as exc:
            record_run(STREAM_SYNC_BARS, status="error",
                       detail={"mode": "sync_bars",
                               "job_id": str(job.get("id") or ""),
                               "params": {k: params.get(k) for k in _keys},
                               "error": str(exc)})
            raise
        summary = result if isinstance(result, dict) else {}
        ok = int(summary.get("ok") or 0)
        failed = int(summary.get("failed") or 0)
        stale = int(summary.get("stale") or 0)
        # 部分成功（有失败或有陈旧）也是**要看得见**的状态，不能压成 ok。
        status = "ok" if not (failed or stale) else ("error" if not ok else "partial")
        record_run(STREAM_SYNC_BARS, status=status, detail=_detail(job, summary))
        return result

    return _run


# ---------------------------------------------------------- fundamentals
def _sync_fundamentals_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.screener.fundamentals import FUNDAMENTAL_FIELDS, fetch_fundamentals
        from app.screener.universe import UniverseSpec, resolve_universe

        fields = list(params.get("fields") or FUNDAMENTAL_FIELDS)
        codes = list(params.get("codes") or [])
        if not codes:
            uni = await resolve_universe(
                UniverseSpec(kind=str(params.get("universe") or "all")),
                policy_str=str(params.get("source_policy") or "auto"))
            codes = uni["codes"]
            if not codes:
                raise RuntimeError(
                    "股票池为空（数据源不可用且本地无股票列表）——请先同步数据或连接券商")
        limit = int(params.get("limit") or 0)
        if limit:
            codes = codes[:limit]

        async def _chunked():
            out: dict[str, Any] = {}
            missing: list[str] = []
            size = int(params.get("chunk") or 200)
            for i in range(0, len(codes), size):
                part = codes[i:i + size]
                res = await fetch_fundamentals(
                    part, policy_str=str(params.get("source_policy") or "auto"),
                    fields=fields)
                for f, m in res["fields"].items():
                    out.setdefault(f, {}).update(m)
                missing.extend(res["missing_codes"])
                job["report"](int((i + size) / max(1, len(codes)) * 100),
                              f"基本面 {min(i + size, len(codes))}/{len(codes)}")
            return {"fields": out, "missing_codes": missing,
                    "codes": len(codes), "source_policy": params.get("source_policy")}

        return await _chunked()
    return _run


# ------------------------------------------------------------- universe
def _refresh_universe_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.screener.universe import UniverseSpec, resolve_universe
        kind = str(params.get("universe") or "all")
        uni = await resolve_universe(
            UniverseSpec(kind=kind),
            policy_str=str(params.get("source_policy") or "auto"))
        job["report"](60, f"universe {kind}: {len(uni['codes'])} codes")
        if not uni["codes"]:
            raise RuntimeError(
                "股票池为空：数据源不可用且本地无股票列表（503 语义，不伪造）")
        return {"kind": kind, "codes": len(uni["codes"]),
                "provider_used": uni.get("provider_used"),
                "degraded": uni.get("degraded")}
    return _run


# --------------------------------------------------------- reconcile
def _reconcile_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        import asyncio

        from datasource.quality import reconcile_bars
        stats = await asyncio.to_thread(
            reconcile_bars, _db(),
            period=str(params.get("period") or "1d"),
            adjust=str(params.get("adjust") or "qfq"),
            lookback_days=int(params.get("lookback_days") or 10),
            price_diff_pct=float(params.get("price_diff_pct") or 0.005))
        job["report"](100, f"对账完成：conflicts={stats['conflicts']}")
        return stats
    return _run


# ----------------------------------------------------- rolling repair
def _rolling_repair_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.sync.bars import BarsSyncer
        lookback = int(params.get("lookback") or 30)

        def _cb(done: int, total: int, code: str) -> None:
            job["report"](int(done / total * 100) if total else 100,
                          f"修复 {done}/{total}（{code}）")

        syncer = BarsSyncer(
            concurrency=int(params.get("concurrency") or 4),
            lookback=lookback,
            provider_id=str(params.get("provider_id") or "auto"),
            batch_id=str(params.get("batch_id") or "") or None)
        summary = await syncer.sync_stock_list(
            limit=int(params.get("limit") or 0) or None, progress_cb=_cb)
        result = summary.to_dict()
        result["mode"] = "rolling_repair"
        return result
    return _run


# ---------------------------------------------------- coverage report
def _coverage_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        import asyncio

        from datasource.quality import coverage_report
        rep = await asyncio.to_thread(
            coverage_report, _db(),
            period=str(params.get("period") or "1d"),
            adjust=str(params.get("adjust") or "qfq"),
            lookback_days=int(params.get("lookback_days") or 10))
        job["report"](100, "coverage report done")
        return rep
    return _run


# ------------------------------------------------- publish snapshot
def _publish_snapshot_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from datasource.snapshots import DatasetSnapshotStore
        store = DatasetSnapshotStore(_db())
        # ★ 未显式给批次号时回退到「最近一次写入 local_bars 的批次」。
        #   此前传空串 ⇒ 快照必然 0 行（还标着 complete）。手动触发发布任务的
        #   用户意图显然不是「发布一份空数据集」。
        batch = str(params.get("batch_id") or "") or store.latest_batch_id()
        snap = store.publish_local_bars(
            str(params.get("dataset") or "cn_equity_daily"),
            str(params.get("version") or batch),
            str(params.get("provider_id") or "auto"),
            batch,
            quality_state=str(params.get("quality_state") or "provisional"),
            calendar_version=str(params.get("calendar_version") or ""),
            adjustment_version=str(params.get("adjustment_version") or "qfq"),
            manifest=dict(params.get("manifest") or {}))
        finality = str(params.get("finality") or "")
        if finality:
            from datasource.quality import apply_finality
            apply_finality(_db(), snap["id"], finality)
            snap["finality"] = finality
        return snap
    return _run


# ------------------------------------------------------------- EOD
def _eod_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.runtime.eod import run_eod_pipeline
        merged = dict(params)
        merged["_job"] = job
        return await run_eod_pipeline(merged)
    return _run


# ------------------------------------------------------ classic_screen
def _classic_screen_runner(params: dict) -> Runner:
    """定时经典策略选股（复刻 Sequoia-X 的「收盘后自动跑策略」）。

    与手动选股共用同一条链路：解析股票池 → BarsProvider 批量取日线 →
    ``screener.classic.run_classic`` 求值。差别只是由调度触发、结果落在作业里。
    """
    async def _run(job: dict) -> dict:
        import asyncio as _asyncio

        from app.data.bars_provider import BarsProvider
        from app.screener.classic import STRATEGY_IDS, run_classic
        from app.screener.universe import UniverseSpec, resolve_universe

        strategies = params.get("strategies") or params.get("strategy") or []
        if isinstance(strategies, str):
            strategies = [strategies]
        strategies = [s for s in strategies if s] or list(STRATEGY_IDS)
        # 写错的策略名必须让作业**失败并留下原因**，而不是每天跑出「0 命中」——
        # 后者用户会当成「行情不好」，永远发现不了配置是错的。
        unknown = [s for s in strategies if s not in STRATEGY_IDS]
        if unknown:
            raise RuntimeError(
                f"未知经典策略：{', '.join(unknown)}（可选 {', '.join(STRATEGY_IDS)}）")

        limit = int(params.get("limit") or 50)
        period = str(params.get("period") or "1d")
        adjust = str(params.get("adjust") or "qfq")
        policy = str(params.get("source_policy") or "auto")

        uni = await resolve_universe(
            UniverseSpec(kind=str(params.get("universe") or "all")), policy_str=policy)
        codes = uni["codes"]
        if not codes:
            raise RuntimeError("股票池为空——请先运行日线同步任务或连接券商数据源")
        max_codes = int(params.get("max_codes") or 0)
        if max_codes and max_codes > 0:
            codes = codes[:max_codes]

        # ★ 长任务必须**边干边报进度**。全市场取数（5000+ 只）与全池形态识别都是
        #   几十秒级的段，中间不报进度会让界面停在「执行中 0%」一动不动 ——
        #   用户分不清「在跑」和「卡死」。租约也靠 report 续期（见 jobs.py 的 reaper）。
        _rep0 = job.get("report") or (lambda *a, **k: None)
        _rep0(2, f"股票池 {len(codes)} 只，开始取日线…")

        bp = BarsProvider()
        bars_map, report = await bp.get_bars_batch(
            codes, period=period, adjust=adjust, policy_str=policy,
            offline=bool(params.get("offline")), lite=True)
        _rep0(35, f"日线就绪 {len(bars_map or {})} 只，开始逐只形态识别…")

        # ★ 启动前置体检：日线是不是已经同步到「最近交易日」。
        #
        #   选股结果的全部意义建立在「数据是新的」之上 —— 用上上周的日线跑出来的
        #   命中，与今天的行情毫无关系。此前这条链路的毛病是：日线没同步（或只同步
        #   到半年前）照样一路跑完并报成功，用户看到「今天没选出票」，真相却是
        #   「数据根本没到位」。现在发现落后就**先补历史再选**。
        #
        # ⚠️ 判据是「落后于最近交易日」，不是「非空」—— 「非空 ≠ 够新」（V11）：
        #   券商本地库可能只到一年前却照样非空。
        from datetime import date as _date

        from app.screener.picks import bars_last_date as _last_date_of
        from app.sync.calendar import prev_trading_day as _prev_trading_day
        from core.clock import bar_date as _bar_date

        _rep = job.get("report") or (lambda *a, **k: None)
        expect_date = _bar_date(_prev_trading_day(_date.today(), include_self=True))
        last_date = _last_date_of(bars_map) if bars_map else ""
        # 默认开启；显式传 auto_backfill=False 才关（用 `is not False` 而非布尔真值，
        # 避免 0 / "" 这类 falsy 配置被当成「没传」而静默改变行为）。
        auto_backfill = params.get("auto_backfill") is not False
        backfill_note = ""

        # ★ 「日期未知」必须按**落后**处理，不能按「不落后」。
        #   旧写法 ``(last_date and last_date < expect_date)`` 在 ``last_date == ""``
        #   时为假 ⇒ 「取到了 K 线但解析不出日期」被当成数据是新的，于是既不补数、
        #   也不告警。无法确认新鲜度时唯一诚实的动作是**当作陈旧去补**。
        _stale = bool(expect_date) and (not last_date or last_date < expect_date)
        if auto_backfill and expect_date and (not bars_map or _stale):
            _rep(0, f"日线截至 {last_date or '无'}，落后于 {expect_date}，先补历史数据")
            _sync_job = {"id": str(job.get("id") or job.get("job_id") or ""), "report": _rep}
            _sync_params = {
                "adjust": adjust,
                "lookback": int(params.get("backfill_lookback") or 320),
                "concurrency": int(params.get("concurrency") or 8),
                # 默认**增量**：全量回补是几小时级的作业，不能因为一次选股就触发。
                "mode": str(params.get("backfill_mode") or "incremental"),
                "limit": int(params.get("backfill_limit") or 0),
            }
            try:
                await _sync_bars_runner(_sync_params)(_sync_job)
                backfill_note = f"已自动补历史（{expect_date} 之前缺失）"
            except Exception as _exc:
                # 补数失败**不掩盖选股本身**：照常跑完，但把失败原因带进结果，
                # 否则用户只会看到「命中 0 只」而永远不知道是补数挂了。
                backfill_note = f"自动补历史失败：{_exc}"
                log.warning("classic_screen 自动补历史失败：%s", _exc)
            # 补完必须**重新取一次** —— 否则这次选股用的还是补之前的旧数据
            bars_map, report = await bp.get_bars_batch(
                codes, period=period, adjust=adjust, policy_str=policy,
                offline=bool(params.get("offline")), lite=True)
            last_date = _last_date_of(bars_map) if bars_map else ""

        # ★ 护栏：「扫了 0 只」不是「今天没选出票」，是**根本没拿到数据**。
        #   此前这里会一路跑到 run_classic，返回 total_hits=0 并报成功 ——
        #   用户看到的是「今天行情不好」，而真相是日线同步没跑成（本项目
        #   「假成功」家族：流程跑完了，数据没动）。必须让它**失败并留下原因**。
        if not bars_map:
            raise RuntimeError(
                f"选股未取得任何 K 线（股票池 {len(codes)} 只，数据源 {report.provider_used or '无'}）："
                "请先运行「定时更新日线」任务或连接券商数据源后再试"
                + (f"；降级原因：{report.degraded_reason}" if report.degraded_reason else ""))

        results: dict[str, list] = {}
        for i, sid in enumerate(strategies):
            # 全池逐只形态识别是纯 CPU ⇒ 必须移出事件循环
            _rep0(35 + int(60 * i / max(1, len(strategies))),
                  f"形态识别 {i + 1}/{len(strategies)}：{sid}")
            results[sid] = await _asyncio.to_thread(
                run_classic, bars_map, sid, params.get("classic_params"), limit,
                uni.get("names") or {})
        _rep0(96, "结果落库…")

        total_hits = sum(len(v) for v in results.values())
        # ★ 落库：定时选股的结果必须**有稳定的界面**。此前只存在于本作业的返回值里，
        #   用户只能去「任务运行时」翻一个巨大的 JSON 字段，翻不到就等于没有 ——
        #   「每天收盘后自动选股」这条链路事实上是跑给日志看的。
        from app.screener.picks import bars_last_date, save_run

        saved = save_run(
            results=results, scanned=len(bars_map), source="schedule",
            job_id=str(job.get("id") or job.get("job_id") or ""),
            bar_date=last_date,
            degraded=bool(report.degraded),
            degraded_reason=report.degraded_reason or "",
            # 名称兜底：即使某策略行没带 name，也按股票池的名称表补上
            names=uni.get("names") or {},
        )

        return {
            "strategies": strategies,
            "scanned": len(bars_map),
            "total_hits": total_hits,
            # ★ 「命中 0 只」必须与「扫了 0 只」区分开：这里显式给出结论，
            #   让结果展开时一眼看出是「行情不好」还是「数据没到位」。
            "conclusion": ("无命中（行情形态不满足）" if total_hits == 0
                           else f"命中 {total_hits} 只"),
            "run_id": saved.get("run_id", ""),
            "saved": saved.get("saved", 0),
            "bar_date": last_date,
            # ★ 数据新鲜度：选股依据的是哪一天、本该是哪一天、是否自动补过。
            #   没有这三项时，「命中 0 只」与「数据没到位」在界面上长得一模一样。
            "expect_bar_date": expect_date,
            # 「日期未知」也算落后（与上面的补数判据同口径）：宁可说「无法确认」，
            # 也不能把一个空的 bar_date 报告成「数据是最新的」。
            "data_lag": bool(expect_date and (not last_date or last_date < expect_date)),
            "auto_backfill": backfill_note,
            "results": results,
            "provider": report.provider_used,
            "degraded": report.degraded,
            "degraded_reason": report.degraded_reason or "",
        }
    return _run


_FACTORIES: dict[str, Callable[[dict], Runner]] = {
    "system.eod": _eod_runner,
    "system.sync_bars": _sync_bars_runner,
    "system.sync_fundamentals": _sync_fundamentals_runner,
    "system.refresh_universe": _refresh_universe_runner,
    "system.reconcile_bars": _reconcile_runner,
    "system.rolling_repair": _rolling_repair_runner,
    "system.coverage_report": _coverage_runner,
    "system.publish_snapshot": _publish_snapshot_runner,
    "system.classic_screen": _classic_screen_runner,
}


def runner_for(kind: str) -> Runner | None:
    """按 kind 取 runner 工厂；非 system kind 返回 None（调用方回退内置）。"""
    factory = _FACTORIES.get(kind)
    return factory


# ------------------------------------------------------ 默认调度（易用性）
# 为什么需要：``register_all`` 只注册 runner 工厂，**不建任何调度** ——
# 用户想要「每天自动更新日线 / 自动选股」必须自己填 cron，门槛很高
# （Sequoia-X 就是靠 crontab 在收盘后跑，这里把它内置成开箱即用）。
#
# 幂等：用**固定 schedule_id** 播种，已存在即跳过，绝不覆盖用户改过的配置。
# 时间排布刻意构成一条链：16:00 先把当日日线落库 → 16:15 才有数据可选股 →
# 18:30 再跑 EOD 全流程对账/快照（既有的 ensure_default_schedule）。
# 若把选股排在日线更新之前，它会拿昨天的 K 线跑，选出的是「昨天的结果」。
#
# ⚠️ 两者**必须错开**（双保险）：
#   ① 顺序：16:00 日线落库 → 16:15 才选股（倒过来就是拿昨天的 K 线跑）；
#   ② 资源组：``RESOURCE_GROUP``（app/runtime/jobs.py）已把二者都归入
#      ``local_bars`` 互斥组 ⇒ 即使用户把 cron 改成同一时刻也不会并发
#      （此前只靠时间错开，改个 cron 就会读到半更新的日线）。
#    存量部署的旧值由迁移 v27 对齐（且只在用户没改过时生效）。
DEFAULT_SCHEDULES: tuple[dict, ...] = (
    {
        "id": "sch-default-sync-bars",
        "kind": "system.sync_bars",
        "cron": "0 16 * * 1-5",           # 每交易日 16:00（收盘 15:00 后数据已稳定）
        "name": "收盘后更新日线数据",
        # concurrency / lookback 用**实测验证过**的值（2026-09-19 全市场 5224 只）：
        # - concurrency=4：券商补下载走的是本地 RPC，4 并发下全市场 7 分钟跑完
        #   （ok=5221 / stale=4）。默认 8 在全市场量级未经实测，且在线源在
        #   并发 32 时曾集体超时触发熔断 —— 批量同步宁慢勿炸。
        # - lookback=120：足够覆盖全部内置策略（最长 high_tight_flag 的 60 日
        #   回看 + MA20 + RPS 20 日），而默认 320 会把耗时翻近一倍。
        #   窗口增量是幂等合并，缩短回看**不会**丢历史（旧数据不删除）。
        # - mode=incremental：日常维护只要「够新」；需要把历史一次性补齐时，
        #   到「离线数据」页点「全量回补」（mode=full），不要塞进每日调度。
        "params": {"period": "1d", "adjust": "qfq", "mode": "incremental",
                   "concurrency": 4, "lookback": 120},
    },
    {
        "id": "sch-default-classic-screen",
        "kind": "system.classic_screen",
        "cron": "15 16 * * 1-5",          # 每交易日 16:15（日线更新之后，留出 15 分钟）
        "name": "收盘后经典策略选股",
        "params": {"strategies": ["turtle_trade", "ma_volume"], "limit": 50},
    },
)


def ensure_default_schedules(enabled: bool = True) -> list[dict]:
    """播种默认调度（幂等）。返回本次新建的调度；失败只记日志，不影响启动。"""
    from app.runtime.schedules import ScheduleStore

    created: list[dict] = []
    try:
        store = ScheduleStore(_db())
        for spec in DEFAULT_SCHEDULES:
            try:
                if store.get(spec["id"]):
                    continue            # 已存在（含用户改过的）⇒ 绝不覆盖
                created.append(store.create(
                    spec["kind"], spec["cron"], name=spec["name"],
                    params=spec["params"], schedule_id=spec["id"],
                    enabled=enabled))
                log.info("已播种默认调度：%s（%s %s）",
                         spec["name"], spec["cron"], spec["kind"])
            except Exception as exc:  # noqa: BLE001 — 单条失败不影响其余
                log.warning("默认调度 %s 创建失败：%s", spec["id"], exc)
    except Exception as exc:  # noqa: BLE001 — 调度播种失败绝不能阻断启动
        log.warning("默认调度播种失败（已跳过，不影响启动）：%s", exc)
    return created


def register_all(seed_schedules: bool = True) -> None:
    """把 system.* 工厂注册进 JobRuntime（main 启动时调用一次）。

    ``seed_schedules=True`` 时顺带播种默认调度（收盘后日线更新 + 经典策略选股），
    让「定时更新日线 / 定时自动选股」开箱即用；幂等且失败不阻断启动。
    """
    from app.runtime.jobs import register_runner_factory
    for kind, factory in _FACTORIES.items():
        register_runner_factory(kind, factory)
    if seed_schedules:
        ensure_default_schedules()


__all__ = [
    "SYSTEM_JOB_KINDS", "runner_for", "register_all",
    "DEFAULT_SCHEDULES", "ensure_default_schedules",
]
