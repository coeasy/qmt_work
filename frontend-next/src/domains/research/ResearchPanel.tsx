import { useState } from "react";
import { Badge, Button, FormRow, Input, Panel, Select, Tabs } from "@/design/primitives";
import { researchApi } from "@/services/api";
import { ConnSelect } from "./ConnSelect";
import s from "../domain.module.css";

/**
 * 研究深度层（阶段 3）：因子 IC/ICIR · 分位分组 · 相关性矩阵 · 组合回测 · walk-forward · 绩效归因。
 * 全部基于真实券商 K 线 / 真实成交，无假数据；降级路径由后端 source 标注。
 * 移植自旧 frontend/features/research/Research.jsx。
 */

type TabKey = "ic" | "quantile" | "corr" | "portfolio" | "wf" | "attr";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "ic", label: "因子 IC" },
  { key: "quantile", label: "分位分组" },
  { key: "corr", label: "相关性矩阵" },
  { key: "portfolio", label: "组合回测" },
  { key: "wf", label: "walk-forward" },
  { key: "attr", label: "绩效归因" },
];

const FACTORS = [
  "sma", "ema", "rsi", "macd", "bollinger", "atr", "adx", "cci",
  "kdj", "obv", "volume_ma", "returns", "log_returns", "zscore", "roc",
];
const STRATS = ["ma_cross", "macd", "rsi"];
const STRAT_PRESETS: Record<string, string> = {
  ma_cross: JSON.stringify({ fast: 5, slow: 20 }),
  macd: JSON.stringify({ fast: 12, slow: 26, signal: 9 }),
  rsi: JSON.stringify({ period: 14, buy: 30, sell: 70 }),
};

function kv(k: string, v: unknown, hint?: string) {
  return (
    <div className={s.kv}>
      <span className={s.kvKey}>{k}</span>
      <span className={s.kvVal}>
        {v === undefined || v === null ? "N/A" : String(v)}
        {hint && <small className={s.muted}> {hint}</small>}
      </span>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <FormRow label={label}>
      <Input mono value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} {...(type !== "text" ? { type } : {})} />
    </FormRow>
  );
}

export function ResearchPanel() {
  const [tab, setTab] = useState<TabKey>("ic");
  const [connId, setConnId] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; t: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<unknown>(null);

  async function go(fn: () => Promise<unknown>) {
    setLoading(true);
    setMsg(null);
    setResult(null);
    try {
      setResult(await fn());
    } catch (e) {
      setMsg({ ok: false, t: e instanceof Error ? e.message : String(e) });
    } finally {
      setLoading(false);
    }
  }

  const shared: SharedProps = { connId, setConnId, loading, msg, result, go };

  return (
    <div className={s.page}>
      <Panel title="研究深度" padded>
        <p className={s.muted} style={{ marginTop: 0 }}>
          阶段 3 · 因子 IC/ICIR · 分位分组 · 相关性矩阵 · 组合回测 · walk-forward · 绩效归因（基于真实 K 线 / 真实成交）。
        </p>
        <div style={{ marginBottom: 16 }}>
          <Tabs
            items={TABS.map((t) => ({ key: t.key, label: t.label }))}
            value={tab}
            onChange={(k) => setTab(k as TabKey)}
          />
        </div>

        {tab === "ic" && <ICTab {...shared} />}
        {tab === "quantile" && <QuantileTab {...shared} />}
        {tab === "corr" && <CorrTab {...shared} />}
        {tab === "portfolio" && <PortfolioTab {...shared} />}
        {tab === "wf" && <WalkForwardTab {...shared} />}
        {tab === "attr" && <AttributionTab {...shared} />}
      </Panel>
    </div>
  );
}

type SharedProps = {
  connId: string;
  setConnId: (v: string) => void;
  loading: boolean;
  msg: { ok: boolean; t: string } | null;
  result: unknown;
  go: (fn: () => Promise<unknown>) => void;
};

function ConnRow({ connId, onChange }: { connId: string; onChange: (v: string) => void }) {
  return (
    <FormRow label="活跃连接">
      <ConnSelect value={connId} onChange={onChange} />
    </FormRow>
  );
}

/* ---------- 因子 IC ---------- */
function ICTab({ connId, setConnId, loading, msg, result, go }: SharedProps) {
  const [symbol, setSymbol] = useState("600519.SH");
  const [factor, setFactor] = useState("rsi");
  const [mode, setMode] = useState("series");
  const [method, setMethod] = useState("pearson");
  const [forward, setForward] = useState("1");
  const [count, setCount] = useState("250");

  const r = result as import("@/services/api").IcResponse | null;
  const icSeries = r?.ic_series ?? [];

  return (
    <>
      <div className={s.form}>
        <Field label="标的(面板模式逗号分隔多标的)" value={symbol} onChange={setSymbol} placeholder="600519.SH" />
        <FormRow label="因子">
          <Select value={factor} onChange={(e) => setFactor(e.target.value)} options={FACTORS.map((f) => ({ value: f, label: f }))} />
        </FormRow>
        <FormRow label="模式">
          <Select value={mode} onChange={(e) => setMode(e.target.value)} options={[{ value: "series", label: "单序列" }, { value: "panel", label: "截面 panel" }]} />
        </FormRow>
        <FormRow label="相关">
          <Select value={method} onChange={(e) => setMethod(e.target.value)} options={[{ value: "pearson", label: "Pearson" }, { value: "spearman", label: "Spearman" }]} />
        </FormRow>
        <Field label="远期(期)" value={forward} onChange={setForward} type="number" />
        <Field label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnRow connId={connId} onChange={setConnId} />
      </div>
      <div className={s.actions}>
        <Button variant="primary" disabled={loading} onClick={() => go(() => researchApi.factorIc({ symbol, factor_name: factor, mode, method, forward: Number(forward) || 1, count: Number(count) || 250, broker_id: connId || undefined }))}>
          {loading ? "分析中…" : "分析 IC"}
        </Button>
      </div>
      {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
      {r && (
        <div style={{ marginTop: 16 }}>
          <div className={s.toolbar}>
            <Badge tone="info">来源：{r.source ?? "N/A"}</Badge>
            {r.ic !== undefined && (
              <Badge tone={r.ic == null ? "neutral" : r.ic >= 0 ? "success" : "danger"}>
                IC = {r.ic ?? "N/A"}
              </Badge>
            )}
          </div>
          {r.stats && (
            <div className={s.stats} style={{ marginTop: 12 }}>
              {kv("IC 均值", r.stats.ic_mean)}
              {kv("IC 波动", r.stats.ic_std)}
              {kv("ICIR", r.stats.icir, "IC均值/波动")}
              {kv("IC>0 占比", r.stats.positive_ratio)}
              {kv("t 值", r.stats.t_stat)}
              {kv("显著(>1.96)", r.stats.significant ? "是" : "否")}
            </div>
          )}
          {icSeries.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className={s.muted}>逐期 IC 序列（{icSeries.filter((v) => v != null).length} 期）</div>
              <div style={{ display: "flex", gap: 2, alignItems: "flex-end", height: 80, overflowX: "auto" }}>
                {icSeries.map((v, i) => (
                  <div
                    key={i}
                    title={`#${i}: ${v}`}
                    style={{
                      width: 6,
                      height: `${Math.max(2, Math.abs(v ?? 0) * 70)}px`,
                      background: (v ?? 0) >= 0 ? "var(--success)" : "var(--danger)",
                    }}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}

/* ---------- 分位分组 ---------- */
function QuantileTab({ connId, setConnId, loading, msg, result, go }: SharedProps) {
  const [symbol, setSymbol] = useState("600519.SH");
  const [factor, setFactor] = useState("rsi");
  const [nq, setNq] = useState("5");
  const [forward, setForward] = useState("1");
  const [count, setCount] = useState("250");

  const r = result as import("@/services/api").QuantileResponse | null;
  const spread = r?.spread_by_quantile ?? [];
  const maxAbs = spread.length ? Math.max(...spread.map((v) => Math.abs(v ?? 0)), 1e-9) : 1;

  return (
    <>
      <div className={s.form}>
        <Field label="标的" value={symbol} onChange={setSymbol} placeholder="600519.SH" />
        <FormRow label="因子">
          <Select value={factor} onChange={(e) => setFactor(e.target.value)} options={FACTORS.map((f) => ({ value: f, label: f }))} />
        </FormRow>
        <Field label="分位数" value={nq} onChange={setNq} type="number" />
        <Field label="远期(期)" value={forward} onChange={setForward} type="number" />
        <Field label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnRow connId={connId} onChange={setConnId} />
      </div>
      <div className={s.actions}>
        <Button variant="primary" disabled={loading} onClick={() => go(() => researchApi.quantile({ symbol, factor_name: factor, n_q: Number(nq) || 5, forward: Number(forward) || 1, count: Number(count) || 250, broker_id: connId || undefined }))}>
          {loading ? "分析中…" : "分位分组"}
        </Button>
      </div>
      {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
      {r && (
        <div style={{ marginTop: 16 }}>
          <div className={s.toolbar}>
            <Badge tone="info">来源：{r.source ?? "N/A"}</Badge>
            <Badge tone="neutral">多空价差：{r.long_short_avg_return ?? "N/A"}</Badge>
            <Badge tone="neutral">多空夏普：{r.long_short_sharpe ?? "N/A"}</Badge>
          </div>
          {r.quantiles && r.quantiles.length > 0 && (
            <>
              <div className={s.scroll} style={{ marginTop: 12 }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead>
                    <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                      <th style={{ padding: "4px 6px" }}>分位</th>
                      <th>样本数</th>
                      <th>区间</th>
                      <th>均值收益</th>
                      <th>累积收益</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.quantiles.map((q) => (
                      <tr key={q.q} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td style={{ padding: "3px 6px" }}><span className="tag">Q{q.q}</span></td>
                        <td>{q.count}</td>
                        <td>{q.min} ~ {q.max}</td>
                        <td>{q.avg_return}</td>
                        <td>{q.cum_return}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className={s.muted} style={{ marginTop: 12 }}>各分位平均收益（单调性）</div>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 90 }}>
                {spread.map((v, i) => (
                  <div key={i} style={{ flex: 1, textAlign: "center" }}>
                    <div style={{ height: `${(Math.abs(v ?? 0) / maxAbs) * 70}px`, background: (v ?? 0) >= 0 ? "var(--success)" : "var(--danger)" }} />
                    <div style={{ fontSize: 11 }}>{typeof v === "number" ? v.toFixed(4) : "N/A"}</div>
                    <div style={{ fontSize: 11, color: "var(--text-dim)" }}>Q{i + 1}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}

/* ---------- 相关性矩阵 ---------- */
function CorrTab({ connId, setConnId, loading, msg, result, go }: SharedProps) {
  const [symbols, setSymbols] = useState("600519.SH,000001.SZ,300750.SZ");
  const [factor, setFactor] = useState("rsi");
  const [method, setMethod] = useState("pearson");
  const [count, setCount] = useState("250");

  const r = result as import("@/services/api").CorrelationResponse | null;

  return (
    <>
      <div className={s.form}>
        <Field label="标的(逗号分隔)" value={symbols} onChange={setSymbols} placeholder="600519.SH,000001.SZ" />
        <FormRow label="因子">
          <Select value={factor} onChange={(e) => setFactor(e.target.value)} options={FACTORS.map((f) => ({ value: f, label: f }))} />
        </FormRow>
        <FormRow label="相关">
          <Select value={method} onChange={(e) => setMethod(e.target.value)} options={[{ value: "pearson", label: "Pearson" }, { value: "spearman", label: "Spearman" }]} />
        </FormRow>
        <Field label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnRow connId={connId} onChange={setConnId} />
      </div>
      <div className={s.actions}>
        <Button variant="primary" disabled={loading} onClick={() => go(() => researchApi.correlation({ symbols, factor_name: factor, method, count: Number(count) || 250, broker_id: connId || undefined }))}>
          {loading ? "计算中…" : "相关性矩阵"}
        </Button>
      </div>
      {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
      {r && r.names.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className={s.toolbar}>
            <Badge tone="info">因子：{r.factor_name ?? "N/A"}</Badge>
            <Badge tone="info">方法：{r.method}</Badge>
          </div>
          <div className={s.scroll} style={{ marginTop: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--text-dim)" }}>
                  <th style={{ padding: "4px 6px" }}></th>
                  {r.names.map((n) => <th key={n}>{n}</th>)}
                </tr>
              </thead>
              <tbody>
                {r.names.map((a) => (
                  <tr key={a} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "3px 6px" }}><span className="tag">{a}</span></td>
                    {r.names.map((b) => {
                      const v = r.matrix[a]?.[b];
                      const c = v == null ? "var(--text-faint)" : v > 0.5 ? "var(--success)" : v < -0.5 ? "var(--danger)" : "var(--warning)";
                      return <td key={b} style={{ color: c, fontWeight: 600 }}>{v ?? "N/A"}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}

/* ---------- 组合回测 ---------- */
function PortfolioTab({ connId, setConnId, loading, msg, result, go }: SharedProps) {
  const [symbols, setSymbols] = useState("600519.SH,000001.SZ");
  const [weights, setWeights] = useState("");
  const [strategy, setStrategy] = useState("ma_cross");
  const [params, setParams] = useState(STRAT_PRESETS.ma_cross ?? "{}");
  const [capital, setCapital] = useState("1000000");
  const [count, setCount] = useState("250");

  const r = result as import("@/services/api").PortfolioBacktestResponse | null;

  return (
    <>
      <div className={s.form}>
        <Field label="标的(逗号分隔)" value={symbols} onChange={setSymbols} placeholder="600519.SH,000001.SZ" />
        <Field label="权重(JSON,缺省等权)" value={weights} onChange={setWeights} placeholder="[0.6,0.4]" />
        <FormRow label="策略">
          <Select value={strategy} onChange={(e) => { setStrategy(e.target.value); setParams(STRAT_PRESETS[e.target.value] ?? "{}"); }} options={STRATS.map((st) => ({ value: st, label: st }))} />
        </FormRow>
        <Field label="策略参数(JSON)" value={params} onChange={setParams} />
        <Field label="初始资金" value={capital} onChange={setCapital} type="number" />
        <Field label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnRow connId={connId} onChange={setConnId} />
      </div>
      <div className={s.actions}>
        <Button variant="primary" disabled={loading} onClick={() => go(() => researchApi.portfolioBacktest({ symbols, weights_json: weights || undefined, strategy, params_json: params, initial_capital: Number(capital) || 1000000, count: Number(count) || 250, broker_id: connId || undefined }))}>
          {loading ? "回测中…" : "组合回测"}
        </Button>
      </div>
      {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
      {r && (
        <div style={{ marginTop: 16 }}>
          <div className={s.toolbar}>
            <Badge tone="info">来源：{r.source ?? "N/A"}</Badge>
            <Badge tone="neutral">组合年化：{r.portfolio_metrics.annual_return ?? "N/A"}</Badge>
            <Badge tone="neutral">夏普：{r.portfolio_metrics.sharpe ?? "N/A"}</Badge>
            <Badge tone="neutral">最大回撤：{r.portfolio_metrics.max_drawdown ?? "N/A"}</Badge>
          </div>
          <div className={s.scroll} style={{ marginTop: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                  <th style={{ padding: "4px 6px" }}>标的</th>
                  <th>权重</th>
                  <th>总收益</th>
                  <th>年化</th>
                  <th>夏普</th>
                  <th>回撤</th>
                </tr>
              </thead>
              <tbody>
                {(r.symbols ?? []).map((sym, i) => {
                  const m = r.per_symbol_metrics[sym];
                  return (
                    <tr key={sym} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "3px 6px" }}><span className="tag">{sym}</span></td>
                      <td>{(r.weights[i] ?? 0)}</td>
                      <td>{m?.total_return ?? "N/A"}</td>
                      <td>{m?.annual_return ?? "N/A"}</td>
                      <td>{m?.sharpe ?? "N/A"}</td>
                      <td>{m?.max_drawdown ?? "N/A"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {r.target_portfolio !== undefined && (
            <div className={s.kv} style={{ marginTop: 12 }}>
              <span className={s.kvKey}>末态目标持仓（可喂回目标持仓页）</span>
              <span className={s.kvVal}><code>{JSON.stringify(r.target_portfolio)}</code></span>
            </div>
          )}
          {r.note && <div className={s.noteInfo} style={{ marginTop: 8 }}>{r.note}</div>}
        </div>
      )}
    </>
  );
}

/* ---------- walk-forward ---------- */
function WalkForwardTab({ connId, setConnId, loading, msg, result, go }: SharedProps) {
  const [symbol, setSymbol] = useState("600519.SH");
  const [strategy, setStrategy] = useState("ma_cross");
  const [params, setParams] = useState(STRAT_PRESETS.ma_cross ?? "{}");
  const [win, setWin] = useState("120");
  const [step, setStep] = useState("60");
  const [optimize, setOptimize] = useState(false);
  const [grid, setGrid] = useState("");
  const [count, setCount] = useState("600");

  const r = result as import("@/services/api").WalkForwardResponse | null;

  return (
    <>
      <div className={s.form}>
        <Field label="标的" value={symbol} onChange={setSymbol} placeholder="600519.SH" />
        <FormRow label="策略">
          <Select value={strategy} onChange={(e) => { setStrategy(e.target.value); setParams(STRAT_PRESETS[e.target.value] ?? "{}"); }} options={STRATS.map((st) => ({ value: st, label: st }))} />
        </FormRow>
        <Field label="训练窗" value={win} onChange={setWin} type="number" />
        <Field label="测试步长" value={step} onChange={setStep} type="number" />
        <Field label="策略参数(JSON)" value={params} onChange={setParams} />
        <Field label="参数网格(JSON)" value={grid} onChange={setGrid} placeholder='{"fast":[5,10,20]}' />
        <Field label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnRow connId={connId} onChange={setConnId} />
      </div>
      <div className={s.form} style={{ marginTop: 4 }}>
        <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={optimize} onChange={(e) => setOptimize(e.target.checked)} />
          参数寻优（对每个训练窗跑 grid search）
        </label>
      </div>
      <div className={s.actions}>
        <Button variant="primary" disabled={loading} onClick={() => go(() => researchApi.walkForward({ symbol, strategy, params_json: params, window: Number(win) || 120, step: Number(step) || 60, optimize, param_grid_json: grid || undefined, count: Number(count) || 600, broker_id: connId || undefined }))}>
          {loading ? "滚动验证中…" : "walk-forward"}
        </Button>
      </div>
      {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
      {r?.summary && (
        <div style={{ marginTop: 16 }}>
          <div className={s.toolbar}>
            <Badge tone={r.summary.robustness === "stable" ? "success" : "warning"}>稳健性：{r.summary.robustness ?? "N/A"}</Badge>
            <Badge tone="neutral">fold 数：{r.summary.n_folds ?? "N/A"}</Badge>
            <Badge tone="neutral">样本外夏普均值：{r.summary.test_sharpe_mean ?? "N/A"}</Badge>
            <Badge tone="neutral">波动：{r.summary.test_sharpe_std ?? "N/A"}</Badge>
            <Badge tone="neutral">正收益 fold：{r.summary.positive_folds ?? "N/A"}/{r.summary.n_folds ?? "?"}</Badge>
          </div>
          <div className={s.scroll} style={{ marginTop: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                  <th style={{ padding: "4px 6px" }}>#</th>
                  <th>训练区间</th>
                  <th>测试区间</th>
                  <th>参数</th>
                  <th>样本外夏普</th>
                  <th>总收益</th>
                </tr>
              </thead>
              <tbody>
                {(r.folds ?? []).map((f, i) => (
                  <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "3px 6px" }}>{i + 1}</td>
                    <td>{f.train_range.join("~")}</td>
                    <td>{f.test_range.join("~")}</td>
                    <td><code>{JSON.stringify(f.params)}</code></td>
                    <td>{f.metrics.sharpe ?? "N/A"}</td>
                    <td>{f.metrics.total_return ?? "N/A"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}

/* ---------- 绩效归因 ---------- */
function AttributionTab({ connId, setConnId, loading, msg, result, go }: SharedProps) {
  const [count, setCount] = useState("120");

  const r = result as import("@/services/api").AttributionResponse | null;

  return (
    <>
      <div className={s.form}>
        <Field label="K线条数(取参考价)" value={count} onChange={setCount} type="number" />
        <ConnRow connId={connId} onChange={setConnId} />
      </div>
      <div className={s.actions}>
        <Button variant="primary" disabled={loading} onClick={() => go(() => researchApi.attribution({ broker_id: connId || undefined, count: Number(count) || 120 }))}>
          {loading ? "归因中…" : "基于真实成交归因"}
        </Button>
      </div>
      <div className={s.noteInfo} style={{ marginTop: 8 }}>需连接券商且账户有真实成交；滑点以次根开盘/VWAP 为参考基准。</div>
      {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
      {r && (
        <div style={{ marginTop: 16 }}>
          <div className={s.toolbar}>
            <Badge tone="neutral">总盈亏：{r.total_pnl ?? "N/A"}</Badge>
            <Badge tone="neutral">滑点样本：{r.slippage?.n ?? "N/A"}</Badge>
            <Badge tone="neutral">平均滑点(bps)：{r.slippage?.avg_slippage_bps ?? "N/A"}</Badge>
          </div>
          <div className={s.stats} style={{ marginTop: 12 }}>
            {kv("佣金(估)", r.cost?.commission_est)}
            {kv("印花税(估)", r.cost?.stamp_tax_est)}
            {kv("成本合计(估)", r.cost?.total_est)}
          </div>
          {r.by_symbol && Object.keys(r.by_symbol).length > 0 && (
            <>
              <div className={s.muted} style={{ marginTop: 12 }}>分标的盈亏</div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, marginTop: 4 }}>
                <tbody>
                  {Object.entries(r.by_symbol).map(([c, v]) => (
                    <tr key={c} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "3px 6px" }}><span className="tag">{c}</span></td>
                      <td className={v >= 0 ? s.up : s.down}>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {r.by_side && (
            <>
              <div className={s.kv} style={{ marginTop: 12 }}><span className={s.kvKey}>买入(成本)</span><span className={s.kvVal}>{r.by_side.buy}</span></div>
              <div className={s.kv}><span className={s.kvKey}>卖出(收入)</span><span className={s.kvVal}>{r.by_side.sell}</span></div>
            </>
          )}
        </div>
      )}
    </>
  );
}
