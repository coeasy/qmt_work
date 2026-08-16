import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";

const KINDS = {
  backtest: { label: "单标的回测", placeholder: "600519.SH" },
  compare: { label: "多标的对比（测量效果）", placeholder: "600519.SH,000001.SZ,300750.SZ" },
  sensitivity: { label: "参数敏感性分析", placeholder: "600519.SH" },
  sweep: { label: "参数扫描（Grid Search）", placeholder: "600519.SH" },
};

export default function Backtest() {
  const { activeId, activeBroker } = useBroker();
  const [kind, setKind] = useState("backtest");
  const [symbols, setSymbols] = useState("600519.SH");
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [cost, setCost] = useState({ commission_rate: 0.0003, stamp_tax: 0.001, slippage_bps: 5 });

  /* 参数扫描（sweep）专用 */
  const [sweepParams, setSweepParams] = useState("ma_window:5,10,20|ma_fast:5,10|ma_slow:15,20,30");

  async function refreshJobs() {
    try { setJobs(await api.get("/backtest/jobs")); } catch {}
  }
  useEffect(() => { refreshJobs(); }, []);

  async function submit() {
    setErr(""); setResult(null); setBusy(true);
    const arr = symbols.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
    const costParams = { commission_rate: cost.commission_rate, stamp_tax: cost.stamp_tax, slippage_bps: cost.slippage_bps };

    if (kind === "sweep") {
      const parsed = parseSweepParams(sweepParams);
      if (!parsed) { setErr("参数扫描格式错误"); setBusy(false); return; }
      const sweepBody = { symbol: arr[0], params: parsed, ...costParams, broker_id: activeId };
      try {
        const j = await api.backtestSweep(sweepBody);
        setJob(j); poll(j.id);
      } catch (e) { setErr(e.message); setBusy(false); }
      return;
    }

    let params = { broker_id: activeId, ...costParams };
    if (kind === "compare") params = { ...params, configs: arr.map((s) => ({ symbol: s })) };
    else if (kind === "sensitivity") params = { ...params, symbol: arr[0], param: "ma_window", values: [5, 10, 20] };
    else params = { ...params, symbol: arr[0] };
    try {
      const j = await api.post("/backtest/jobs", { kind, params });
      setJob(j);
      poll(j.id);
    } catch (e) { setErr(e.message); setBusy(false); }
  }

  function parseSweepParams(raw) {
    const groups = {};
    raw.split("|").forEach((part) => {
      const [key, vals] = part.split(":").map((s) => s.trim());
      if (!key || !vals) return;
      groups[key] = vals.split(",").map((v) => Number(v)).filter((v) => !isNaN(v));
    });
    return Object.keys(groups).length ? groups : null;
  }

  async function poll(id) {
    const t = setInterval(async () => {
      try {
        const j = await api.get(`/backtest/jobs/${id}`);
        setJob(j);
        if (j.status === "done") {
          clearInterval(t); setResult(j.result); setBusy(false); refreshJobs();
        } else if (j.status === "failed") {
          clearInterval(t); setErr(j.error || "回测失败"); setBusy(false);
        }
      } catch (e) { clearInterval(t); setErr(e.message); setBusy(false); }
    }, 800);
  }

  function equityOption() {
    const eq = result?.equity_curve || [];
    return {
      backgroundColor: "transparent",
      legend: { textStyle: { color: "#e6ecf5" }, top: 0 },
      grid: { left: 56, right: 16, top: 36, bottom: 28 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: eq.map((_, i) => i + 1), axisLabel: { color: "#8a97ad" } },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#2c3850" } }, axisLabel: { color: "#8a97ad" } },
      series: [{
        name: "净值", type: "line", smooth: true, showSymbol: false,
        data: eq, areaStyle: { opacity: 0.12 },
        lineStyle: { color: "#4f8cff", width: 2 }, itemStyle: { color: "#4f8cff" },
      }],
    };
  }

  function compareOption() {
    const rows = result?.rows || [];
    return {
      backgroundColor: "transparent",
      legend: { textStyle: { color: "#e6ecf5" }, top: 0 },
      grid: { left: 50, right: 16, top: 40, bottom: 30 },
      xAxis: { type: "category", data: rows.map((r) => r.symbol), axisLabel: { color: "#8a97ad" } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#2c3850" } } },
      tooltip: { trigger: "axis" },
      series: [
        { name: "总收益%", type: "bar", data: rows.map((r) => r.total_return), itemStyle: { color: "#4f8cff" } },
        { name: "最大回撤%", type: "bar", data: rows.map((r) => r.max_drawdown), itemStyle: { color: "#ff5c6c" } },
        { name: "夏普", type: "bar", data: rows.map((r) => r.sharpe), itemStyle: { color: "#2bd4a4" } },
      ],
    };
  }

  return (
    <div>
      <h2 className="page-title">回测与测量效果对比</h2>
      <p className="page-sub">
        异步回测任务队列（事件循环内执行，基于券商真实 K 线，支持对比/敏感性分析）
        {activeBroker && <span className="muted"> · 数据源：{activeBroker.broker_name}</span>}
      </p>
      {err && <div className="toast err">{err}</div>}

      <div className="card">
        <label>任务类型</label>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
        <label>标的（{KINDS[kind].label}）</label>
        <input value={symbols} placeholder={KINDS[kind].placeholder}
          onChange={(e) => setSymbols(e.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <label>佣金率</label>
          <input type="number" step="0.0001" value={cost.commission_rate}
                 onChange={(e) => setCost({ ...cost, commission_rate: +e.target.value })} style={{ width: 110 }} />
          <label>印花税</label>
          <input type="number" step="0.0001" value={cost.stamp_tax}
                 onChange={(e) => setCost({ ...cost, stamp_tax: +e.target.value })} style={{ width: 110 }} />
          <label>滑点(bp)</label>
          <input type="number" step="0.5" value={cost.slippage_bps}
                 onChange={(e) => setCost({ ...cost, slippage_bps: +e.target.value })} style={{ width: 110 }} />
          <span className="muted">交易成本模型（佣金/印花税/滑点）</span>
        </div>
        {kind === "sweep" && (
          <div style={{ marginTop: 10 }}>
            <label>参数扫描定义</label>
            <input value={sweepParams} onChange={(e) => setSweepParams(e.target.value)}
              placeholder="ma_window:5,10,20|ma_fast:5,10|ma_slow:15,20,30"
              style={{ width: "100%", fontFamily: "monospace" }} />
            <p className="muted" style={{ fontSize: 11, margin: "4px 0 0" }}>
              格式：<code>参数名:值1,值2,值3|参数名:值1,值2,值3</code>
              使用竖线 | 分隔多组参数
            </p>
          </div>
        )}
        <div className="btn-row">
          <button onClick={submit} disabled={busy}>{busy ? "运行中…" : "提交回测"}</button>
          {job && <span className="muted">任务 {job.id.slice(0, 8)} · {job.status} · {Math.round((job.progress || 0) * 100)}%</span>}
        </div>
      </div>

      {result && kind === "compare" && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>测量效果对比</h3>
          <Chart option={compareOption()} height={320} />
          <table style={{ marginTop: 12 }}>
            <thead><tr><th>标的</th><th>总收益%</th><th>年化%</th><th>最大回撤%</th><th>夏普</th><th>胜率%</th></tr></thead>
            <tbody>
              {result.rows.map((r, i) => (
                <tr key={i}><td>{r.symbol}</td><td className={r.total_return >= 0 ? "up" : "down"}>{r.total_return}</td>
                  <td>{r.annual_return}</td><td className="down">{r.max_drawdown}</td><td>{r.sharpe}</td><td>{r.win_rate}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result && kind === "sweep" && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>参数扫描结果（按夏普排序）</h3>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%" }}>
              <thead><tr>
                <th>排名</th>
                {Object.keys(result.params || result.groups || {}).length
                  ? Object.keys(result.params || result.groups || {}).map((k) => <th key={k}>{k}</th>)
                  : <th>参数组合</th>}
                <th>夏普</th><th>总收益%</th><th>最大回撤%</th><th>交易次数</th>
              </tr></thead>
              <tbody>
                {(result.results || []).slice(0, 20).map((r, i) => (
                  <tr key={i}>
                    <td><strong>#{i + 1}</strong></td>
                    {Object.keys(r.params || {}).map((k) => <td key={k}>{r.params[k]}</td>)}
                    <td style={{ color: "#2bd4a4", fontWeight: "bold" }}>{r.sharpe != null ? r.sharpe.toFixed(3) : "—"}</td>
                    <td className={r.total_return >= 0 ? "up" : "down"}>{r.total_return?.toFixed(2) || "—"}</td>
                    <td className="down">{r.max_drawdown?.toFixed(2) || "—"}</td>
                    <td>{r.trade_count ?? "—"}</td>
                  </tr>
                ))}
                {(result.results || []).length === 0 && <tr><td colSpan={99} className="muted">暂无结果</td></tr>}
              </tbody>
            </table>
          </div>
          <p className="muted" style={{ marginTop: 8 }}>
            扫描参数：{Object.entries(result.groups || {}).map(([k, v]) => `${k}=[${v.join(",")}]`).join(" · ")}
            共 {(result.results || []).length} 组
          </p>
        </div>
      )}

      {result && kind === "backtest" && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>回测指标</h3>
          <div className="grid grid-3">
            {Object.entries(result.metrics || {}).map(([k, v]) => (
              <div key={k}><div className="stat-value" style={{ fontSize: 20 }}>{v}</div><div className="stat-label">{k}</div></div>
            ))}
          </div>

          {result.equity_curve?.length > 1 && (
            <div style={{ marginTop: 16 }}>
              <h3>资金曲线</h3>
              <Chart option={equityOption()} height={300} />
            </div>
          )}

          {result.trades?.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h3>成交明细（最近 {result.trades.length} 笔）</h3>
              <div style={{ overflowX: "auto" }}>
                <table>
                  <thead><tr><th>时间</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th></tr></thead>
                  <tbody>
                    {result.trades.map((t, i) => (
                      <tr key={i}>
                        <td>{t.time}</td>
                        <td className={t.side === "buy" ? "up" : "down"}>{t.side === "buy" ? "买入" : "卖出"}</td>
                        <td>{t.price}</td>
                        <td>{t.qty}</td>
                        <td className={(t.pnl ?? 0) >= 0 ? "up" : "down"}>{t.pnl != null ? t.pnl : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {result && kind === "sensitivity" && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>敏感性分析结果</h3>
          <table>
            <thead><tr><th>参数值</th><th>夏普</th><th>总收益%</th><th>最大回撤%</th></tr></thead>
            <tbody>
              {(result.results || []).map((r, i) => (
                <tr key={i}>
                  <td><strong>{r.param_value}</strong></td>
                  <td>{r.sharpe?.toFixed(3)}</td>
                  <td className={r.total_return >= 0 ? "up" : "down"}>{r.total_return?.toFixed(2)}</td>
                  <td className="down">{r.max_drawdown?.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h3>历史任务</h3>
        <table>
          <thead><tr><th>ID</th><th>类型</th><th>状态</th><th>进度</th><th>创建时间</th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}><td>{j.id.slice(0, 8)}</td><td>{j.kind}</td>
                <td><span className={`tag ${j.status === "done" ? "ok" : j.status === "failed" ? "fail" : "run"}`}>{j.status}</span></td>
                <td>{Math.round((j.progress || 0) * 100)}%</td><td>{j.created_at}</td></tr>
            ))}
            {jobs.length === 0 && <tr><td colSpan={5} className="muted">暂无任务</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
