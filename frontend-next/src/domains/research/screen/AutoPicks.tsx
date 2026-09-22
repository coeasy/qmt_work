import { useEffect, useState } from "react";
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
import { screenApi, systemApi, fmtBarDate, type ScreenPick, type ScreenRunMeta } from "@/services/api";
import type { PageProps } from "@/app/routes";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { useSessionStore } from "@/stores/session";
import { useWatchlistStore } from "@/stores/watchlist";
import { fmtDateTime, toTimestamp } from "@/shared/time";
import { fmtPct, fmtPrice, toneColor } from "@/shared/format";
import { isLivePrice } from "@/shared/freshness";
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
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
 *
 * ★ 新增「数据体检」：选股结果的可信度完全取决于跑的时候有没有日线数据。
 *   若 ``run.bar_date`` 落后于「今日（交易日）/ 最近交易日」，说明日线没同步到最新，
 *   此时翻结果没有意义 —— 页面直接给出「补充历史数据」按钮（提交
 *   ``system.sync_bars``），而不是让用户自己去猜该点哪里。
 */
export interface AutoPicksProps extends Partial<PageProps> {
  /** 嵌入「选股」工作台页签时为真：由父级承担内边距与滚动，避免双滚动条 */
  bare?: boolean;
}

/**
 * 选股依据的日线是否**落后于「本该有的那一天」**。
 *
 * ``expectDate`` 由后端 ``GET /market/session`` 给出（交易日 = 今天，
 * 非交易日 = 最近交易日），是唯一正确的参照口径。两个曾经踩过的坑：
 *
 * ① **不能拿「今天」当判据**：周末/节假日 ``bar_date``（周五）必然早于今天（周日），
 *    于是每逢休市就误报「行情未更新到最新交易日」—— 而数据其实是最新的。
 * ② **不能用 ``new Date().toISOString()``**：那是 **UTC** 的今天，UTC+8 的用户在
 *    早上 8 点前会拿到前一天，判据再错一天。
 *
 * 两处（页面级 ``lagging`` 与元数据行内提示）必须走同一个判据，否则同一页会出现
 * 两套「新鲜」标准。
 */
export function isBarDateLagging(barDate: string, expectDate: string): boolean {
  if (!barDate || !expectDate) return false;
  return barDate < expectDate;
}

export function AutoPicks({ bare = false }: AutoPicksProps = {}) {
  const [runId, setRunId] = useState("");
  const [strategy, setStrategy] = useState("");
  const [expanded, setExpanded] = useState<ScreenPick | null>(null);
  /** 说明卡默认展开一次；关掉后本次会话不再自动弹回来 */
  const [showIntro, setShowIntro] = useState(true);
  // 选股结果点一行 ⇒ 直接进行情工作台看这只票（唯一出口，勿各写一遍）
  const openWorkbench = useOpenWorkbench();

  const res = useAsync(() => screenApi.classicPicks({ runId, strategy, limit: 800 }), [runId, strategy]);
  const data = res.data;
  const run = data?.run ?? null;
  const picks = data?.picks ?? [];

  // ---- 数据体检：日线是否同步到「本该有的那一天」 ----
  const snapshot = useSessionStore((st) => st.snapshot);
  const refreshSnapshot = useSessionStore((st) => st.refreshSnapshot);
  useEffect(() => {
    void refreshSnapshot();
  }, [refreshSnapshot]);

  /** 期望的数据日：今天是交易日 → 今天；否则 → 最近交易日（周末/节假日） */
  const expectDate = snapshot
    ? snapshot.tradingDay
      ? snapshot.today
      : snapshot.lastTradingDay
    : "";
  const barDate = run?.bar_date ?? "";
  /** 落后 = 期望有数据那天之后就没再同步过（含从未同步 → barDate 为空） */
  const lagging = Boolean(expectDate) && Boolean(run) &&
    (!barDate || isBarDateLagging(barDate, expectDate));

  const [jobMsg, setJobMsg] = useState("");
  const [jobBusy, setJobBusy] = useState(false);
  const backfill = async () => {
    setJobBusy(true);
    setJobMsg("");
    try {
      const r = await systemApi.submitJob({
        kind: "system.sync_bars",
        name: "自动选股 · 补历史日线",
        params: { mode: "full", reason: "auto_picks_lagging" },
      });
      setJobMsg(
        `已提交日线补数据任务 ${r.id}。完成后回到本页点「刷新」，并到「选股 · 经典策略」重跑一次 —— 补数据只补 K 线，不会自动重算选股结果。`,
      );
    } catch (e) {
      setJobMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setJobBusy(false);
    }
  };

  const codes = picks.map((p) => p.code);
  const quotes = useLiveQuotes(codes);
  const toggle = useWatchlistStore((st) => st.toggle);
  const watchCodes = useWatchlistStore((st) => st.codes);

  const cols: Column<ScreenPick>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 100, render: (r) => r.name || "--" },
    {
      key: "last",
      // ★ 表头原写「最新」，但回退值是**选股那一刻的收盘价** —— 与 ScreenPanels
      //   同口径改成「最新/收盘」，别让收盘价冒充此刻的价。
      header: "最新/收盘",
      width: 84,
      align: "right",
      mono: true,
      // 实时行情优先；无行情时如实回退选股那一刻的收盘价（两者都不是 0）
      render: (r) => {
        const q = quotes[r.code]?.price;
        return isLivePrice(q) ? (
          fmtPrice(q)
        ) : (
          <span title="收盘价（选股就是按这根 K 线的收盘价算的，非实时）">
            {fmtPrice(r.close ?? undefined)}
          </span>
        );
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
    <div className={bare ? s.panelHost : s.page}>
      {/* ---- 这个页面是什么 ----
          没有这段说明时，用户看到的是「一堆表 + 一堆数字」，既不知道数据从哪来、
          也不知道多久更新一次、更不知道命中 0 只是什么意思。 */}
      {showIntro && (
        <div className={`${s.note} ${s.noteOk} ${s.intro}`}>
          <div className={s.introHead}>
            <b>关于自动选股</b>
            <Button size="sm" variant="ghost" onClick={() => setShowIntro(false)}>
              收起
            </Button>
          </div>
          <div className={s.introList}>
            <div>
              · 这里展示的是<b>定时任务</b> <span className={s.mono}>system.classic_screen</span>
              （默认每天 16:15）跑出来的结果；每次运行留一个批次，用上方「运行批次」可回看历史。
            </div>
            <div>
              · 结果落库在 <span className={s.mono}>screen_runs</span> /{" "}
              <span className={s.mono}>screen_picks</span> 两张表，<b>只记录成功的批次</b> ——
              跑失败的批次在这里看不到，要去「自动化 · 任务运行时」看失败原因。
            </div>
            <div>
              · 想立刻跑一次：切到「经典策略」页签手动运行，或到「自动化 · 任务运行时」立即执行该任务。
            </div>
            <div>
              · <b>命中 0 只 ≠ 行情不好</b>：先看下面的「扫描 N 只」。<b>扫描 0 只</b>说明日线数据没到位，
              与「扫了 5000 只但没选中」完全是两回事，处理方式也不同。
            </div>
          </div>
        </div>
      )}

      {/* ---- 数据体检：日线落后时，翻结果没有意义 ---- */}
      {lagging && (
        <div className={`${s.note} ${s.noteWarn}`}>
          <div className={s.introHead}>
            <span>
              {barDate
                ? `日线数据截至 ${fmtBarDate(barDate)}，落后于${snapshot?.tradingDay ? "今日" : "最近交易日"} ${fmtBarDate(expectDate)}`
                : "本次运行没有取到任何日线数据"}
              ：此时看到的是旧数据（甚至可能是空扫描），建议先补齐历史再重跑。
            </span>
            <Button size="sm" onClick={() => void backfill()} disabled={jobBusy}>
              {jobBusy ? "提交中…" : "补充历史数据"}
            </Button>
          </div>
          {jobMsg && <div className={s.introList}>{jobMsg}</div>}
        </div>
      )}

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
              {/* ★ 判据必须是「最近交易日」（expectDate），不能是「今天」——
                  详见 isBarDateLagging 的注释（周末误报 + UTC 差一天）。 */}
              {isBarDateLagging(run.bar_date, expectDate) && (
                <span> —— 早于最近交易日，行情未更新到最新交易日，结果仅供参考</span>
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
            // ★ 空态要给出**下一步动作**：只说「尚未跑过」用户还得自己猜去哪跑。
            <EmptyState
              text="尚未跑过选股 —— 定时任务 system.classic_screen 默认每天 16:15 自动运行；现在可以先补日线数据，再到「经典策略」页签手动跑一次。"
              actionText={jobBusy ? "提交中…" : "先补日线数据"}
              onAction={() => void backfill()}
            />
          ) : picks.length === 0 ? (
            <EmptyState
              text={
                run.scanned === 0
                  ? "未取得任何 K 线，本次选股没有可比对的行情数据"
                  : `扫描了 ${run.scanned} 只但无命中（行情形态不满足策略条件）`
              }
            />
          ) : (
            <DataTable
              columns={cols}
              rows={picks}
              rowKey={(r) => `${r.strategy}-${r.code}`}
              rowHeight={26}
              onRowClick={(r) => openWorkbench(r.code, r.name ?? "")}
            />
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
