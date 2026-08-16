import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";

const PERIODS = [
  { v: "1m", label: "1分钟" },
  { v: "5m", label: "5分钟" },
  { v: "15m", label: "15分钟" },
  { v: "30m", label: "30分钟" },
  { v: "60m", label: "60分钟" },
  { v: "1d", label: "日线" },
  { v: "week", label: "周线" },
  { v: "month", label: "月线" },
];

function toDate(s) {
  if (!s) return "";
  return String(s).slice(0, 10);
}

export default function Kline() {
  const { activeId, activeBroker } = useBroker();
  const [code, setCode] = useState("600519.SH");
  const [input, setInput] = useState("600519.SH");
  const [period, setPeriod] = useState("1d");
  const [count, setCount] = useState(120);
  const [bars, setBars] = useState([]);
  const [meta, setMeta] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const wantRef = useRef({ code, period, count });

  async function load(force) {
    const sym = (force ? input.trim() : code) || code;
    setErr(""); setLoading(true);
    wantRef.current = { code: sym, period, count };
    try {
      const r = await api.marketKline({
        code: sym, period, count, conn_id: activeId, force: force ? true : false,
      });
      setCode(sym);
      setBars(r.bars || []);
      setMeta({ source: r.source, cached_at: r.cached_at, note: r.note, count: r.count });
    } catch (e) {
      setErr(e.message);
      setBars([]); setMeta(null);
    } finally {
      setLoading(false);
    }
  }

  // 券商切换时自动重载
  useEffect(() => {
    if (code) load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  useEffect(() => {
    if (code) load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function quickTrade() {
    const last = bars[bars.length - 1];
    const price = last ? Number(last.close) : null;
    window.dispatchEvent(new CustomEvent("nav", { detail: "trade" }));
    window.dispatchEvent(new CustomEvent("trade:prefill", {
      detail: { code, price: price != null ? price : undefined },
    }));
  }

  const times = bars.map((b) => toDate(b.time));
  const candle = bars.map((b) => [
    Number(b.open), Number(b.close), Number(b.low), Number(b.high),
  ]);
  const vol = bars.map((b) => Number(b.volume) || 0);

  const option = {
    backgroundColor: "transparent",
    animation: false,
    legend: { data: ["K线", "成交量"], textStyle: { color: "#e6ecf5" }, top: 4 },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 58, right: 16, top: 34, height: "56%" },
      { left: 58, right: 16, top: "74%", height: "16%" },
    ],
    xAxis: [
      { type: "category", data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } }, axisLabel: { color: "#8a97ad" },
        splitLine: { show: false }, min: "dataMin", max: "dataMax" },
      { type: "category", gridIndex: 1, data: times, boundaryGap: true,
        axisLine: { lineStyle: { color: "#2c3850" } }, axisLabel: { show: false },
        axisTick: { show: false }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, position: "right",
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { color: "#8a97ad" }, splitLine: { lineStyle: { color: "#2c3850" } } },
      { gridIndex: 1, scale: true, splitNumber: 2,
        axisLine: { lineStyle: { color: "#2c3850" } },
        axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
    ],
    series: [
      { name: "K线", type: "candlestick", data: candle,
        itemStyle: { color: "#ef4d56", color0: "#29c08a", borderColor: "#ef4d56", borderColor0: "#29c08a" } },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vol,
        itemStyle: { color: "#3a6ea5" } },
    ],
  };

  const last = bars[bars.length - 1];

  return (
    <div>
      <h2 className="page-title">K 线行情</h2>
      <p className="page-sub">
        历史 K 线（本地缓存优先，券商真实数据）
        {activeBroker && <span className="muted"> · 数据源：{activeBroker.broker_name}</span>}
      </p>
      {err && <div className="toast err">{err}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <input style={{ width: 180 }} value={input} placeholder="代码 如 600519.SH"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load(true)} />
          <button onClick={() => load(true)} disabled={loading}>{loading ? "加载中…" : "查询"}</button>
          <select value={period} onChange={(e) => { setPeriod(e.target.value); }}>
            {PERIODS.map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
          </select>
          <label className="muted">根数</label>
          <input type="number" style={{ width: 90 }} min={10} max={1000} value={count}
            onChange={(e) => setCount(Math.max(10, Math.min(1000, +e.target.value || 120)))} />
          <button className="ghost" onClick={() => load(true)}>刷新</button>
          <button className="ghost" onClick={quickTrade} disabled={!last}>交易此标的</button>
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          当前：{code} · {PERIODS.find((p) => p.v === period)?.label} · 共 {bars.length} 根
          {meta && (
            <> · 来源 {meta.source}{meta.cached_at ? ` · 缓存于 ${toDate(meta.cached_at)}` : ""}
            {meta.note ? ` · ${meta.note}` : ""}</>
          )}
        </div>
        {last && (
          <div className="grid grid-4" style={{ marginTop: 10 }}>
            <div className="card"><h3>开</h3><div className="stat-value">{last.open}</div></div>
            <div className="card"><h3>高</h3><div className="stat-value up">{last.high}</div></div>
            <div className="card"><h3>低</h3><div className="stat-value down">{last.low}</div></div>
            <div className="card"><h3>收</h3>
              <div className={`stat-value ${(Number(last.close) >= Number(last.open)) ? "up" : "down"}`}>{last.close}</div>
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <h3>蜡烛图</h3>
        {bars.length > 0
          ? <Chart option={option} height={460} />
          : <p className="muted" style={{ padding: 40, textAlign: "center" }}>
              {err ? "加载失败，请检查代码与券商连接" : "暂无数据，输入代码后点击查询"}
            </p>}
      </div>
    </div>
  );
}
