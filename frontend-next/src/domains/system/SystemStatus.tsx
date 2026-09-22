import { useEffect, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Panel, type Column } from "@/design/primitives";
import {
  marketApi,
  systemApi,
  type CapabilityItem,
  type DataProviderInfo,
  type DataProvidersResponse,
  type DatahubPolicies,
  type KlineCacheStats,
  type KlineSyncStatus,
  type SourceDiagnostics,
} from "@/services/api";
import { useQuotesStore } from "@/stores/quotes";
import { fmtDate } from "@/shared/time";
import s from "./systemstatus.module.css";

interface CheckRow {
  name: string;
  /** pass / warn / fail（后端口径，不是布尔） */
  status: string;
}

/**
 * 系统状态：健康探针 / 能力自描述 / 行情通道 / K 线数据同步。
 *
 * ★ 契约要点：
 *   - /health 的 checks 是 {name, status: pass|warn|fail}，没有 ok/detail 字段
 *   - /capabilities 返回 {total, agent_visible, items:[...]}，不是裸数组；
 *     条目字段是 category（不是 domain）
 *   - /market/kline/sync-status 的 last_run 只反映**本进程**跑过的那次
 *     （重启后为空属正常，不代表没同步）
 *
 * 「K 线数据同步」面板补上了此前的静默断裂：`marketApi.klineSyncStatus` 早就
 * 存在却**没有任何页面消费**，于是「每日 16:00 自动下载」对用户完全不可见 ——
 * 开关开着不等于今天跑过，用户需要一个能区分「已同步 / 待同步 / 未启用」的地方。
 */
export function SystemStatus() {
  const [checks, setChecks] = useState<CheckRow[]>([]);
  const [version, setVersion] = useState("");
  const [caps, setCaps] = useState<CapabilityItem[]>([]);
  const [capTotal, setCapTotal] = useState(0);
  /** 能力清单加载失败原因（不可静默） */
  const [capErr, setCapErr] = useState("");
  const [sync, setSync] = useState<KlineSyncStatus | null>(null);
  const [cache, setCache] = useState<KlineCacheStats | null>(null);
  /**
   * ★ 同步状态读取失败的原因（R23）。
   * 不能吞掉：`/market/kline/sync-status` 在「后端未装配 market_sync」时是
   * **200 + {initialized:false}**，接口明明可达；而请求失败时同样是 `sync === null`。
   * 两者落进同一个 `!sync` 分支 ⇒ 若不记住原因，只能写成「未装配**或**接口不可达」，
   * 用户被迫在「查后端装配」和「查网络」之间猜。
   */
  const [syncErr, setSyncErr] = useState("");
  /** ★ 缓存统计读取失败原因：失败时不得显示成「尚无请求」（那是在断言一个我们并不知道的事实） */
  const [cacheErr, setCacheErr] = useState("");
  /** 数据源矩阵（/data/providers）—— 未接界面前「哪个源挂了」只能翻 API */
  const [providers, setProviders] = useState<DataProvidersResponse | null>(null);
  const [providersErr, setProvidersErr] = useState("");
  /**
   * ★ 是否已收到过数据源矩阵的响应（R23）。
   * `providers` 初值是 null，而请求失败时也是 null ⇒ 只看 `!providers` 的话，
   * **请求还没回来就已经在断言「不可用」**（假警报），且与「真失败」长得一样。
   */
  const [providersLoaded, setProvidersLoaded] = useState(false);
  /** 限流策略（/datahub/policies）：行情多久算过期、多久合并一次请求 */
  const [policies, setPolicies] = useState<DatahubPolicies | null>(null);
  /** 最近一次数据源失败溯源（/data/source/diagnostics）—— 把「不支持」从「网络坏了」里捞出来 */
  const [diag, setDiag] = useState<SourceDiagnostics | null>(null);
  /** ★ 溯源读取失败原因：本面板的全部意义就是给原因，读不到时必须直说，不能只留一排「—」 */
  const [diagErr, setDiagErr] = useState("");
  const [probeBusy, setProbeBusy] = useState(false);
  const [err, setErr] = useState("");
  const socketState = useQuotesStore((st) => st.socketState);
  const refs = useQuotesStore((st) => st.refs);

  const load = () => {
    void systemApi
      .health()
      .then((h) => {
        setVersion(h.version ?? "");
        setChecks((h.checks ?? []).map((c) => ({ name: c.name, status: c.status })));
        setErr("");
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
    void systemApi
      .capabilities()
      .then((res) => {
        setCaps(res.items ?? []);
        setCapTotal(res.total ?? res.items?.length ?? 0);
      })
      .catch((e: unknown) => {
        setCaps([]);
        setCapTotal(0);
        // ★ 失败必须可见：静默置空会让界面显示「REST 能力 0」，
        // 与「系统真的没注册能力」长得一样，排障方向整个被带偏。
        setCapErr(e instanceof Error ? e.message : String(e));
      });
    void marketApi
      .klineSyncStatus()
      .then((r) => {
        setSync(r);
        setSyncErr("");
      })
      // ★ 失败必须可见（与上面 capabilities / dataProviders 同一纪律）：
      //   静默置空会让「接口挂了」和「后端没装配同步器」长得一样。
      .catch((e: unknown) => {
        setSync(null);
        setSyncErr(e instanceof Error ? e.message : String(e));
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
    void systemApi
      .dataProviders()
      .then((r) => {
        setProviders(r ?? null);
        setProvidersErr("");
        setProvidersLoaded(true);
      })
      // ★ 失败必须可见：置空会让「接口挂了」和「没有数据源」长得一样
      .catch((e: unknown) => {
        setProviders(null);
        setProvidersErr(e instanceof Error ? e.message : String(e));
        setProvidersLoaded(true);
      });
    void systemApi
      .datahubPolicies()
      .then(setPolicies)
      .catch(() => setPolicies(null));
    void systemApi
      .sourceDiagnostics()
      .then((d) => {
        setDiag(d ?? null);
        setDiagErr("");
      })
      .catch((e: unknown) => {
        setDiag(null);
        setDiagErr(e instanceof Error ? e.message : String(e));
      });
  };

  useEffect(load, []);

  const cols: Column<CheckRow>[] = [
    { key: "name", header: "检查项", width: 200, render: (r) => r.name },
    {
      key: "status",
      header: "状态",
      width: 80,
      render: (r) => (
        <Badge tone={r.status === "pass" ? "success" : r.status === "warn" ? "warning" : "danger"}>
          {r.status === "pass" ? "正常" : r.status === "warn" ? "告警" : "异常"}
        </Badge>
      ),
    },
  ];

  /** 数据源画像列：status 是后端算好的结论（active/dependency/commercial 三者合成） */
  const providerCols: Column<DataProviderInfo>[] = [
    { key: "provider", header: "源", width: 110, mono: true, render: (r) => String(r.provider) },
    { key: "name", header: "名称", width: 150, render: (r) => String(r.name ?? "—") },
    {
      key: "status", header: "状态", width: 110,
      render: (r) => {
        const st = String(r.status ?? "");
        const tone = st === "active" ? "success" : st === "dependency-ready" ? "warning" : "neutral";
        return <Badge tone={tone}>{st || "—"}</Badge>;
      },
    },
    {
      key: "caps", header: "能力数", width: 80, mono: true,
      render: (r) => String(r.capabilities?.length ?? 0),
    },
    {
      key: "commercial", header: "商用", width: 70,
      render: (r) => (
        <Badge tone={r.commercial_ok ? "success" : "neutral"}>
          {r.commercial_ok ? "允许" : "禁止"}
        </Badge>
      ),
    },
    {
      key: "note", header: "说明",
      render: (r) => String(r.license_note ?? r.requires ?? "—"),
    },
  ];

  const byDomain = caps.reduce<Record<string, number>>((acc, c) => {
    const d = c.category || c.path.split("/")[1] || "其他";
    acc[d] = (acc[d] ?? 0) + 1;
    return acc;
  }, {});

  // ---- K 线数据同步状态 ----
  const hot = sync?.hot ?? {};
  const lastRun = sync?.last_run ?? null;
  const today = fmtDate(Date.now());
  const syncedToday = !!lastRun?.date && lastRun.date.startsWith(today);
  // 「开关开着」与「今天跑过」是两件事：只显示开关会让用户以为已经同步
  const syncTone: "success" | "warning" | "neutral" = !sync?.enabled
    ? "warning"
    : syncedToday
      ? "success"
      : "warning";
  const syncText = !sync?.enabled
    ? "未启用"
    : syncedToday
      ? "今日已同步"
      : "待同步";
  const lastText = lastRun
    ? `${lastRun.date ?? "—"} · ${lastRun.ok ?? 0}/${lastRun.codes ?? 0} 只成功` +
      (lastRun.fail ? ` · ${lastRun.fail} 失败` : "") +
      (lastRun.count_per_code ? ` · 每只 ${lastRun.count_per_code} 根` : "")
    : "本进程尚未运行（重启后清零属正常）";
  const coldText = hot.cold_enabled
    ? `${hot.cold_path || "—"}（${hot.cold_rows ?? 0} 行）`
    : "未启用（归档回退主库表）";

  return (
    <div className={s.wrap}>
      <div className={s.top}>
        <Panel title="服务信息">
          <div className={s.infoBody}>
            <div className={s.kv}>
              <span>版本</span>
              <span className={s.mono}>{version || "—"}</span>
            </div>
            <div className={s.kv}>
              <span>行情通道</span>
              <Badge tone={socketState === "open" ? "success" : "warning"}>
                {socketState}
              </Badge>
            </div>
            <div className={s.kv}>
              <span>活跃订阅</span>
              <span className={s.mono}>{Object.keys(refs).length}</span>
            </div>
            <div className={s.kv}>
              <span>REST 能力</span>
              <span className={s.mono}>{capTotal}</span>
            </div>
            <Button size="sm" onClick={load}>
              刷新
            </Button>
          </div>
        </Panel>

        <Panel title="能力分布（按域）">
          <div className={s.domainBody}>
            {capErr ? (
              <EmptyState text={`能力清单加载失败：${capErr}`} />
            ) : Object.keys(byDomain).length === 0 ? (
              <EmptyState text="无法读取能力清单" />
            ) : (
              Object.entries(byDomain)
                .sort((a, b) => b[1] - a[1])
                .map(([d, n]) => (
                  <div key={d} className={s.domainRow}>
                    <span>{d}</span>
                    <span className={s.mono}>{n}</span>
                  </div>
                ))
            )}
          </div>
        </Panel>
      </div>

      <Panel
        title="K 线数据同步"
        extra={
          <span style={{ fontSize: "var(--font-xs)", color: "var(--text-faint)" }}>
            每日定时刷新热数据 · 超过 3 个月转冷仓 · 过点启动自动补跑
          </span>
        }
      >
        <div className={s.infoBody}>
          {!sync && !syncErr ? (
            <EmptyState text="正在读取同步状态…" />
          ) : !sync || sync.initialized === false ? (
            <EmptyState
              text={
                syncErr
                  ? `同步状态读取失败：${syncErr}（点上方「刷新」重试）`
                  : "同步器未初始化 —— 后端未装配 market_sync（接口可达，已如实返回该状态）"
              }
            />
          ) : (
            <>
              <div className={s.kv}>
                <span>状态</span>
                <Badge tone={syncTone}>{syncText}</Badge>
              </div>
              <div className={s.kv}>
                <span>触发时刻</span>
                <span className={s.mono}>{sync.sync_time || "—"}</span>
              </div>
              <div className={s.kv}>
                <span>最近运行</span>
                <span className={s.mono}>{lastText}</span>
              </div>
              <div className={s.kv}>
                <span>热窗口</span>
                <span className={s.mono}>
                  {hot.hot_days ?? "—"} 天
                  {hot.hot_cutoff ? ` · 早于 ${hot.hot_cutoff} 为冷数据` : ""}
                </span>
              </div>
              <div className={s.kv}>
                <span>热表行数</span>
                <span className={s.mono}>{hot.hot_rows ?? "—"}</span>
              </div>
              <div className={s.kv}>
                <span>冷仓</span>
                <span className={s.mono}>{coldText}</span>
              </div>
            {/* 缓存命中率：hit_rate 为 null 表示「还没有任何请求」，
                不能显示成 0%（那会被读成「全部未命中」）；
                读取失败也不能说「尚无请求」—— 那是在断言一个我们并不知道的事实 */}
            <div className={s.kv}>
              <span>缓存序列数</span>
              <span className={s.mono}>{cache?.series ?? "—"}</span>
            </div>
            <div className={s.kv}>
              <span>缓存命中率</span>
              <span className={s.mono}>
                {cacheErr
                  ? `统计不可用（${cacheErr}）`
                  : !cache
                    ? "—"
                    : cache.hit_rate === null || cache.hit_rate === undefined
                      ? "尚无请求"
                      : `${(cache.hit_rate * 100).toFixed(1)}%（命中 ${cache.hits ?? 0} / 未命中 ${cache.misses ?? 0}）`}
              </span>
            </div>
            </>
          )}
        </div>
      </Panel>

      <Panel title="数据源与限流">
        {providersErr ? (
          <EmptyState text={`数据源矩阵加载失败：${providersErr}`} />
        ) : !providersLoaded ? (
          <EmptyState text="正在读取数据源矩阵…" />
        ) : !providers ? (
          <EmptyState text="数据源矩阵接口可达，但未返回矩阵内容（契约异常，非网络问题）" />
        ) : (
          <>
            <div className={s.kv}>
              <span>选股可用性</span>
              <span>
                <Badge tone={providers.screening_ready ? "success" : "warning"}>
                  {providers.screening_ready ? "可用" : "不可用"}
                </Badge>
                {providers.screening_providers?.length
                  ? ` ${providers.screening_providers.join(" / ")}`
                  : ""}
              </span>
            </div>
            <div className={s.kv}>
              <span>本地数据</span>
              <span>{providers.local_data_available ? "可用" : "不可用"}</span>
            </div>
            <div className={s.kv}>
              <span>商用模式</span>
              <span>{providers.commercial_mode ? "是" : "否"}</span>
            </div>
            <div className={s.kv}>
              <span>默认源策略</span>
              <span className={s.mono}>{providers.default_source_policy ?? "—"}</span>
            </div>
            <div className={s.kv}>
              <span>降级链版本</span>
              <span className={s.mono}>{providers.chain_version ?? "—"}</span>
            </div>
            <div className={s.tableArea} style={{ maxHeight: 200 }}>
              <DataTable
                columns={providerCols}
                rows={providers.providers ?? []}
                rowKey={(r) => String(r.provider)}
              />
            </div>
          </>
        )}
        {/* 限流策略：行情多久过期、多久合并一次 —— 决定「数据看着没更新」是不是配置问题 */}
        {policies ? (
          <>
            <div className={s.kv}>
              <span>默认 TTL</span>
              <span className={s.mono}>
                {policies.default?.ttl_ms ?? "—"} ms（最小间隔{" "}
                {policies.default?.min_interval_ms ?? "—"} ms，合并窗口{" "}
                {policies.default?.coalesce_within_ms ?? "—"} ms）
              </span>
            </div>
            <div className={s.kv}>
              <span>主题策略</span>
              <span className={s.mono}>
                {Object.keys(policies.topics ?? {}).length} 条（
                {Object.entries(policies.topics ?? {})
                  .slice(0, 4)
                  .map(([k, v]) => `${k}:${v?.ttl_ms ?? "—"}ms`)
                  .join("，")}
                {Object.keys(policies.topics ?? {}).length > 4 ? " …" : ""}）
              </span>
            </div>
          </>
        ) : null}
      </Panel>

      <Panel title="数据源失败溯源">
        {diagErr ? (
          <EmptyState text={`溯源信息读取失败：${diagErr}（点上方「刷新」重试）`} />
        ) : null}
        <div className={s.kv}>
          <span>能力</span>
          {/* ★ 未就绪时不得默认成 "sector"：那会把「还没读到」显示成「查的就是板块能力」 */}
          <span className={s.mono}>{diag?.capability ?? "—"}</span>
        </div>
        <div className={s.kv}>
          <span>本应被尝试</span>
          <span className={s.mono}>
            {!diag
              ? "—"
              : diag.chain?.length
                ? diag.chain.join(" → ")
                : "（空 ⇒ 该源不提供该能力，不是网络问题）"}
          </span>
        </div>
        <div className={s.kv}>
          <span>实际尝试</span>
          <span className={s.mono}>
            {!diag
              ? "—"
              : diag.tried?.length
                ? diag.tried.join("；")
                : diag.chain?.length === 0
                  ? "（无源可试）"
                  : "（暂无最近失败记录，点下方「触发探测」可主动跑一次）"}
          </span>
        </div>
        {diag?.interpretation ? (
          <div className={s.kv}>
            <span>解读</span>
            <span className={s.mono}>{diag.interpretation}</span>
          </div>
        ) : null}
        <div style={{ marginTop: 8 }}>
          <Button
            size="sm"
            disabled={probeBusy}
            onClick={async () => {
              setProbeBusy(true);
              try {
                const d = await systemApi.sourceDiagnostics({ probe: 1 });
                setDiag(d ?? null);
                setDiagErr("");
              } catch (e: unknown) {
                setDiag(null);
                setDiagErr(e instanceof Error ? e.message : String(e));
              } finally {
                setProbeBusy(false);
              }
            }}
          >
            {probeBusy ? "探测中…" : "触发探测（broker × sector）"}
          </Button>
        </div>
      </Panel>

      <Panel title="健康检查">
        <div className={s.tableArea}>
          {err ? (
            <EmptyState text={err} />
          ) : (
            <DataTable columns={cols} rows={checks} rowKey={(r) => r.name} />
          )}
        </div>
      </Panel>
    </div>
  );
}

export default SystemStatus;
