import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";

/* 研究深度层（阶段 3）：因子 IC/ICIR · 分位分组 · 相关性矩阵 · 组合回测 · walk-forward · 绩效归因
   全部基于真实券商 K 线 / 真实成交，无假数据；降级路径由后端标注 source。 */

const TABS = [
  { key: "ic", label: "因子 IC" },
  { key: "quantile", label: "分位分组" },
  { key: "corr", label: "相关性矩阵" },
  { key: "portfolio", label: "组合回测" },
  { key: "wf", label: "walk-forward" },
  { key: "attr", label: "绩效归因" },
];

const FACTORS = ["sma", "ema", "rsi", "macd", "bollinger", "atr", "adx", "cci", "kdj", "obv", "volume_ma", "returns", "log_returns", "zscore", "roc"];
const STRATS = ["ma_cross", "macd", "rsi"];
const STRAT_PRESETS = {
  ma_cross: { fast: 5, slow: 20 },
  macd: { fast: 12, slow: 26, signal: 9 },
  rsi: { period: 14, buy: 30, sell: 70 },
};

function KV({ k, v, hint }) {
  return (
    <div className="kv-pair">
      <span className="k">{k}</span>
      <span className="v">{v}{hint && <small className="metric-help"> {hint}</small>}</span>
    </div>
  );
}

export default function Research() {
  const { activeId, brokers } = useBroker();
  const [tab, setTab] = useState("ic");
  const [connId, setConnId] = useState(activeId);
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!activeId) setConnId("");
    else if (!connId) setConnId(activeId);
  }, [activeId]);

  const run = async (fn) => {
    setLoading(true); setMsg(null); setResult(null);
    try { setResult(await fn()); }
    catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>研究深度</h2>
        <p>阶段 3 · 因子 IC/ICIR · 分位分组 · 相关性矩阵 · 组合回测 · walk-forward · 绩效归因</p>
      </div>

      <div className="card" style={{ maxWidth: 980 }}>
        <div className="btn-group" style={{ marginBottom: 16 }}>
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>

        {tab === "ic" && <ICTab connId={connId} brokers={brokers} onConn={setConnId}
          loading={loading} run={run} msg={msg} result={result} />}
        {tab === "quantile" && <QuantileTab connId={connId} brokers={brokers} onConn={setConnId}
          loading={loading} run={run} msg={msg} result={result} />}
        {tab === "corr" && <CorrTab connId={connId} brokers={brokers} onConn={setConnId}
          loading={loading} run={run} msg={msg} result={result} />}
        {tab === "portfolio" && <PortfolioTab connId={connId} brokers={brokers} onConn={setConnId}
          loading={loading} run={run} msg={msg} result={result} />}
        {tab === "wf" && <WalkForwardTab connId={connId} brokers={brokers} onConn={setConnId}
          loading={loading} run={run} msg={msg} result={result} />}
        {tab === "attr" && <AttributionTab connId={connId} brokers={brokers}
          loading={loading} run={run} msg={msg} result={result} />}
      </div>
    </div>
  );
}

/* 通用：活跃连接下拉 */
function ConnSelect({ connId, brokers, onConn }) {
  return (
    <div className="form-field">
      <label>活跃连接</label>
      <select value={connId || ""} onChange={(e) => onConn(e.target.value)}>
        <option value="">默认</option>
        {brokers.map((b) => (
          <option key={b.conn_id} value={b.conn_id} disabled={!b.connected}>{b.name || b.conn_id}</option>
        ))}
      </select>
    </div>
  );
}

function TextField({ label, value, onChange, placeholder, type = "text" }) {
  return (
    <div className="form-field">
      <label>{label}</label>
      <input type={type} value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

/* ---------- 因子 IC ---------- */
function ICTab({ connId, brokers, onConn, loading, run, msg, result }) {
  const [symbol, setSymbol] = useState("600519.SH");
  const [factor, setFactor] = useState("rsi");
  const [mode, setMode] = useState("series");
  const [method, setMethod] = useState("pearson");
  const [forward, setForward] = useState(1);
  const [count, setCount] = useState(250);
  const go = () => run(() => api.researchFactorIc({
    symbol, factor_name: factor, mode, method, forward: +forward, count: +count, broker_id: connId || undefined,
  }));
  return (
    <>
      <div className="form-row">
        <TextField label="标的(面板模式逗号分隔多标的)" value={symbol} onChange={setSymbol} placeholder="600519.SH" />
        <div className="form-field"><label>因子</label>
          <select value={factor} onChange={(e) => setFactor(e.target.value)}>{FACTORS.map((f) => <option key={f}>{f}</option>)}</select>
        </div>
        <div className="form-field"><label>模式</label>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="series">单序列</option><option value="panel">截面 panel</option>
          </select>
        </div>
        <div className="form-field"><label>相关</label>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="pearson">Pearson</option><option value="spearman">Spearman</option>
          </select>
        </div>
      </div>
      <div className="form-row">
        <TextField label="远期(期)" value={forward} onChange={setForward} type="number" />
        <TextField label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnSelect connId={connId} brokers={brokers} onConn={onConn} />
      </div>
      <button className="btn-primary" onClick={go} disabled={loading}>{loading ? "分析中…" : "分析 IC"}</button>
      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="status-bar">
            <span>来源：<b>{result.source}</b></span>
            {result.ic !== undefined && <span>IC = <b>{result.ic ?? "N/A"}</b></span>}
          </div>
          {result.stats && (
            <div className="metric-card">
              <KV k="IC 均值" v={result.stats.ic_mean} />
              <KV k="IC 波动" v={result.stats.ic_std} />
              <KV k="ICIR" v={result.stats.icir} hint="IC均值/波动" />
              <KV k="IC>0 占比" v={result.stats.positive_ratio} />
              <KV k="t 值" v={result.stats.t_stat} />
              <KV k="显著(>1.96)" v={result.stats.significant ? "是" : "否"} />
            </div>
          )}
          {result.ic_series && (
            <div style={{ marginTop: 12 }}>
              <h4>逐期 IC 序列（{result.ic_series.filter((v) => v != null).length} 期）</h4>
              <div style={{ display: "flex", gap: 2, alignItems: "flex-end", height: 80, overflowX: "auto" }}>
                {result.ic_series.map((v, i) => (
                  <div key={i} title={`#${i}: ${v}`}
                    style={{ width: 6, height: `${Math.max(2, Math.abs(v || 0) * 70)}px`,
                      background: v >= 0 ? "#27c08a" : "#e0594f" }} />
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
function QuantileTab({ connId, brokers, onConn, loading, run, msg, result }) {
  const [symbol, setSymbol] = useState("600519.SH");
  const [factor, setFactor] = useState("rsi");
  const [nq, setNq] = useState(5);
  const [forward, setForward] = useState(1);
  const [count, setCount] = useState(250);
  const go = () => run(() => api.researchQuantile({
    symbol, factor_name: factor, n_q: +nq, forward: +forward, count: +count, broker_id: connId || undefined,
  }));
  const maxAbs = result ? Math.max(...result.spread_by_quantile.map((v) => Math.abs(v)), 1e-9) : 1;
  return (
    <>
      <div className="form-row">
        <TextField label="标的" value={symbol} onChange={setSymbol} placeholder="600519.SH" />
        <div className="form-field"><label>因子</label>
          <select value={factor} onChange={(e) => setFactor(e.target.value)}>{FACTORS.map((f) => <option key={f}>{f}</option>)}</select>
        </div>
        <TextField label="分位数" value={nq} onChange={setNq} type="number" />
        <TextField label="远期(期)" value={forward} onChange={setForward} type="number" />
        <TextField label="数据条数" value={count} onChange={setCount} type="number" />
      </div>
      <div className="form-row"><ConnSelect connId={connId} brokers={brokers} onConn={onConn} /></div>
      <button className="btn-primary" onClick={go} disabled={loading}>{loading ? "分析中…" : "分位分组"}</button>
      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="status-bar">
            <span>来源：<b>{result.source}</b></span>
            <span>多空价差：<b>{result.long_short_avg_return}</b></span>
            <span>多空夏普：<b>{result.long_short_sharpe ?? "N/A"}</b></span>
          </div>
          <table className="table" style={{ marginTop: 12 }}>
            <thead><tr><th>分位</th><th>样本数</th><th>区间</th><th>均值收益</th><th>累积收益</th></tr></thead>
            <tbody>
              {result.quantiles.map((q) => (
                <tr key={q.q}>
                  <td><span className="tag">Q{q.q}</span></td>
                  <td>{q.count}</td>
                  <td>{q.min} ~ {q.max}</td>
                  <td>{q.avg_return}</td><td>{q.cum_return}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4 style={{ marginTop: 12 }}>各分位平均收益（单调性）</h4>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 90 }}>
            {result.spread_by_quantile.map((v, i) => (
              <div key={i} style={{ flex: 1, textAlign: "center" }}>
                <div style={{ height: `${Math.abs(v) / maxAbs * 70}px`, background: v >= 0 ? "#27c08a" : "#e0594f" }} />
                <div style={{ fontSize: 11 }}>{v.toFixed(4)}</div>
                <div style={{ fontSize: 11, color: "#8aa0c4" }}>Q{i + 1}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

/* ---------- 相关性矩阵 ---------- */
function CorrTab({ connId, brokers, onConn, loading, run, msg, result }) {
  const [symbols, setSymbols] = useState("600519.SH,000001.SZ,300750.SZ");
  const [factor, setFactor] = useState("rsi");
  const [method, setMethod] = useState("pearson");
  const [count, setCount] = useState(250);
  const go = () => run(() => api.researchCorrelation({
    symbols, factor_name: factor, method, count: +count, broker_id: connId || undefined,
  }));
  return (
    <>
      <div className="form-row">
        <TextField label="标的(逗号分隔)" value={symbols} onChange={setSymbols} placeholder="600519.SH,000001.SZ" />
        <div className="form-field"><label>因子</label>
          <select value={factor} onChange={(e) => setFactor(e.target.value)}>{FACTORS.map((f) => <option key={f}>{f}</option>)}</select>
        </div>
        <div className="form-field"><label>相关</label>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="pearson">Pearson</option><option value="spearman">Spearman</option>
          </select>
        </div>
        <TextField label="数据条数" value={count} onChange={setCount} type="number" />
      </div>
      <div className="form-row"><ConnSelect connId={connId} brokers={brokers} onConn={onConn} /></div>
      <button className="btn-primary" onClick={go} disabled={loading}>{loading ? "计算中…" : "相关性矩阵"}</button>
      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="status-bar"><span>因子：<b>{result.factor_name}</b></span><span>方法：<b>{result.method}</b></span></div>
          <table className="table" style={{ marginTop: 12 }}>
            <thead><tr><th></th>{result.names.map((n) => <th key={n}>{n}</th>)}</tr></thead>
            <tbody>
              {result.names.map((a) => (
                <tr key={a}>
                  <td><span className="tag">{a}</span></td>
                  {result.names.map((b) => {
                    const v = result.matrix[a][b];
                    const c = v == null ? "#444" : v > 0.5 ? "#27c08a" : v < -0.5 ? "#e0594f" : "#e0a93b";
                    return <td key={b} style={{ color: c, fontWeight: 600 }}>{v ?? "N/A"}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/* ---------- 组合回测 ---------- */
function PortfolioTab({ connId, brokers, onConn, loading, run, msg, result }) {
  const [symbols, setSymbols] = useState("600519.SH,000001.SZ");
  const [weights, setWeights] = useState("");
  const [strategy, setStrategy] = useState("ma_cross");
  const [params, setParams] = useState(JSON.stringify(STRAT_PRESETS.ma_cross));
  const [capital, setCapital] = useState(1000000);
  const [count, setCount] = useState(250);
  const go = () => run(() => api.researchPortfolioBacktest({
    symbols, weights_json: weights || undefined, strategy, params_json: params,
    initial_capital: +capital, count: +count, broker_id: connId || undefined,
  }));
  return (
    <>
      <div className="form-row">
        <TextField label="标的(逗号分隔)" value={symbols} onChange={setSymbols} placeholder="600519.SH,000001.SZ" />
        <TextField label="权重(JSON,缺省等权)" value={weights} onChange={setWeights} placeholder="[0.6,0.4]" />
        <div className="form-field"><label>策略</label>
          <select value={strategy} onChange={(e) => { setStrategy(e.target.value); setParams(JSON.stringify(STRAT_PRESETS[e.target.value])); }}>
            {STRATS.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
      </div>
      <div className="form-row">
        <TextField label="策略参数(JSON)" value={params} onChange={setParams} />
        <TextField label="初始资金" value={capital} onChange={setCapital} type="number" />
        <TextField label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnSelect connId={connId} brokers={brokers} onConn={onConn} />
      </div>
      <button className="btn-primary" onClick={go} disabled={loading}>{loading ? "回测中…" : "组合回测"}</button>
      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="status-bar"><span>来源：<b>{result.source}</b></span>
            <span>组合年化：<b>{result.portfolio_metrics.annual_return}</b></span>
            <span>夏普：<b>{result.portfolio_metrics.sharpe}</b></span>
            <span>最大回撤：<b>{result.portfolio_metrics.max_drawdown}</b></span></div>
          <table className="table" style={{ marginTop: 12 }}>
            <thead><tr><th>标的</th><th>权重</th><th>总收益</th><th>年化</th><th>夏普</th><th>回撤</th></tr></thead>
            <tbody>
              {result.symbols.map((s, i) => {
                const m = result.per_symbol_metrics[s];
                return (
                  <tr key={s}>
                    <td><span className="tag">{s}</span></td>
                    <td>{result.weights[i]}</td>
                    <td>{m.total_return}</td><td>{m.annual_return}</td>
                    <td>{m.sharpe}</td><td>{m.max_drawdown}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="kv-pair" style={{ marginTop: 12 }}>
            <span className="k">末态目标持仓（可喂回目标持仓页）</span>
            <span className="v">
              <code>{JSON.stringify(result.target_portfolio)}</code>
            </span>
          </div>
          <div className="metric-help">{result.note}</div>
        </div>
      )}
    </>
  );
}

/* ---------- walk-forward ---------- */
function WalkForwardTab({ connId, brokers, onConn, loading, run, msg, result }) {
  const [symbol, setSymbol] = useState("600519.SH");
  const [strategy, setStrategy] = useState("ma_cross");
  const [params, setParams] = useState(JSON.stringify(STRAT_PRESETS.ma_cross));
  const [window, setWindow] = useState(120);
  const [step, setStep] = useState(60);
  const [optimize, setOptimize] = useState(false);
  const [grid, setGrid] = useState("");
  const [count, setCount] = useState(600);
  const go = () => run(() => api.researchWalkForward({
    symbol, strategy, params_json: params, window: +window, step: +step,
    optimize, param_grid_json: grid || undefined, count: +count, broker_id: connId || undefined,
  }));
  return (
    <>
      <div className="form-row">
        <TextField label="标的" value={symbol} onChange={setSymbol} placeholder="600519.SH" />
        <div className="form-field"><label>策略</label>
          <select value={strategy} onChange={(e) => { setStrategy(e.target.value); setParams(JSON.stringify(STRAT_PRESETS[e.target.value])); }}>
            {STRATS.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
        <TextField label="训练窗" value={window} onChange={setWindow} type="number" />
        <TextField label="测试步长" value={step} onChange={setStep} type="number" />
      </div>
      <div className="form-row">
        <TextField label="策略参数(JSON)" value={params} onChange={setParams} />
        <TextField label="参数网格(JSON)" value={grid} onChange={setGrid} placeholder='{"fast":[5,10,20]}' />
        <TextField label="数据条数" value={count} onChange={setCount} type="number" />
        <ConnSelect connId={connId} brokers={brokers} onConn={onConn} />
      </div>
      <div className="form-row">
        <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={optimize} onChange={(e) => setOptimize(e.target.checked)} />
          参数寻优（对每个训练窗跑 grid search）
        </label>
      </div>
      <button className="btn-primary" onClick={go} disabled={loading}>{loading ? "滚动验证中…" : "walk-forward"}</button>
      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
      {result && result.summary && (
        <div style={{ marginTop: 16 }}>
          <div className="status-bar">
            <span>稳健性：<b className={result.summary.robustness === "stable" ? "" : "danger"}>{result.summary.robustness}</b></span>
            <span>fold 数：<b>{result.summary.n_folds}</b></span>
            <span>样本外夏普均值：<b>{result.summary.test_sharpe_mean}</b></span>
            <span>波动：<b>{result.summary.test_sharpe_std}</b></span>
            <span>正收益 fold：<b>{result.summary.positive_folds}/{result.summary.n_folds}</b></span>
          </div>
          <table className="table" style={{ marginTop: 12 }}>
            <thead><tr><th>#</th><th>训练区间</th><th>测试区间</th><th>参数</th><th>样本外夏普</th><th>总收益</th></tr></thead>
            <tbody>
              {result.folds.map((f, i) => (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td>{f.train_range.join("~")}</td>
                  <td>{f.test_range.join("~")}</td>
                  <td><code>{JSON.stringify(f.params)}</code></td>
                  <td>{f.metrics.sharpe}</td><td>{f.metrics.total_return}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.summary.param_drift && (
            <div className="metric-help">参数漂移：{Object.entries(result.summary.param_drift).map(([k, v]) => `${k}=[${v.join(",")}]`).join("  ")}</div>
          )}
        </div>
      )}
    </>
  );
}

/* ---------- 绩效归因 ---------- */
function AttributionTab({ connId, brokers, loading, run, msg, result }) {
  const [count, setCount] = useState(120);
  const go = () => run(() => api.researchAttribution({ broker_id: connId || undefined, count: +count }));
  return (
    <>
      <div className="form-row">
        <TextField label="K线条数(取参考价)" value={count} onChange={setCount} type="number" />
        <ConnSelect connId={connId} brokers={brokers} onConn={() => {}} />
      </div>
      <button className="btn-primary" onClick={go} disabled={loading}>{loading ? "归因中…" : "基于真实成交归因"}</button>
      <div className="metric-help">需连接券商且账户有真实成交；滑点以次根开盘/VWAP 为参考基准。</div>
      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="status-bar"><span>总盈亏：<b>{result.total_pnl}</b></span>
            <span>滑点样本：<b>{result.slippage.n}</b></span>
            <span>平均滑点(bps)：<b>{result.slippage.avg_slippage_bps ?? "N/A"}</b></span></div>
          <div className="metric-card">
            <KV k="佣金(估)" v={result.cost.commission_est} />
            <KV k="印花税(估)" v={result.cost.stamp_tax_est} />
            <KV k="成本合计(估)" v={result.cost.total_est} />
          </div>
          <h4>分标的盈亏</h4>
          <table className="table">
            <thead><tr><th>标的</th><th>已实现盈亏</th></tr></thead>
            <tbody>{Object.entries(result.by_symbol).map(([c, v]) => (
              <tr key={c}><td><span className="tag">{c}</span></td><td className={v >= 0 ? "" : "danger"}>{v}</td></tr>
            ))}</tbody>
          </table>
          <h4>分买卖侧</h4>
          <div className="kv-pair"><span className="k">买入(成本)</span><span className="v">{result.by_side.buy}</span></div>
          <div className="kv-pair"><span className="k">卖出(收入)</span><span className="v">{result.by_side.sell}</span></div>
        </div>
      )}
    </>
  );
}
