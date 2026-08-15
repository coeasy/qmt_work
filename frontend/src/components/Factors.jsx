import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";

/* 因子/指标库（P1）
   15 类指标：SMA / EMA / RSI / MACD / BOLL / ATR / ADX / CCI / KDJ / OBV /
              量MA / 收益率 / 对数收益率 / ZScore / ROC
   支持：手动输入数据、基于真实券商 K 线
*/

const INDICATORS = [
  { name: "sma",      label: "SMA 简单均线",     extra: "周期",         default: 20 },
  { name: "ema",      label: "EMA 指数均线",     extra: "周期",         default: 20 },
  { name: "rsi",      label: "RSI 相对强弱",     extra: "周期",         default: 14 },
  { name: "macd",     label: "MACD",             extra: "快/慢/信号",   default: "12,26,9" },
  { name: "boll",     label: "BOLL 布林带",      extra: "周期/标准差",  default: "20,2" },
  { name: "atr",      label: "ATR 真实波幅",     extra: "周期",         default: 14 },
  { name: "adx",      label: "ADX 趋势强弱",     extra: "周期",         default: 14 },
  { name: "cci",      label: "CCI 顺势指标",     extra: "周期",         default: 20 },
  { name: "kdj",      label: "KDJ",              extra: "周期",         default: 9 },
  { name: "obv",      label: "OBV 能量潮",       extra: "",             default: "" },
  { name: "vol_ma",   label: "量MA",             extra: "周期",         default: 20 },
  { name: "returns",  label: "收益率",           extra: "",             default: "" },
  { name: "log_returns", label: "对数收益率",     extra: "",             default: "" },
  { name: "zscore",   label: "ZScore",           extra: "周期",         default: 20 },
  { name: "roc",      label: "ROC 变动率",       extra: "周期",         default: 10 },
];

const MODELS = [
  { key: "manual", label: "手动输入数据" },
  { key: "kline", label: "基于券商 K 线" },
];

export default function Factors() {
  const { activeId, brokers } = useBroker();
  const [mode, setMode] = useState("kline");
  const [connId, setConnId] = useState(activeId);
  const [selected, setSelected] = useState(["sma", "rsi", "macd"]);
  const [symbol, setSymbol] = useState("");
  const [period, setPeriod] = useState("1d");
  const [count, setCount] = useState(250);
  const [raw, setRaw] = useState("");
  const [results, setResults] = useState(null);
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!activeId) setConnId("");
    else if (!connId) setConnId(activeId);
  }, [activeId]);

  const handleCompute = async () => {
    setLoading(true); setMsg(null); setResults(null);
    try {
      const names = selected.filter(Boolean);
      if (names.length === 0) throw new Error("请至少选择一个指标");
      if (mode === "manual") {
        const vals = parseNums(raw);
        if (vals.length < 5) throw new Error("请至少输入 5 个数值，逗号分隔");
        const r = await api.computeManyFactors({ names, values: vals });
        setResults(r);
      } else {
        if (!symbol) throw new Error("请输入股票代码");
        const body = { names, symbol, period, count, broker_id: connId || undefined };
        const r = await api.factorFromKline(body);
        setResults(r);
      }
    } catch (e) {
      setMsg({ ok: false, t: e.message });
    } finally {
      setLoading(false);
    }
  };

  const toggle = (n) => {
    setSelected((s) => s.includes(n) ? s.filter((x) => x !== n) : [...s, n]);
  };

  const parseNums = (s) => s.split(/[,\s\n]+/).map(Number).filter((x) => !isNaN(x));

  /* ---- 渲染 ---- */
  const paramCells = useMemo(() => {
    return selected.map((name) => {
      const ind = INDICATORS.find((i) => i.name === name);
      return { name, label: ind?.label || name, extra: ind?.extra || "", default: ind?.default || "" };
    });
  }, [selected]);

  return (
    <div className="page">
      <div className="page-header">
        <h2>因子 / 指标库</h2>
        <p>15 类技术指标（基于真实 K 线或手动输入数据）</p>
      </div>

      <div className="card" style={{ maxWidth: 720 }}>
        <div className="form-row">
          <div className="form-field">
            <label>模式</label>
            <div className="btn-group">
              {MODELS.map((m) => (
                <button key={m.key} className={mode === m.key ? "active" : ""} onClick={() => setMode(m.key)}>
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          {mode === "kline" && (
            <>
              <div className="form-field">
                <label>股票代码</label>
                <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="600519.SH" />
              </div>
              <div className="form-field">
                <label>K 线周期</label>
                <select value={period} onChange={(e) => setPeriod(e.target.value)}>
                  <option value="1m">1 分钟</option>
                  <option value="5m">5 分钟</option>
                  <option value="15m">15 分钟</option>
                  <option value="30m">30 分钟</option>
                  <option value="60m">60 分钟</option>
                  <option value="1d">日线</option>
                </select>
              </div>
              <div className="form-field">
                <label>数据条数</label>
                <input type="number" value={count} onChange={(e) => setCount(+e.target.value)} min={50} max={2000} />
              </div>
              <div className="form-field">
                <label>活跃连接</label>
                <select value={connId} onChange={(e) => setConnId(e.target.value)}>
                  <option value="">默认</option>
                  {brokers.map((b) => (
                    <option key={b.conn_id} value={b.conn_id} disabled={!b.connected}>
                      {b.name || b.conn_id}
                    </option>
                  ))}
                </select>
              </div>
            </>
          )}

          {mode === "manual" && (
            <div className="form-field" style={{ flex: 1 }}>
              <label>收盘价序列（逗号/换行分隔，至少 5 个）</label>
              <textarea rows={4} value={raw} onChange={(e) => setRaw(e.target.value)}
                placeholder="12.5,12.8,13.1,12.9,13.5,14.0,13.7,14.2,14.5,14.1,14.8,15.0,14.6,15.2,15.5" />
            </div>
          )}
        </div>

        <div className="form-row" style={{ marginTop: 12 }}>
          <div className="form-field" style={{ flex: 1 }}>
            <label>选择指标</label>
            <div className="chip-grid">
              {INDICATORS.map((ind) => (
                <button key={ind.name}
                  className={`chip${selected.includes(ind.name) ? " active" : ""}`}
                  onClick={() => toggle(ind.name)}>
                  {ind.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <button className="btn-primary" onClick={handleCompute} disabled={loading || selected.length === 0}>
          {loading ? "计算中…" : "计算指标"}
        </button>
      </div>

      {msg && (
        <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>
      )}

      {results && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>计算结果
            {results.symbol && <span style={{ fontSize: 13, color: "#8aa0c4", marginLeft: 8 }}>
              {results.symbol} · {results.period} · {results.count} 条
            </span>}
          </h3>
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr>
                <th>指标</th>
                <th>最新值</th>
                <th>数据点数</th>
                <th>前 10 个值</th>
              </tr></thead>
              <tbody>
                {Object.entries(results.values || results).map(([name, data]) => {
                  const arr = Array.isArray(data) ? data : (data && data.values ? data.values : []);
                  const latest = arr.length ? arr[arr.length - 1] : null;
                  return (
                    <tr key={name}>
                      <td><span className="tag">{name}</span></td>
                      <td><strong>{latest !== null ? latest.toFixed(4) : "N/A"}</strong></td>
                      <td>{arr.length}</td>
                      <td style={{ fontSize: 12, color: "#9bb" }}>
                        {arr.slice(0, 10).map((v) => v.toFixed ? v.toFixed(3) : v).join(", ")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}