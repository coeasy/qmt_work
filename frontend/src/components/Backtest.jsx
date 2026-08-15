import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";

const KINDS = {
  backtest: { label: "单标的回测", placeholder: "600519.SH" },
  compare: { label: "多标的对比（测量效果）", placeholder: "600519.SH,000001.SZ,300750.SZ" },
  sensitivity: { label: "参数敏感性分析", placeholder: "600519.SH" },
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

  async function refreshJobs() {
    try { setJobs(await api.get("/backtest/jobs")); } catch {}
  }
  useEffect(() => { refreshJobs(); }, []);

  async function submit() {
    setErr(""); setResult(null); setBusy(true);
    const arr = symbols.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
    const costParams = { commission_rate: cost.commission_rate, stamp_tax: cost.stamp_tax, slippage_bps: cost.slippage_bps };
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

      {result && kind !== "compare" && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>回测指标</h3>
          <div className="grid grid-3">
            {Object.entries(result.metrics || {}).map(([k, v]) => (
              <div key={k}><div className="stat-value" style={{ fontSize: 20 }}>{v}</div><div className="stat-label">{k}</div></div>
            ))}
          </div>
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
