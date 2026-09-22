import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Panel,
  Select,
  type BadgeTone,
  type Column,
} from "@/design/primitives";
import {
  marketApi,
  systemApi,
  type KlineCacheStats,
  type KlineSyncStatus,
  type MarketCoverage,
  type SessionSnapshot,
  type SyncRunRecord,
  fmtBarDate,
} from "@/services/api";
import { fmtDateTime, toTimestamp } from "@/shared/time";
import { DataPaths } from "./DataPaths";
import s from "./offline.module.css";

/**
 * 离线数据 —— 「本地到底有哪些数据、新到哪天、上次同步跑成什么样」的唯一入口。
 *
 * ## 为什么必须单独有一页
 *
 * 在此之前，「今天的数据同步了没有」这件事在界面上**答不上来**：
 *
 * - 热窗口刷新的结果只写在进程内存里 ⇒ 重启即归零，用户看到的是「未知」；
 * - 全市场日线同步的结果只落在作业表，会被裁剪，且**只有成功才留痕** ⇒
 *   「今天没跑」与「跑了但失败」长得一模一样；
 * - 没有任何地方展示「在库数据新到哪天」⇒ 而 ``last_run.ok = 5221`` 只说明
 *   **调用**成功，不说明数据**够新**（源链「第一个非空即返回」时，券商本地
 *   历史停在一年前也照样 ok）。
 *
 * 所以本页的三块信息缺一不可：**同步开关与触发时刻**（会不会跑）、
 * **上次运行的落库结果**（跑成什么样）、**覆盖度报表**（数据新到哪天）。
 *
 * ## 界面纪律（全部来自踩过的坑）
 *
 * 1. **``null`` 不等于「未知」，更不等于「0」**：``last_run_persisted === null``
 *    一律渲染成「从未跑过」，而不是空白或 0；
 * 2. **空结果必须与失败同样可见**：``per_day`` 为空时显示「本地库没有任何日线
 *    数据」+ 可操作指引，绝不渲染一个空表格（那看起来像「加载中」）；
 * 3. **陈旧要顶到台面上**：``latest_day`` 落后于数据参照日时给红色提示，
 *    而不是把「昨天/上周的行情」当成今天的；
 * 4. **``skipped`` 不是失败**：单独一种 tone 与文案，不能混进「异常」。
 */
export function OfflineData() {
  const [sync, setSync] = useState<KlineSyncStatus | null>(null);
  const [cache, setCache] = useState<KlineCacheStats | null>(null);
  /** ★ 缓存统计读取失败原因（R23）：失败时不得显示成「尚无请求」——那是在断言我们并不知道的事实 */
  const [cacheErr, setCacheErr] = useState("");
  const [cov, setCov] = useState<MarketCoverage | null>(null);
  const [session, setSession] = useState<SessionSnapshot | null>(null);
  const [lookback, setLookback] = useState(30);
  /** 全量回补的目标年数（越久越慢，默认 12 年已覆盖内置策略与绝大多数回测） */
  const [fullYears, setFullYears] = useState(12);
  /** 同步状态接口的失败原因（不可静默 —— 静默会显示成「未启用」） */
  const [err, setErr] = useState("");
  /** 覆盖度接口的失败原因（与 err 分开：一个挂了不代表另一个也挂了） */
  const [covErr, setCovErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    void marketApi
      .klineSyncStatus()
      .then((r) => {
        setSync(r);
        setErr("");
      })
      .catch((e: unknown) => {
        setSync(null);
        setErr(e instanceof Error ? e.message : String(e));
      });
    void marketApi
      .klineCacheStats()
      .then((r) => {
        setCache(r);
        setCacheErr("");
      })
      .catch((e: unknown) => {
        setCache(null);
        setCacheErr(e instanceof Error ? e.message : String(e));
      });
    void marketApi
      .coverage({ lookbackDays: lookback })
      .then((r) => {
        setCov(r);
        setCovErr("");
      })
      .catch((e: unknown) => {
        setCov(null);
        setCovErr(e instanceof Error ? e.message : String(e));
      });
    void marketApi
      .session()
      .then(setSession)
      .catch(() => setSession(null));
  }, [lookback]);

  useEffect(load, [load]);

  const triggerSync = () => {
    setBusy(true);
    setMsg("");
    void systemApi
      .submitJob({
        kind: "system.sync_bars",
        name: "手动同步日线",
        params: { period: "1d", adjust: "qfq", lookback: 120, concurrency: 4 },
      })
      .then((r) => {
        setMsg(`已提交同步任务（${r.id}，状态 ${r.status}）。完成后点「刷新」查看结果。`);
        setBusy(false);
      })
      .catch((e: unknown) => {
        setMsg(`提交失败：${e instanceof Error ? e.message : String(e)}`);
        setBusy(false);
      });
  };

  /**
   * 全量回补历史（V11 §5.3 P0-3 III）。
   *
   * 与「立即同步日线」的区别：后者只把**最近** `lookback` 根合并进库（日常维护，
   * 要的是「够新」）；本动作按自然年**向前逐页**取，把历史一次性补齐。
   *
   * ★ 并发必须压低（4→2）：全量是「每只标的翻十几次区间请求」，在券商渠道上
   * 单只耗时是增量的十几倍，并发高只会把回源压垮、把失败率推上去。
   * ★ 结果里必须能看出**有没有真的翻页**：没有支持区间的源时会退化为单次大
   * window，此时「全量」名不副实 —— 由 `paged` 字段暴露（见下方提示块）。
   */
  const triggerFullBackfill = () => {
    setBusy(true);
    setMsg("");
    void systemApi
      .submitJob({
        kind: "system.sync_bars",
        name: `全量回补日线（近 ${fullYears} 年）`,
        params: {
          period: "1d",
          adjust: "qfq",
          mode: "full",
          full_years: fullYears,
          concurrency: 2,
        },
      })
      .then((r) => {
        setMsg(
          `已提交全量回补任务（${r.id}，状态 ${r.status}）。` +
            `该任务按自然年逐年向前取数，耗时较长；` +
            `重复执行会自动跳过已补齐的标的（断点续传），可放心重跑。`,
        );
        setBusy(false);
      })
      .catch((e: unknown) => {
        setMsg(`提交失败：${e instanceof Error ? e.message : String(e)}`);
        setBusy(false);
      });
  };

  // ---- 上次运行（落库） ----
  /**
   * ⚠️ **两条流不能混**：
   * - `last_run_persisted` = 热窗口刷新（`market.sync`）：只把最近若干天的热数据
   *   回源一遍，detail 里只有 `codes/ok/fail/hot_days`；
   * - `last_bars_run` = 全市场日线同步（`sync.bars`）：detail 里才有
   *   `sync_mode/paged/as_of_min/skipped_complete/stale/as_of_max`。
   *
   * 「同步总览」这块讲的是**热刷新调度器**（`sync.sync_time`/`enabled`/`hot_days`），
   * 所以它的「上次运行」用前者；而「数据够不够新」「全量回补到底成没成」是后者
   * 的事实 —— 从前者读会**永远读到 undefined**（表现为「点了按钮没反应」）。
   */
  const hotRec: SyncRunRecord | null = sync?.last_run_persisted ?? null;
  const barsRec: SyncRunRecord | null =
    sync?.last_bars_run ?? cov?.sync ?? null;
  const run = hotRec?.detail ?? {};
  const bars = barsRec?.detail ?? {};

  const toneOf = (rec: SyncRunRecord | null): BadgeTone =>
    rec === null
      ? "neutral"
      : rec.status === "ok"
        ? "success"
        : rec.status === "partial"
          ? "warning"
          : rec.status === "skipped"
            ? "info"
            : "danger";
  const runLabel: Record<string, string> = {
    ok: "成功",
    partial: "部分成功",
    error: "失败",
    skipped: "已跳过",
  };
  const labelOf = (rec: SyncRunRecord | null) =>
    rec === null
      ? "从未跑过"
      : (runLabel[rec.status ?? ""] ?? rec.status ?? "未知");

  const runTone = toneOf(hotRec);
  const runText = labelOf(hotRec);
  const barsTone = toneOf(barsRec);
  const barsText = labelOf(barsRec);

  /** 热刷新流的详情（只有 codes/ok/fail/hot_days 口径）。 */
  const runDetailText = hotRec
    ? run.summary
      ? String(run.summary)
      : run.reason
        ? String(run.reason)
        : run.error
          ? String(run.error)
          : `刷新 ${run.codes ?? "—"} 只 · 成功 ${run.ok ?? "—"}` +
            (run.fail ? ` · 失败 ${run.fail}` : "")
    : "";

  /** 日线同步流的详情（total/ok/failed/stale 口径）。 */
  const barsDetailText = barsRec
    ? bars.error
      ? String(bars.error)
      : `扫描 ${bars.total ?? "—"} · 成功 ${bars.ok ?? "—"}` +
        (bars.failed ? ` · 失败 ${bars.failed}` : "") +
        (bars.stale ? ` · 陈旧 ${bars.stale}` : "") +
        (bars.bars_written ? ` · 写入 ${bars.bars_written} 根` : "")
    : "";

  // ---- 覆盖度 ----
  const perDay = cov?.per_day ?? [];
  const asOf = session?.as_of ?? "";
  // ★ 「数据新到哪天」与「该看哪天的行情」必须对着比，否则「昨天同步成功」
  //   会被读成「今天的数据已就绪」。
  const stale = !!cov?.latest_day && !!asOf && cov.latest_day !== asOf;

  const dayCols: Column<{ dt: string; codes: number }>[] = [
    {
      key: "dt",
      header: "交易日",
      width: 140,
      render: (r) => (
        <span className={s.row}>
          <span className={s.mono}>{fmtBarDate(r.dt)}</span>
          {r.dt === cov?.latest_day ? <Badge tone="info">最新</Badge> : null}
        </span>
      ),
    },
    {
      key: "codes",
      header: "在库标的数",
      width: 110,
      render: (r) => <span className={s.mono}>{r.codes}</span>,
    },
    {
      key: "gap",
      header: "与上一交易日",
      render: (r, i) => {
        const prev = perDay[i + 1];
        if (!prev) return <span className={s.dim}>—</span>;
        const d = r.codes - prev.codes;
        // 差额大不代表出错（新股/退市/停牌都可能），所以只提示差异不判对错
        return (
          <span className={d === 0 ? s.dim : s.mono}>
            {d === 0 ? "持平" : d > 0 ? `+${d}` : String(d)}
          </span>
        );
      },
    },
  ];

  const provCols: Column<{ provider_id: string; bars: number; share: number }>[] = [
    {
      key: "provider_id",
      header: "数据源",
      width: 160,
      // 空 provider_id 是**历史遗留行**（provider_id 列是后加的），不能显示成空白
      render: (r) => (
        <span className={s.mono}>{r.provider_id || "（未标注来源）"}</span>
      ),
    },
    { key: "bars", header: "K 线根数", width: 120, render: (r) => <span className={s.mono}>{r.bars}</span> },
    {
      key: "share",
      header: "占比",
      render: (r) => <span className={s.mono}>{(r.share * 100).toFixed(1)}%</span>,
    },
  ];

  const hot = sync?.hot ?? {};
  const coldText = hot.cold_enabled
    ? `${hot.cold_path || "—"}（${hot.cold_rows ?? 0} 行）`
    : "未启用（归档回退主库表）";

  return (
    <div className={s.wrap}>
      {err ? (
        <Panel title="同步状态">
          <div className={s.body}>
            <EmptyState text={`同步状态加载失败：${err}`} />
          </div>
        </Panel>
      ) : null}

      <div className={s.top}>
        <Panel
          title="同步总览"
          extra={
            <span className={s.hint}>
              每日 {sync?.sync_time || "16:00"} 自动刷新热数据 · 超过 {hot.hot_days ?? 92} 天转冷仓
            </span>
          }
        >
          <div className={s.body}>
            {!sync && !err ? (
              <EmptyState text="正在读取同步状态…" />
            ) : !sync || sync.initialized === false ? (
              <>
                {/* ★ 「接口不可达」与「后端未装配 market_sync」是两回事（R23）：
                    /market/kline/sync-status 在未装配时是 200 + {initialized:false}，接口可达。
                    用「或」把两者糊在一起，用户就得在「查网络」和「查装配」之间猜。 */}
                <EmptyState
                  text={
                    err
                      ? `同步状态读取失败：${err} —— 这是接口不可达，与后端是否装配 market_sync 无关`
                      : "同步器未初始化 —— 后端未装配 market_sync（接口可达，已如实返回该状态）"
                  }
                />
                {/* ★ 「调度器没启动」≠「从来没有同步过」：落库历史与调度器无关，
                    在出故障的这一刻丢掉它，用户恰好失去唯一线索。 */}
                {barsRec || hotRec ? (
                  <div className={s.note}>
                    但仍有历史记录：热刷新
                    {hotRec ? `${labelOf(hotRec)}（${hotRec.last_ts ?? "时间未知"}）` : "从未跑过"}；
                    日线同步
                    {barsRec ? `${labelOf(barsRec)}（${barsRec.last_ts ?? "时间未知"}）` : "从未跑过"}。
                  </div>
                ) : null}
              </>
            ) : (
              <>
                <div className={s.kv}>
                  <span>定时刷新</span>
                  <Badge tone={sync.enabled ? "success" : "warning"}>
                    {sync.enabled ? "已启用" : "未启用"}
                  </Badge>
                </div>
                <div className={s.kv}>
                  <span>触发时刻</span>
                  <span className={s.mono}>{sync.sync_time || "—"}</span>
                </div>
                <div className={s.kv}>
                  <span>上次热刷新（落库）</span>
                  <span className={s.row}>
                    <Badge tone={runTone}>{runText}</Badge>
                    {hotRec?.last_ts ? (
                      <span className={s.mono}>{fmtDateTime(toTimestamp(hotRec.last_ts))}</span>
                    ) : null}
                  </span>
                </div>
                {hotRec ? <div className={s.note}>{runDetailText}</div> : null}
                {/* ★★ 「上次日线同步」用的是**另一条流**（sync.bars）。全市场日线
                    落库是用户真正关心的那条；它与热刷新是两件事，必须分开显示，
                    否则用户会以为「热刷新成功 = 日线已更新」。 */}
                <div className={s.kv}>
                  <span>上次日线同步（落库）</span>
                  <span className={s.row}>
                    <Badge tone={barsTone}>{barsText}</Badge>
                    {barsRec?.last_ts ? (
                      <span className={s.mono}>{fmtDateTime(toTimestamp(barsRec.last_ts))}</span>
                    ) : null}
                  </span>
                </div>
                {barsRec ? <div className={s.note}>{barsDetailText}</div> : null}
                {/* ★ 从未跑过不是「异常」，但也不是「一切正常」——它意味着
                    这个功能一次都没成功过，用户需要知道该去点一次。 */}
                {hotRec === null && barsRec === null && sync.initialized ? (
                  <div className={s.noteWarn}>
                    尚未记录到任何一次同步运行。若刚安装或从未同步过，可点下方
                    「立即同步日线」手动跑一次。
                  </div>
                ) : null}
                {/* ★ ok 很高但陈旧很多 —— 这是「假成功」，必须顶到台面上。
                    ⚠️ 取自 sync.bars 流：热刷新的 detail 里没有 stale。 */}
                {bars.stale ? (
                  <div className={s.noteError}>
                    上次日线同步有 {bars.stale} 只标的的数据「陈旧」（最新截至
                    {bars.as_of_max ? ` ${fmtBarDate(String(bars.as_of_max))}` : "未知"}）。
                    这通常意味着券商客户端本地历史未下载到近期，或在线数据源不可用。
                  </div>
                ) : null}
                {/* ★★ 「名义全量、实际没翻页」是最危险的一种假成功：sync_mode 写着
                    full、ok 是满的，用户会以为历史已补齐，其实只是把最近 N 根
                    又写了一遍。这里必须明确否定它。 */}
                {bars.sync_mode === "full" && bars.paged === false ? (
                  <div className={s.noteError}>
                    上次「全量回补未生效」：当前数据源链上没有任何源支持按日期区间
                    取数（免费在线源只接受「最近 N 根」），实际退化为单次大窗口，
                    历史并没有真正补齐。请到「连接管理」连接券商后重跑 ——
                    券商渠道是唯一支持按年翻页取历史的数据源。
                  </div>
                ) : null}
                {bars.degraded_reason ? (
                  <div className={s.noteWarn}>{String(bars.degraded_reason)}</div>
                ) : null}
                {/* 全量回补的结果：历史推到了哪一年 + 跳过了多少（断点续传的证据） */}
                {bars.sync_mode === "full" && bars.paged ? (
                  <div className={s.note}>
                    上次全量回补：历史最早到
                    {bars.as_of_min ? ` ${fmtBarDate(String(bars.as_of_min))}` : "未知"}，
                    写入 {bars.bars_written ?? "—"} 根
                    {bars.skipped_complete
                      ? `；因本地已补齐而跳过 ${bars.skipped_complete} 只（断点续传）`
                      : ""}
                    。
                  </div>
                ) : null}
                <div className={s.actions}>
                  <Button size="sm" onClick={load}>
                    刷新
                  </Button>
                  <Button size="sm" variant="primary" disabled={busy} onClick={triggerSync}>
                    {busy ? "提交中…" : "立即同步日线"}
                  </Button>
                </div>
                {/* ---- 全量回补（V11 §5.3 P0-3 III） ---- */}
                <div className={s.actions}>
                  <span className={s.hint}>补齐历史</span>
                  <Select
                    value={String(fullYears)}
                    onChange={(e) => setFullYears(Number(e.target.value))}
                    options={[
                      { value: "5", label: "近 5 年" },
                      { value: "8", label: "近 8 年" },
                      { value: "12", label: "近 12 年" },
                      { value: "20", label: "近 20 年" },
                    ]}
                  />
                  <Button size="sm" disabled={busy} onClick={triggerFullBackfill}>
                    {busy ? "提交中…" : "全量回补历史"}
                  </Button>
                </div>
                <div className={s.hint}>
                  全量回补按自然年向前逐页取数，耗时远高于增量同步；重复执行会跳过
                  已补齐的标的，可放心重跑。仅券商数据源支持按日期区间取历史。
                </div>
                {msg ? <div className={s.note}>{msg}</div> : null}
              </>
            )}
          </div>
        </Panel>

        <Panel title="冷热分层与缓存">
          <div className={s.body}>
            <div className={s.kv}>
              <span>热窗口</span>
              <span className={s.mono}>{hot.hot_days ?? "—"} 天</span>
            </div>
            <div className={s.kv}>
              <span>冷热边界</span>
              <span className={s.mono}>{hot.hot_cutoff || "—"}</span>
            </div>
            <div className={s.kv}>
              <span>热表行数</span>
              <span className={s.mono}>{hot.hot_rows ?? "—"}</span>
            </div>
            <div className={s.kv}>
              <span>冷仓</span>
              <span className={s.mono}>{coldText}</span>
            </div>
            <div className={s.kv}>
              <span>缓存序列数</span>
              <span className={s.mono}>{cache?.series ?? "—"}</span>
            </div>
            {/* hit_rate 为 null = 还没有任何请求；显示 0% 会被读成「全部未命中」。
                读取失败同理不能说「尚无请求」—— 那是把「读不到」冒充成「没有」。 */}
            <div className={s.kv}>
              <span>缓存命中率</span>
              <span className={s.mono}>
                {cacheErr
                  ? `统计不可用（${cacheErr}）`
                  : !cache
                    ? "—"
                    : cache.hit_rate === null || cache.hit_rate === undefined
                      ? "尚无请求"
                      : `${(cache.hit_rate * 100).toFixed(1)}%`}
              </span>
            </div>
            <div className={s.kv}>
              <span>陈旧服务次数</span>
              <span className={s.mono}>{cache?.stale_serves ?? "—"}</span>
            </div>
          </div>
        </Panel>
      </div>

      {/* ---- 数据目录（P0-3 II）：导出目录 / 冷库目录运行期可改，主库需重启 ---- */}
      <DataPaths />

      <Panel
        title="本地数据覆盖度"
        extra={
          <span className={s.row}>
            <span className={s.hint}>统计最近</span>
            <Select
              value={String(lookback)}
              onChange={(e) => setLookback(Number(e.target.value))}
              options={[
                { value: "10", label: "10 天" },
                { value: "30", label: "30 天" },
                { value: "60", label: "60 天" },
                { value: "120", label: "120 天" },
              ]}
            />
            <span className={s.hint}>的日线（{cov?.adjust || "qfq"}）</span>
          </span>
        }
      >
        <div className={s.body}>
          {covErr ? (
            <EmptyState text={`覆盖度报表加载失败：${covErr}`} />
          ) : cov === null ? (
            <EmptyState text="正在读取覆盖度…" />
          ) : (
            <>
              <div className={s.kv}>
                <span>数据最新交易日</span>
                <span className={s.row}>
                  <span className={s.mono}>
                    {cov.latest_day ? fmtBarDate(cov.latest_day) : "—"}
                  </span>
                  {cov.latest_day ? (
                    <Badge tone="neutral">{cov.latest_codes} 只</Badge>
                  ) : null}
                </span>
              </div>
              <div className={s.kv}>
                <span>数据参照日（该看哪天）</span>
                <span className={s.mono}>
                  {asOf ? fmtBarDate(asOf) : "—"}
                  {session?.trading_day === false ? "（今日非交易日）" : ""}
                </span>
              </div>
              <div className={s.kv}>
                <span>有数据的交易日数</span>
                <span className={s.mono}>
                  {cov.days_with_data} / {cov.lookback_days}
                </span>
              </div>
              {/* ★ 落后就是落后：不提示的话，「昨天同步成功」会被读成「今天数据已就绪」 */}
              {stale ? (
                <div className={s.noteWarn}>
                  本地日线最新到 {fmtBarDate(cov.latest_day)}，而当前应看
                  {fmtBarDate(asOf)} 的行情 —— 数据尚未更新到最新交易日。
                  若已开启定时同步，请检查「定时任务」里
                  <code className={s.code}>system.sync_bars</code> 是否失败。
                </div>
              ) : null}
              {cov.days_with_data === 0 ? (
                // ★ 空结果必须与失败同样可见：空表格看起来像「正在加载」
                <EmptyState text="本地库中没有任何日线数据。请先点上方「立即同步日线」，或到「连接管理」添加并连接券商（券商渠道无限流，是最快的补齐方式）。" />
              ) : (
                <div className={s.tables}>
                  <div className={s.tableBox}>
                    <div className={s.tableTitle}>每个交易日在库标的数</div>
                    <DataTable columns={dayCols} rows={perDay} rowKey={(r) => r.dt} />
                  </div>
                  <div className={s.tableBox}>
                    <div className={s.tableTitle}>数据来源占比</div>
                    {cov.provider_share.length === 0 ? (
                      <EmptyState text="没有可统计的 K 线行" />
                    ) : (
                      <DataTable
                        columns={provCols}
                        rows={cov.provider_share}
                        rowKey={(r) => r.provider_id || "__unknown__"}
                      />
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

export default OfflineData;
