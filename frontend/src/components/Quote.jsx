import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";
import Chart from "./Chart.jsx";

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/v1/ws`;
}

export default function Quote() {
  const { activeId, activeBroker } = useBroker();
  const [code, setCode] = useState("600519.SH");
  const [input, setInput] = useState("600519.SH");
  const [tick, setTick] = useState(null);
  const [series, setSeries] = useState([]);
  const [slip, setSlip] = useState(null);
  const [err, setErr] = useState("");
  const [wsState, setWsState] = useState("connecting"); // connected|reconnecting|offline
  const wsRef = useRef(null);
  const retryRef = useRef(0);      // 连续重连次数（指数退避）
  const timerRef = useRef(null);   // 重连定时器
  const wantSymRef = useRef(code); // 期望订阅的代码（重连后自动恢复）

  function scheduleReconnect() {
    if (timerRef.current) return;          // 已有定时器，避免叠加
    const delay = Math.min(0.5 * Math.pow(2, retryRef.current), 15);
    retryRef.current += 1;
    setWsState("reconnecting");
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      connect(wantSymRef.current);
    }, delay * 1000);
  }

  function connect(sym) {
    wantSymRef.current = sym;
    if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } wsRef.current = null; }
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => {
      retryRef.current = 0;
      setWsState("connected");
      setErr("");
      ws.send(JSON.stringify({ action: "subscribe", codes: [sym] }));
    };
    ws.onmessage = (e) => {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "quote" && msg.data?.code === sym) {
        const q = msg.data;
        setTick(q);
        setSeries((prev) => [...prev.slice(-59), { ts: q.ts, last: q.last }]);
      }
    };
    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null;
      setWsState("offline");
      scheduleReconnect();     // 断线自动重连（后端有 30s 补发窗口兜底）
    };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }

  function subscribe() {
    const sym = input.trim();
    if (!sym) return;
    setCode(sym); setSeries([]); setTick(null); setErr("");
    connect(sym);
    api.get("/account/slippage", { code: sym, conn_id: activeId })
      .then(setSlip).catch((e) => setErr(e.message));
  }

  useEffect(() => {
    connect(code);
    api.get("/account/slippage", { code, conn_id: activeId }).then(setSlip).catch(() => {});
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (wsRef.current) wsRef.current.close();
      wsRef.current = null;
    };
  }, [activeId]);

  const priceOption = {
    backgroundColor: "transparent",
    grid: { left: 55, right: 16, top: 16, bottom: 28 },
    xAxis: { type: "category", data: series.map((d) => d.ts), axisLabel: { show: false } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#2c3850" } } },
    tooltip: { trigger: "axis" },
    series: [{
      type: "line", smooth: true, showSymbol: false, data: series.map((d) => d.last),
      lineStyle: { color: "#4f8cff" }, areaStyle: { color: "rgba(79,140,255,.12)" },
    }],
  };

  const cls = tick ? (tick.last >= tick.bid ? "up" : "down") : "";

  return (
    <div>
      <h2 className="page-title">实时行情
        <span className={`ws-badge ws-${wsState}`}>
          {wsState === "connected" ? "● 已连接"
            : wsState === "reconnecting" ? "⟳ 重连中…"
            : wsState === "connecting" ? "○ 连接中…" : "○ 离线"}
        </span>
      </h2>
      <p className="page-sub">
        WebSocket 实时推送（来自活跃券商客户端真实 tick）
        {activeBroker && <span className="muted"> · 当前连接：{activeBroker.broker_name}</span>}
        {wsState === "reconnecting" && <span className="muted"> · 断线自动重连，恢复后自动补发最近行情</span>}
      </p>
      {err && <div className="toast err">{err}</div>}

      <div className="row" style={{ marginBottom: 16 }}>
        <input style={{ width: 200 }} value={input} placeholder="代码 如 600519.SH"
          onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && subscribe()} />
        <button onClick={subscribe}>订阅</button>
        <span className="muted">当前：{code}</span>
      </div>

      <div className="grid grid-4">
        <div className="card"><h3>最新价</h3><div className={`stat-value ${cls}`}>{tick ? tick.last : "—"}</div></div>
        <div className="card"><h3>买价</h3><div className="stat-value down">{tick ? tick.bid : "—"}</div></div>
        <div className="card"><h3>卖价</h3><div className="stat-value up">{tick ? tick.ask : "—"}</div></div>
        <div className="card"><h3>成交量</h3><div className="stat-value">{tick ? tick.volume?.toLocaleString() : "—"}</div></div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>价格走势</h3>
        <Chart option={priceOption} height={260} />
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>滑点分析（成交价 vs 当日 开/收/均价，基点 bps）</h3>
        {slip ? (
          slip.samples.length === 0 ? <p className="muted">该标的暂无成交记录，无法计算滑点。</p> : (
          <table>
            <thead><tr><th>时间</th><th>方向</th><th>成交价</th><th>对开盘</th><th>对收盘</th><th>对均价(VWAP)</th></tr></thead>
            <tbody>
              {slip.samples.map((s, i) => (
                <tr key={i}>
                  <td>{s.time}</td>
                  <td className={s.side === "buy" ? "up" : "down"}>{s.side === "buy" ? "买" : "卖"}</td>
                  <td>{s.price}</td>
                  <td className={(s.slippage_open_bps || 0) >= 0 ? "up" : "down"}>{s.slippage_open_bps}</td>
                  <td className={(s.slippage_close_bps || 0) >= 0 ? "up" : "down"}>{s.slippage_close_bps}</td>
                  <td className={(s.slippage_avg_bps || 0) >= 0 ? "up" : "down"}>{s.slippage_avg_bps}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )) : <p className="muted">加载中…</p>}
        {slip && slip.samples.length > 0 && (
          <p className="muted" style={{ marginTop: 10 }}>
            平均绝对滑点（对均价）：{slip.avg_abs_slippage_avg_bps} bps
          </p>
        )}
      </div>
    </div>
  );
}
