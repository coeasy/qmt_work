import { useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  FormRow,
  Panel,
  Select,
  Spinner,
  type Column,
} from "@/design/primitives";
import { screenApi, fmtBarDate, type ScreenPick, type ScreenRunMeta } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { useWatchlistStore } from "@/stores/watchlist";
import { fmtDateTime, toTimestamp } from "@/shared/time";
import { fmtPct, fmtPrice, toneColor } from "@/shared/format";
import s from "../../domain.module.css";

/**
 * 自动选股（定时任务结果）面板。
 *
 * ★ 为什么需要这个页面：``system.classic_screen`` 每天收盘后自动跑选股，但结果此前
 * **只存在于作业返回的 JSON 里** —— 用户只能去「任务运行时」翻一个巨大的结果字段，
 * 翻不到就等于没有。于是「每天自动选股」这条链路事实上是跑给日志看的。
 *
 * ★ 三条必须显式呈现的**可信度元数据**（`screen_runs` 表）：
 *   1. ``scanned``：扫了多少只。**0 表示根本没拿到数据**（日线同步没跑成），
 *      与「扫了 5000 只但没选中」是两件完全不同的事；
 *   2. ``bar_date``：命中所依据的日线截至哪一天。**非空 ≠ 够新** —— 券商本地历史
 *      可能只到一年前却非空，不标注的话用户会以为这是今天的选股结果；
 *   3. ``degraded``：这次用的是降级数据源，结果可信度要跟着降。
 *
 * ★ ``run === null`` 时显示「尚未跑过选股」，**不是**空表格 ——
 *   空表格会被读成「今天没选出票」。
 */
export function AutoPicks() {
  const [runId, setRunId] = useState("");
  const [strategy, setStrategy] = useState("");
  const [expanded, setExpanded] = useState<ScreenPick | null>(null);

  const res = useAsync(() => screenApi.classicPicks({ runId, strategy, limit: 800 }), [runId, strategy]);
  const data = res.data;
  const run = data?.run ?? null;
  const picks = data?.picks ?? [];

  const codes = picks.map((p) => p.code);
  const quotes = useLiveQuotes(codes);
  const toggle = useWatchlistStore((st) => st.toggle);
  const watchCodes = useWatchlistStore((st) => st.codes);

  const cols: Column<ScreenPick>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 100, render: (r) => r.name || "--" },
    {
      key: "last",
      header: "最新",
      width: 84,
      align: "right",
      mono: true,
      // 实时行情优先；无行情时如实回退选股那一刻的收盘价（两者都不是 0）
      render: (r) => {
        const q = quotes[r.code]?.price;
        return fmtPrice(q !== undefined && q > 0 ? q : r.close ?? undefined);
      },
    },
    {
      key: "chg",
      header: "涨跌幅",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = quotes[r.code]?.change_pct ?? r.change_pct ?? undefined;
        return <span style={{ color: toneColor(pct) }}>{fmtPct(pct)}</span>;
      },
    },
    {
      key: "reason",
      header: "命中理由",
      render: (r) => <span title={r.reason}>{r.reason || "--"}</span>,
    },
    {
      key: "detail",
      header: "明细",
      width: 220,
      render: (r) => {
        const parts = Object.entries(r.detail ?? {})
          .filter(([, v]) => v !== null && v !== undefined)
          .slice(0, 3)
          .map(([k, v]) => `${k} ${typeof v === "number" ? v.toFixed(2) : String(v)}`);
        return <span className={s.muted}>{parts.join(" · ") || "--"}</span>;
      },
    },
    {
      key: "act",
      header: "操作",
      width: 130,
      render: (r) => (
        <div style={{ display: "flex", gap: 4 }}>
          <Button size="sm" variant="ghost" onClick={() => setExpanded(r)}>
            明细
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => toggle(r.code)}
            title={watchCodes.includes(r.code) ? "从自选股移除" : "加入自选股"}
          >
            {watchCodes.includes(r.code) ? "移出自选" : "加自选"}
          </Button>
        </div>
      ),
    },
  ];

  const runLabel = (r: ScreenRunMeta) =>
    `${fmtDateTime(toTimestamp(r.created_at))} · ${r.source === "schedule" ? "定时" : "手动"} · 命中 ${r.hits} · 扫描 ${r.scanned}`;

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <FormRow label="运行批次">
          <Select
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            options={[
              { value: "", label: "最近一次" },
              ...(data?.available_runs ?? []).map((r) => ({ value: r.run_id, label: runLabel(r) })),
            ]}
          />
        </FormRow>
        <FormRow label="策略">
          <Select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            options={[
              { value: "", label: "全部策略" },
              ...(run?.strategies ?? []).map((sid) => ({ value: sid, label: sid })),
            ]}
          />
        </FormRow>
        <Button size="sm" onClick={() => void res.reload()}>
          刷新
        </Button>
        <span className={s.spacer} />
        {run && <Badge tone={run.source === "schedule" ? "info" : "neutral"}>
          {run.source === "schedule" ? "定时任务" : "手动运行"}
        </Badge>}
      </div>

      {/* ---- 可信度元数据：命中 0 只时必须靠它区分「行情不好」与「没数据」 ---- */}
      {run && (
        <div className={`${s.note} ${run.scanned === 0 ? s.noteError : run.bar_date ? s.noteOk : s.noteWarn}`}>
          {run.scanned === 0 ? (
            <>
              <b>本次未取得任何 K 线（扫描 0 只）</b> —— 这不是「没选中票」，而是日线数据没到位。
              请先运行「定时更新日线」任务（或检查券商连接）后重跑选股。
            </>
          ) : (
            <>
              扫描 <span className={s.mono}>{run.scanned}</span> 只 · 命中{" "}
              <span className={s.mono}>{run.hits}</span> 只 · 数据截至{" "}
              <span className={s.mono}>{run.bar_date ? fmtBarDate(run.bar_date) : "未知"}</span>
              {run.bar_date && run.bar_date < new Date().toISOString().slice(0, 10).replace(/-/g, "") && (
                <span> —— 早于今天，行情未更新到最新交易日，结果仅供参考</span>
              )}
              {run.truncated && <span> · 命中过多，仅保留前 800 条</span>}
            </>
          )}
        </div>
      )}

      {run?.degraded && (
        <div className={`${s.note} ${s.noteWarn}`}>
          数据源已降级：{run.degraded_reason || "未给出原因"} —— 结果可信度随之下降。
        </div>
      )}

      <Panel
        flush
        title={`选股结果（${picks.length}）`}
        className={s.grow}
        extra={run ? <span className={s.muted}>{fmtDateTime(toTimestamp(run.created_at))}</span> : undefined}
      >
        <div className={s.tableArea}>
          {res.loading && !data ? (
            <Spinner label="读取选股结果…" />
          ) : res.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {res.error}
            </div>
          ) : !run ? (
            <EmptyState text="尚未跑过选股 —— 可到「任务运行时」立即运行 system.classic_screen，或手动跑一次经典策略" />
          ) : picks.length === 0 ? (
            <EmptyState
              text={
                run.scanned === 0
                  ? "未取得任何 K 线，本次选股没有可比对的行情数据"
                  : `扫描了 ${run.scanned} 只但无命中（行情形态不满足策略条件）`
              }
            />
          ) : (
            <DataTable columns={cols} rows={picks} rowKey={(r) => `${r.strategy}-${r.code}`} rowHeight={26} />
          )}
        </div>
      </Panel>

      {expanded && (
        <Panel
          title={`命中明细 · ${expanded.name || expanded.code}`}
          extra={
            <Button size="sm" variant="ghost" onClick={() => setExpanded(null)}>
              关闭
            </Button>
          }
        >
          <div className={s.kv}>
            <span className={s.kvKey}>策略</span>
            <span className={s.kvVal}>{expanded.strategy}</span>
            <span className={s.kvKey}>命中理由</span>
            <span className={s.kvVal}>{expanded.reason || "--"}</span>
            {Object.entries(expanded.detail ?? {}).map(([k, v]) => (
              <span key={k} style={{ display: "contents" }}>
                <span className={s.kvKey}>{k}</span>
                <span className={s.kvVal}>
                  {v === null || v === undefined
                    ? "—"
                    : typeof v === "number"
                      ? v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
                      : String(v)}
                </span>
              </span>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}

export default AutoPicks;
