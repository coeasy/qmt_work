import { useEffect, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Panel, type Column } from "@/design/primitives";
import { marketApi, systemApi, type CapabilityItem, type KlineCacheStats, type KlineSyncStatus } from "@/services/api";
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
      .then(setSync)
      .catch(() => setSync(null));
    void marketApi
      .klineCacheStats()
      .then(setCache)
      .catch(() => setCache(null));
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
          {!sync || sync.initialized === false ? (
            <EmptyState text="同步器未初始化（后端未装配 market_sync，或接口不可达）" />
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
                  不能显示成 0%（那会被读成「全部未命中」） */}
              <div className={s.kv}>
                <span>缓存序列数</span>
                <span className={s.mono}>{cache?.series ?? "—"}</span>
              </div>
              <div className={s.kv}>
                <span>缓存命中率</span>
                <span className={s.mono}>
                  {cache?.hit_rate === null || cache?.hit_rate === undefined
                    ? "尚无请求"
                    : `${(cache.hit_rate * 100).toFixed(1)}%（命中 ${cache.hits ?? 0} / 未命中 ${cache.misses ?? 0}）`}
                </span>
              </div>
            </>
          )}
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
