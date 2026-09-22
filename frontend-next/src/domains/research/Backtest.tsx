import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmButton,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  type Column,
} from "@/design/primitives";
import { backtestApi, type BacktestJob, type BacktestMetrics, type BacktestResult } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "@/charts/echartsSetup";
import s from "../domain.module.css";

/**
 * 回测（P1 新建页）：把**已经存在但界面不可达**的回测能力接上界面。
 *
 * ## 为什么必须补这个页面
 *
 * 后端 `/backtest/jobs` 的提交 / 列表 / 查询 / 取消 / 批量取消 + 参数扫描**早就齐了**，
 * 但 `src/` 里零调用、`routes.tsx` 里无页面 —— 于是「回测跑完没有、结果好不好」
 * 只能靠 curl 或翻库。这与通知渠道是同一类缺陷：**能力存在但不可达**。
 *
 * ## 契约要点（照抄 `app/routes/backtest.py` + `backtest/__init__.py`）
 *
 * - `POST /backtest/jobs` 的 `kind` 白名单是 `backtest/compare/sensitivity/sweep`，
 *   写错后端**直接 400** ⇒ 这里用 Select 枚举，把错误消灭在输入层；
 * - 单个作业的参数在 `params`（内存作业）或 `params_json`（DB 行）里，
 *   结果在 `result` / `result_json` —— **两种形状都要吃下**，否则列表里
 *   一半作业显示「无结果」而实际跑完了；
 * - `DELETE /backtest/jobs/{id}` 语义是**取消**，且只对 `pending/running` 有效：
 *   对已结束的作业返回 `cancelled:false` ⇒ 界面不能一律提示「已取消」；
 * - 批量删除 body 键是 `ids`（与 alerts / webhooks / notifications 一致）；
 * - 指标来自 `tools/metrics.py::compute_metrics`：样本不足时它**主动**给出
 *   `tail_metrics_note` 说明 VaR/CVaR 未输出 —— 这是诚信标注，必须照显示，
 *   否则用户会把「没有这个字段」读成「风险为零」。
 */

const KINDS = [
  { value: "backtest", label: "单标的回测" },
  { value: "sweep", label: "参数网格扫描" },
];

/** 后端 tools/backtest.py 支持的策略（写死在 signals_for 里）。 */
const STRATEGIES = [
  { value: "ma_cross", label: "双均线（ma_cross）" },
  { value: "macd", label: "MACD" },
  { value: "rsi", label: "RSI" },
];

const STATUS_TONE: Record<string, "neutral" | "success" | "warning" | "danger" | "info"> = {
  pending: "neutral",
  running: "info",
  done: "success",
  failed: "danger",
  cancelled: "warning",
};
const STATUS_LABEL: Record<string, string> = {
  pending: "排队中", running: "运行中", done: "已完成",
  failed: "失败", cancelled: "已取消",
};

/** 字段可能是对象，也可能是 JSON 字符串（DB 行口径） ⇒ 统一解成对象。 */
function asObject(v: unknown): Record<string, unknown> {
  if (v && typeof v === "object") return v as Record<string, unknown>;
  if (typeof v === "string" && v.trim()) {
    try {
      const parsed = JSON.parse(v);
      return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }
  return {};
}

function jobResult(j: BacktestJob): BacktestResult | null {
  const r = asObject(j.result ?? j.result_json);
  return Object.keys(r).length ? (r as BacktestResult) : null;
}

function jobParams(j: BacktestJob): Record<string, unknown> {
  return asObject(j.params ?? j.params_json);
}

/** 成交明细列（trades 是宽松字典 ⇒ 显式给泛型，否则 render 的 row 会被推成 unknown） */
const tradeCols: Column<Record<string, unknown>>[] = [
  { key: "i", header: "#", width: 56, mono: true, render: (_r, i) => String(i + 1) },
  { key: "side", header: "方向", width: 80,
    render: (r) => String(r.side ?? r.direction ?? "—") },
  { key: "price", header: "价格", width: 100, mono: true,
    render: (r) => num(Number(r.price ?? 0), 2) },
  { key: "shares", header: "数量", width: 90, mono: true,
    render: (r) => String(r.shares ?? r.qty ?? "—") },
  { key: "pnl", header: "盈亏", width: 100, mono: true,
    render: (r) => num(Number(r.pnl ?? 0), 2) },
];

const pct = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
const num = (v: number | null | undefined, digits = 3) =>
  v === null || v === undefined ? "—" : String(Number(v).toFixed(digits));

export function Backtest() {
  const list = useAsync<BacktestJob[]>(() => backtestApi.jobs(), []);
  const [selectedId, setSelectedId] = useState<string>("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);

  /* ---------- 提交表单 ---------- */
  const [kind, setKind] = useState("backtest");
  const [symbol, setSymbol] = useState("600519.SH");
  const [strategy, setStrategy] = useState("ma_cross");
  const [fast, setFast] = useState("5");
  const [slow, setSlow] = useState("20");
  const [capital, setCapital] = useState("100000");
  const [count, setCount] = useState("250");
  const [grid, setGrid] = useState("3,5,10,20");

  const rows = list.data ?? [];
  const active = rows.filter((r) => r.status === "pending" || r.status === "running");

  // ★ 有未结束的作业就轮询：否则用户只能手动刷新，而「跑完没」正是他想知道的
  useEffect(() => {
    if (!active.length) return;
    const t = window.setInterval(() => void list.reload(), 2000);
    return () => window.clearInterval(t);
  }, [active.length, list]);

  const submit = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const params: Record<string, unknown> = {
        symbol: symbol.trim(), strategy,
        initial_capital: Number(capital) || 100000,
        count: Number(count) || 250,
      };
      if (kind === "sweep") {
        const values = grid.split(",").map((x) => Number(x.trim())).filter((n) => Number.isFinite(n));
        if (!values.length) {
          setBanner({ tone: "error", text: "参数网格为空：至少填一个数值" });
          return;
        }
        params.param_grid = { fast: values };
      } else {
        params.params = { fast: Number(fast) || 5, slow: Number(slow) || 20 };
      }
      const job = await backtestApi.submit({ kind, params });
      setBanner({ tone: "ok", text: `已提交 #${job.id}` });
      setSelectedId(job.id);
      await list.reload();
    } catch (e) {
      setBanner({ tone: "error", text: `提交失败：${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const cancelOne = async (id: string) => {
    setBusy(true);
    try {
      const r = await backtestApi.cancel(id);
      // ★ 后端对已结束的作业返回 cancelled:false —— 一律提示「已取消」就是假成功
      setBanner(r?.cancelled
        ? { tone: "ok", text: `已取消 #${id}` }
        : { tone: "warn", text: `#${id} 已结束，无法取消（取消只对排队/运行中的作业有效）` });
      await list.reload();
    } catch (e) {
      setBanner({ tone: "error", text: `取消失败：${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const cancelMany = async () => {
    if (!selected.length) return;
    setBusy(true);
    try {
      const r = await backtestApi.batchDelete(selected);
      setBanner({ tone: "ok", text: `已取消 ${r.deleted} 个作业` });
      setSelected([]);
      await list.reload();
    } catch (e) {
      setBanner({ tone: "error", text: `批量取消失败：${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const cols: Column<BacktestJob>[] = [
    {
      key: "sel", header: "", width: 44,
      render: (r) => (
        <input
          type="checkbox"
          aria-label={`选择 ${r.id}`}
          checked={selected.includes(r.id)}
          onChange={() => toggle(r.id)}
        />
      ),
    },
    { key: "id", header: "ID", width: 110, mono: true, render: (r) => String(r.id) },
    {
      key: "kind", header: "类型", width: 90, mono: true,
      render: (r) => String(r.kind ?? "—"),
    },
    {
      key: "status", header: "状态", width: 88,
      render: (r) => (
        <Badge tone={STATUS_TONE[String(r.status ?? "")] ?? "neutral"}>
          {STATUS_LABEL[String(r.status ?? "")] ?? String(r.status ?? "—")}
        </Badge>
      ),
    },
    {
      key: "progress", header: "进度", width: 80, mono: true,
      render: (r) => `${Number(r.progress ?? 0)}%`,
    },
    {
      key: "sym", header: "标的", width: 110, mono: true,
      render: (r) => String(jobParams(r).symbol ?? "—"),
    },
    {
      key: "created", header: "创建", width: 150, mono: true,
      render: (r) => String(r.created_at ?? "—").replace("T", " ").slice(0, 19),
    },
    {
      key: "act", header: "操作", width: 190,
      render: (r) => (
        <div className={s.actions}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setSelectedId(r.id)}>
            结果
          </Button>
          <ConfirmButton disabled={busy} onConfirm={() => void cancelOne(r.id)}>
            取消
          </ConfirmButton>
        </div>
      ),
    },
  ];

  const cur = rows.find((r) => r.id === selectedId) ?? null;
  const result = cur ? jobResult(cur) : null;
  const metrics: BacktestMetrics | null = result?.metrics ?? null;

  const equityOption = useMemo<EChartsOption>(() => {
    const eq = Array.isArray(result?.equity) ? (result?.equity as number[]) : [];
    return {
      grid: { left: 48, right: 16, top: 16, bottom: 28 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: eq.map((_, i) => String(i)), show: false },
      yAxis: { type: "value", scale: true },
      series: [{ type: "line", data: eq, showSymbol: false, smooth: true }],
    };
  }, [result]);

  return (
    <div className={s.page}>
      {banner ? (
        <div className={
          banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
        }>
          {banner.text}
        </div>
      ) : null}

      <Panel title="提交回测">
        <div className={s.form}>
          <FormRow label="类型">
            <Select options={KINDS} value={kind} onChange={(e) => setKind(e.target.value)} />
          </FormRow>
          <FormRow label="标的">
            <Input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="600519.SH" />
          </FormRow>
          <FormRow label="策略">
            <Select options={STRATEGIES} value={strategy} onChange={(e) => setStrategy(e.target.value)} />
          </FormRow>
          {kind === "sweep" ? (
            <FormRow label="参数网格（fast，逗号分隔）">
              <Input value={grid} onChange={(e) => setGrid(e.target.value)} />
            </FormRow>
          ) : (
            <>
              <FormRow label="快线">
                <Input value={fast} onChange={(e) => setFast(e.target.value)} />
              </FormRow>
              <FormRow label="慢线">
                <Input value={slow} onChange={(e) => setSlow(e.target.value)} />
              </FormRow>
            </>
          )}
          <FormRow label="初始资金">
            <Input value={capital} onChange={(e) => setCapital(e.target.value)} />
          </FormRow>
          <FormRow label="K 线根数">
            <Input value={count} onChange={(e) => setCount(e.target.value)} />
          </FormRow>
          <div className={s.actions}>
            <Button variant="primary" size="sm" disabled={busy} onClick={() => void submit()}>
              提交
            </Button>
            {/* ★ 回测依赖券商真实历史 K 线；没连券商时提交必失败，这句提示能省一次排错 */}
            <span className={s.muted}>未连接券商时回测会失败（K 线取不到）</span>
          </div>
        </div>
      </Panel>

      <Panel
        title="回测作业"
        extra={
          <div className={s.actions}>
            {active.length ? <Badge tone="info">{active.length} 个进行中</Badge> : null}
            <span className={s.muted}>已选 {selected.length} 项</span>
            <Button size="sm" variant="ghost" onClick={() => void list.reload()}>刷新</Button>
            <ConfirmButton disabled={busy || !selected.length} onConfirm={() => void cancelMany()}>
              批量取消
            </ConfirmButton>
          </div>
        }
      >
        {list.error ? (
          <EmptyState text={`回测作业加载失败：${list.error}`} />
        ) : rows.length === 0 ? (
          <EmptyState text="还没有回测作业。回测基于券商真实历史 K 线，需先连接券商。" />
        ) : (
          <div className={s.tableArea}>
            <DataTable columns={cols} rows={rows} rowKey={(r) => String(r.id)} />
          </div>
        )}
      </Panel>

      <Panel title={cur ? `结果 #${cur.id}` : "结果"}>
        {!cur ? (
          <EmptyState text="在上方点「结果」查看某次回测的绩效与 equity 曲线" />
        ) : cur.status === "failed" ? (
          <EmptyState text={`回测失败：${String(cur.error ?? "未知原因")}`} />
        ) : !result ? (
          <EmptyState text={
            cur.status === "done"
              ? "作业已完成但没有结果数据（可能被后端清理）"
              : "作业尚未完成（排队中 / 运行中）"
          } />
        ) : (
          <div className={s.page}>
            {/* ★ 诚信标注：样本不足时后端主动声明 VaR/CVaR 未输出，必须照显示 */}
            {metrics?.tail_metrics_note ? (
              <div className={s.noteWarn}>{String(metrics.tail_metrics_note)}</div>
            ) : null}
            <div className={`${s.statRow} ${s.statRowDense}`}>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>总收益</span>
                <span className={s.statRowValue}>{pct(metrics?.total_return)}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>年化</span>
                <span className={s.statRowValue}>{pct(metrics?.annual_return)}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>夏普</span>
                <span className={s.statRowValue}>{num(metrics?.sharpe)}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>最大回撤</span>
                <span className={s.statRowValue}>{pct(metrics?.max_drawdown)}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>卡玛</span>
                <span className={s.statRowValue}>{num(metrics?.calmar ?? null)}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>胜率</span>
                <span className={s.statRowValue}>{pct(metrics?.win_rate, 1)}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>交易数</span>
                <span className={s.statRowValue}>{String(metrics?.trade_count ?? "—")}</span>
              </div>
              <div className={s.statRowItem}>
                <span className={s.statRowLabel}>评级</span>
                <span className={s.statRowValue}>{String(metrics?.rating ?? "—")}</span>
              </div>
            </div>

            {Array.isArray(result.equity) && result.equity.length > 1 ? (
              <div style={{ height: 220 }}>
                <EChart option={equityOption} />
              </div>
            ) : null}

            {Array.isArray(result.trades) && result.trades.length ? (
              <div className={s.tableArea} style={{ maxHeight: 240 }}>
                <DataTable<Record<string, unknown>>
                  rowKey={(_r, i) => String(i)}
                  rows={result.trades}
                  columns={tradeCols}
                />
              </div>
            ) : null}
          </div>
        )}
      </Panel>
    </div>
  );
}

export default Backtest;
