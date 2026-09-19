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
  const [cov, setCov] = useState<MarketCoverage | null>(null);
  const [session, setSession] = useState<SessionSnapshot | null>(null);
  const [lookback, setLookback] = useState(30);
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
      .then(setCache)
      .catch(() => setCache(null));
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

  // ---- 上次运行（落库） ----
  const rec: SyncRunRecord | null = sync?.last_run_persisted ?? null;
  const run = rec?.detail ?? {};
  const runTone: BadgeTone =
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
  const runText =
    rec === null
      ? "从未跑过"
      : (runLabel[rec.status ?? ""] ?? rec.status ?? "未知");

  // 详情优先用 sync_bars 的口径（total/ok/failed/stale），没有就用热刷新的口径（codes/ok/fail）
  const runDetailText = rec
    ? run.summary
      ? String(run.summary)
      : run.reason
        ? String(run.reason)
        : run.error
          ? String(run.error)
          : `扫描 ${run.total ?? run.codes ?? "—"} · 成功 ${run.ok ?? "—"}` +
            (run.failed ? ` · 失败 ${run.failed}` : "") +
            (run.fail ? ` · 失败 ${run.fail}` : "") +
            (run.stale ? ` · 陈旧 ${run.stale}` : "")
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
            {!sync || sync.initialized === false ? (
              <EmptyState text="同步器未初始化（后端未装配 market_sync，或接口不可达）" />
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
                  <span>上次运行（落库）</span>
                  <span className={s.row}>
                    <Badge tone={runTone}>{runText}</Badge>
                    {rec?.last_ts ? (
                      <span className={s.mono}>{fmtDateTime(toTimestamp(rec.last_ts))}</span>
                    ) : null}
                  </span>
                </div>
                {rec ? <div className={s.note}>{runDetailText}</div> : null}
                {/* ★ 从未跑过不是「异常」，但也不是「一切正常」——它意味着
                    这个功能一次都没成功过，用户需要知道该去点一次。 */}
                {rec === null && sync.initialized ? (
                  <div className={s.noteWarn}>
                    尚未记录到任何一次同步运行。若刚安装或从未同步过，可点下方
                    「立即同步日线」手动跑一次。
                  </div>
                ) : null}
                {/* ★ ok 很高但陈旧很多 —— 这是「假成功」，必须顶到台面上 */}
                {run.stale ? (
                  <div className={s.noteError}>
                    上次同步有 {run.stale} 只标的的数据**陈旧**（最新截至
                    {run.as_of_max ? ` ${fmtBarDate(String(run.as_of_max))}` : "未知"}）。
                    这通常意味着券商客户端本地历史未下载到近期，或在线数据源不可用。
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
            {/* hit_rate 为 null = 还没有任何请求；显示 0% 会被读成「全部未命中」 */}
            <div className={s.kv}>
              <span>缓存命中率</span>
              <span className={s.mono}>
                {cache?.hit_rate === null || cache?.hit_rate === undefined
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
                  {fmtBarDate(asOf)} 的行情 —— 数据**未更新到最新交易日**。
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
