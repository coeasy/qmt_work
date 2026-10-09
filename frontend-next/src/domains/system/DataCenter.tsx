import { useCallback, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  Input,
  Panel,
} from "@/design/primitives";
import { ConfirmModal } from "@/design/primitives/Modal";
import {
  datasetsApi,
  type DatasetItem,
  type DatasetLocalStatus,
} from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import s from "../domain.module.css";

/**
 * 数据中心 —— 多数据类型下载与本地查询（R28）。
 *
 * 设计要点
 * --------
 * 1. **清单来自后端 SSOT**：前端不内置数据集列表。后端
 *    `datasource/datasets.py` 加一行，这里自动多一张卡片。
 * 2. **源状态必须显式**：「QMT 开着走券商、没开降级第三方」是后端能力链的
 *    行为，但对用户是黑盒。顶部常驻显示券商是否可用 + 每个数据集的**实际**
 *    生效源，用户才知道「我点了同步，数据从哪来」。
 * 3. **同步成功但 0 行 = 失败**：后端对「源返回空」返回 `code != 0`，
 *    前端只看 HTTP 200 会把「数据一天没更新」显示成「已完成」。这里一律
 *    看 `code`，失败弹红色 banner + 展示 problems。
 * 4. **本地查询是下载价值的证明**：同步完能立刻查到数据，才算闭环。
 * 5. **默认只跑 50 只**：全市场同步走定时调度，HTTP 请求不挂死。
 */
export default function DataCenter() {
  const list = useAsync(() => datasetsApi.list(), []);
  const sources = useAsync(() => datasetsApi.sources(), []);
  const [busyId, setBusyId] = useState("");
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{
    title: string; message: string; warn?: string; run: () => Promise<void>;
  } | null>(null);
  const [preview, setPreview] = useState<{
    id: string; label: string; count: number;
    rows: Record<string, unknown>[];
    local?: DatasetLocalStatus | null;
    last_sync_at?: string;
  } | null>(null);
  const [previewCode, setPreviewCode] = useState("");

  const runSync = useCallback(
    async (item: DatasetItem) => {
      setBusyId(item.id);
      setBanner(null);
      try {
        const res = await datasetsApi.sync(item.id, { limit: 50, dry_run: false });
        setBanner({
          tone: res.ok ? "ok" : "error",
          text: res.ok
            ? `${item.label}：写入 ${res.written} 条（成功 ${res.requested - res.failed}/${res.requested}，耗时 ${res.duration_s}s）`
            : `${item.label} 同步未完成：${res.problems.join("; ") || "未知原因"}`,
        });
        list.reload();
      } catch (e: any) {
        setBanner({ tone: "error", text: `${item.label} 同步失败：${e.message}` });
      } finally {
        setBusyId("");
      }
    },
    [list],
  );

  const runPreview = useCallback(async (item: DatasetItem) => {
    setBusyId(item.id);
    setBanner(null);
    try {
      // ★ 同时取详情：表格里的行数是**列表加载那一刻**的快照，同步/清理之后
      //   会过期。预览面板要显示「此刻库里到底有多少」，所以走一次 detail，
      //   而不是拿列表里的旧数字冒充实时值。
      const [res, det] = await Promise.all([
        datasetsApi.data(item.id, { code: previewCode || undefined, limit: 20 }),
        datasetsApi.detail(item.id).catch(() => null),
      ]);
      setPreview({
        id: item.id, label: item.label,
        count: res.count, rows: res.rows || [],
        local: det?.local ?? null,
        last_sync_at: det?.last_sync_at || "",
      });
    } catch (e: any) {
      setBanner({ tone: "error", text: `读取失败：${e.message}` });
    } finally {
      setBusyId("");
    }
  }, [previewCode]);

  const srcData = sources.data;
  const heavy = (i: DatasetItem) => i.tags.includes("heavy");

  /** 游标语义 → 人话（决定「增量同步」到底是什么含义）。 */
  const cursorLabel: Record<string, string> = {
    bar_date: "按 K 线日期",
    snapshot: "整体快照",
    report_period: "按报告期",
    event_date: "按交易日",
  };

  /** 数据集说明行：单位口径 / 复权 / 周期。
   *  单位口径是最容易踩的坑（TDX 逐笔 vol=手、K 线 vol=股），必须常驻可见。 */
  const specMeta = (i: DatasetItem) => {
    const bits: string[] = [];
    if (i.period) bits.push(i.period);
    if (i.adjust) bits.push(`复权:${i.adjust}`);
    if (i.cursor) bits.push(cursorLabel[i.cursor] || i.cursor);
    if (i.cursor === "bar_date" && !i.supports_range) {
      bits.push(`增量回看 ${i.lookback} 根`);
    }
    return bits.join(" · ");
  };

  /** declared vs resolved 链的差异说明（后端 docstring 明确强调要一起展示）。 */
  const chainMeta = (i: DatasetItem) => {
    const c = srcData?.per_dataset?.[i.id];
    if (!c) return null;
    const declared = c.declared.join(",");
    const resolved = c.resolved.join(",");
    if (declared === resolved) return null;
    return `声明：${declared} → 实际：${resolved}`;
  };

  return (
    <div className={s.page}>
      <Panel
        title="数据源状态"
        extra={
          srcData ? (
            <Badge tone={srcData.broker_available ? "success" : "neutral"}>
              {srcData.broker_available ? "QMT 已连接（券商优先）" : "未连券商（走第三方）"}
            </Badge>
          ) : null
        }
      >
        {sources.error ? (
          <EmptyState text={`源状态加载失败：${sources.error}`} />
        ) : !srcData ? (
          <span className={s.muted}>加载中…</span>
        ) : (
          <div className={s.muted} style={{ lineHeight: 1.6 }}>
            {srcData.broker_note}
            <br />
            声明链是「想用谁」（券商优先），实际链是「此刻真能用到谁」——
            券商未连接或依赖未安装时会自动降级。
            {srcData.providers?.length > 0 && (
              <div style={{ marginTop: 6, fontSize: "0.85em" }}>
                {srcData.providers.map((p) => (
                  <span
                    key={p.provider}
                    title={`依赖:${p.dependency_available ? "已安装" : "未安装"}｜商用:${p.commercial_ok ? "可用" : "受限"}｜${p.capabilities.join(",")}`}
                    style={{
                      display: "inline-block",
                      margin: "2px 4px 0 0",
                      padding: "1px 6px",
                      borderRadius: 3,
                      background: p.active
                        ? "var(--success-dim)" : "var(--bg-2)",
                      color: p.active ? "var(--success)" : "var(--text-dim)",
                    }}
                  >
                    {p.name || p.provider}{p.active ? "" : "（不可用）"}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </Panel>

      {banner && (
        <div style={{
          padding: "6px 10px", borderRadius: 4,
          background: banner.tone === "ok"
            ? "var(--success-dim)" : "var(--danger-dim)",
          color: banner.tone === "ok"
            ? "var(--success)" : "var(--danger)",
        }}>
          {banner.text}
        </div>
      )}

      {list.error ? (
        <EmptyState text={`数据集清单加载失败：${list.error}`} />
      ) : !list.data ? (
        <span className={s.muted}>加载中…</span>
      ) : (
        list.data.categories.map((cat) => (
          <Panel key={cat.id} title={cat.label} extra={<Badge tone="neutral">{cat.items.length}</Badge>}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9em" }}>
              <thead>
                <tr style={{ textAlign: "left" }}>
                  <th style={{ padding: "4px 6px" }}>数据集</th>
                  <th style={{ padding: "4px 6px" }}>本地</th>
                  <th style={{ padding: "4px 6px" }}>生效源</th>
                  <th style={{ padding: "4px 6px" }}>调度</th>
                  <th style={{ padding: "4px 6px" }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {cat.items.map((it) => {
                  const chain = srcData?.per_dataset?.[it.id];
                  const eff = chain?.effective_first || "";
                  const builtin = chain?.builtin_fallback || "";
                  const effLabel = eff || builtin;
                  return (
                    <tr key={it.id} style={{ borderTop: "1px solid var(--border)" }}>
                      <td style={{ padding: "4px 6px" }}>
                        <strong>{it.label}</strong>
                        <div className={s.muted} style={{ fontSize: "0.85em" }}>
                          {it.id}
                          {specMeta(it) && ` · ${specMeta(it)}`}
                        </div>
                        {it.unit_note && (
                          <div
                            className={s.muted}
                            style={{ fontSize: "0.82em", color: "var(--warning)" }}
                          >
                            口径：{it.unit_note}
                          </div>
                        )}
                        {heavy(it) && <Badge tone="warning">大数据量</Badge>}
                      </td>
                      <td style={{ padding: "4px 6px" }}>
                        {it.local.rows > 0 ? (
                          <span>
                            {it.local.rows.toLocaleString()} 行 / {it.local.codes} 只
                            {it.local.last_dt && (
                              <div className={s.muted} style={{ fontSize: "0.85em" }}>
                                截至 {it.local.last_dt}
                              </div>
                            )}
                          </span>
                        ) : (
                          <Badge tone="neutral">无数据</Badge>
                        )}
                      </td>
                      <td style={{ padding: "4px 6px" }}>
                        {effLabel ? (
                          <Badge
                            tone={effLabel === "broker" ? "success"
                              : builtin ? "neutral" : "neutral"}
                          >
                            {effLabel}
                          </Badge>
                        ) : (
                          <Badge tone="danger">无可用源</Badge>
                        )}
                        {builtin && (
                          <div className={s.muted} style={{ fontSize: "0.8em", marginTop: 2 }}>
                            {chain?.builtin_note || "内置实现，无需外部源"}
                          </div>
                        )}
                        {chainMeta(it) && (
                          <div className={s.muted} style={{ fontSize: "0.8em", marginTop: 2 }}>
                            {chainMeta(it)}
                          </div>
                        )}
                        {!it.supports_range && (
                          <div className={s.muted} style={{ fontSize: "0.8em" }}>
                            源不支持区间拉取
                          </div>
                        )}
                      </td>
                      <td style={{ padding: "4px 6px" }}>
                        <code style={{ fontSize: "0.85em" }}>{it.cron}</code>
                        <div className={s.muted} style={{ fontSize: "0.85em" }}>
                          {it.default_enabled ? "默认开启" : "默认关闭"}
                          {it.retention_days > 0 ? ` · 保留 ${it.retention_days} 天` : " · 永久"}
                          {it.cursor === "snapshot" && " · 整批刷新"}
                          {it.cursor === "bar_date" && it.supports_range && " · 可增量续传"}
                        </div>
                      </td>
                      <td style={{ padding: "4px 6px", whiteSpace: "nowrap" }}>
                        <Button
                          variant="ghost"
                          disabled={busyId === it.id}
                          onClick={() =>
                            setConfirm({
                              title: `同步 ${it.label}`,
                              message: `将同步前 50 只标的（全市场请到「定时任务」建调度）。\n生效源：${effLabel || "无"}`,
                              warn: heavy(it)
                                ? "该数据集数据量很大，同步可能耗时较长。"
                                : undefined,
                              run: () => runSync(it),
                            })
                          }
                        >
                          {busyId === it.id ? "同步中…" : "同步"}
                        </Button>
                        <Button
                          variant="ghost"
                          disabled={busyId === it.id}
                          onClick={() => runPreview(it)}
                        >
                          查看
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>
        ))
      )}

      {preview && (
        <Panel
          title={`本地数据预览 · ${preview.label}`}
          extra={<Badge tone="neutral">{preview.count} 条</Badge>}
        >
          {preview.local && (
            <div className={s.muted} style={{ fontSize: "0.88em", marginBottom: 6 }}>
              库内实时统计：{preview.local.rows.toLocaleString()} 行 / {preview.local.codes} 只
              {preview.local.first_dt && <> · 覆盖 {preview.local.first_dt} ~ {preview.local.last_dt}</>}
              {preview.last_sync_at && <> · 上次同步 {preview.last_sync_at}</>}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
            <Input
              placeholder="标的代码（K 线/逐笔类必填，如 600000.SH）"
              value={previewCode}
              onChange={(e) => setPreviewCode(e.target.value)}
            />
          </div>
          {preview.rows.length === 0 ? (
            <EmptyState text="本地暂无数据（或该数据集需要指定标的代码）" />
          ) : (
            <pre style={{
              maxHeight: 320, overflow: "auto", fontSize: "0.82em",
              background: "var(--bg-2)", padding: 8, borderRadius: 4,
            }}>
              {JSON.stringify(preview.rows.slice(0, 20), null, 2)}
            </pre>
          )}
        </Panel>
      )}

      {confirm && (
        <ConfirmModal
          open
          title={confirm.title}
          message={confirm.message}
          warn={confirm.warn}
          onConfirm={confirm.run}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
