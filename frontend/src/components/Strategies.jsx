import { useState } from "react";
import { api } from "../api.js";
import StrategyRunner from "./StrategyRunner.jsx";

// 策略模板库：借鉴 QMT-MCP 的 generate_ma_strategy / save_qmt_strategy
const TYPES = [
  { id: "ma_cross", label: "双均线金叉/死叉", fields: [["fast", "快线", 5], ["slow", "慢线", 20], ["volume", "股数", 100]] },
  { id: "macd", label: "MACD 金叉/死叉", fields: [["fast", "快线", 12], ["slow", "慢线", 26], ["signal", "信号", 9], ["volume", "股数", 100]] },
  { id: "rsi", label: "RSI 超买超卖", fields: [["period", "周期", 14], ["buy_at", "买入阈值", 30], ["sell_at", "卖出阈值", 70], ["volume", "股数", 100]] },
  { id: "limitup", label: "涨停监控打板", fields: [["limit_pct", "涨停幅度", 0.1], ["cutoff", "截止时间", "10:00"], ["buy_volume", "买入股数", 100]] },
];

export default function Strategies() {
  const [type, setType] = useState("ma_cross");
  const [code, setCode] = useState("600519.SH");
  const [params, setParams] = useState({});
  const [clientPath, setClientPath] = useState("");
  const [accountId, setAccountId] = useState("");
  const [generated, setGenerated] = useState(null);
  const [saveResult, setSaveResult] = useState(null);
  const [err, setErr] = useState("");
  const [view, setView] = useState("gen");     // gen | run
  const [prefill, setPrefill] = useState(null);

  const tpl = TYPES.find((t) => t.id === type);

  async function generate() {
    setErr(""); setSaveResult(null);
    try {
      const g = await api.strategyGenerate({
        strategy_type: type, code, client_path: clientPath, account_id: accountId, params });
      setGenerated(g);
    } catch (e) { setErr(e.message); }
  }
  async function save() {
    if (!generated) return;
    try {
      const r = await api.strategySave({
        filename: `strategy_${type}.py`, content: generated.content, client_path: clientPath });
      setSaveResult(r); setErr("");
    } catch (e) { setErr(e.message); }
  }
  function runNow() {
    if (!generated) return;
    setPrefill({ strategy_type: type, code, params: { ...params, volume: params.volume ?? params.buy_volume } });
    setView("run");
  }

  if (view === "run") {
    return (
      <div>
        <div className="btn-row">
          <button onClick={() => setView("gen")}>← 返回策略生成</button>
        </div>
        <StrategyRunner prefill={prefill} />
      </div>
    );
  }

  return (
    <div>
      <h2 className="page-title">策略模板库</h2>
      <p className="page-sub">借鉴 QMT-MCP：一键生成策略代码（ma_cross/macd/rsi/limitup）。可直接写盘到 QMT 客户端，或「一键运行」在平台内当成机器人实盘/模拟执行。</p>
      {err && <div className="toast err">{err}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3>配置与生成</h3>
          <div className="row">
            <select value={type} onChange={(e) => { setType(e.target.value); setParams({}); setGenerated(null); }}>
              {TYPES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
            <input placeholder="标的代码" value={code} onChange={(e) => setCode(e.target.value)} style={{ width: 140 }} />
          </div>
          {tpl.fields.map(([key, label, def]) => (
            <div className="row" key={key}>
              <label style={{ width: 90 }}>{label}</label>
              <input value={params[key] ?? def}
                     onChange={(e) => setParams({ ...params, [key]: e.target.value })}
                     style={{ flex: 1 }} />
            </div>
          ))}
          <div className="row">
            <label style={{ width: 90 }}>客户端路径</label>
            <input placeholder="userdata_mini 目录（可选）" value={clientPath}
                   onChange={(e) => setClientPath(e.target.value)} style={{ flex: 1 }} />
          </div>
          <div className="row">
            <label style={{ width: 90 }}>资金账号</label>
            <input placeholder="可选" value={accountId}
                   onChange={(e) => setAccountId(e.target.value)} style={{ flex: 1 }} />
          </div>
          <div className="btn-row">
            <button onClick={generate}>生成策略代码</button>
            {generated && <button onClick={save}>保存到 QMT 客户端</button>}
            {generated && <button className="btn-primary" onClick={runNow}>一键运行 ▶</button>}
          </div>
          {saveResult && (
            <p className="muted" style={{ marginTop: 8 }}>
              已保存：<span className="code">{saveResult.path}</span>
            </p>
          )}
        </div>

        <div className="card">
          <h3>代码预览</h3>
          {generated ? (
            <textarea readOnly rows={22} value={generated.content}
                      style={{ width: "100%", fontFamily: "monospace", fontSize: 12 }} />
          ) : <p className="muted">点击「生成策略代码」后在此预览</p>}
        </div>
      </div>
    </div>
  );
}
