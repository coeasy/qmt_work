// 因子清单与单因子计算面板：GET /factors + POST /factors/compute 的前端入口
// （孤儿端点补齐，2026-09 第三期）。既有 Factors 页消费的是 /factors/compute/many
// 与 /factors/from-kline；本页补齐「指标注册表浏览 + 单指标计算」入口。
import { useEffect, useState } from "react";
import { api } from "../api.js";

export default function FactorRegistry() {
  const [items, setItems] = useState([]);
  const [err, setErr] = useState("");

  const [name, setName] = useState("");
  const [code, setCode] = useState("600519.SH");
  const [period, setPeriod] = useState("1d");
  const [count, setCount] = useState(120);
  const [win, setWin] = useState(20);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    api.factorsList()
      .then((r) => setItems(Array.isArray(r) ? r : (r?.items || [])))
      .catch((e) => setErr(e.message || "因子清单暂不可用"));
  }, []);

  const compute = async () => {
    setBusy(true); setMsg(null); setResult(null);
    try {
      const params = {};
      if (win) params.win = Number(win) || undefined;
      const r = await api.factorCompute({
        name: name.trim(), code: code.trim(), period,
        count: Number(count) || 120, params,
      });
      setResult(r);
      setMsg({ ok: true, t: "计算完成" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setBusy(false); }
  };

  return (
    <div className="hub-body">
      <h2 className="page-title">因子注册表</h2>
      <p className="page-sub">
        后端统一指标引擎（app/indicators）的注册表浏览与单指标计算入口；
        指标计算已收敛后端单一真源，前端不自算。
      </p>
      {err && <div className="toast err">{err}</div>}
      {msg && <div className={`toast ${msg.ok ? "ok" : "err"}`}>{msg.t}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>单因子计算</h3>
        <div className="row">
          <input style={{ flex: 1 }} placeholder="指标名（如 ma / macd / boll）"
                 value={name} onChange={(e) => setName(e.target.value)} list="factor-names" />
          <datalist id="factor-names">
            {items.map((f) => <option key={f.name || f} value={f.name || f} />)}
          </datalist>
          <input style={{ flex: 1 }} placeholder="代码 600519.SH"
                 value={code} onChange={(e) => setCode(e.target.value)} />
        </div>
        <div className="row">
          <select value={period} onChange={(e) => setPeriod(e.target.value)}>
            {["1d", "1w", "60m", "30m", "15m", "5m", "1m"].map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <input style={{ width: 72 }} value={count}
                 onChange={(e) => setCount(e.target.value)} title="K 线根数" />
          <input style={{ width: 72 }} value={win}
                 onChange={(e) => setWin(e.target.value)} title="窗口参数 win（可选）" />
          <button onClick={compute} disabled={busy || !name.trim() || !code.trim()}>
            {busy ? "计算中…" : "计算"}
          </button>
        </div>
        {result && (
          <details open style={{ marginTop: 8 }}>
            <summary style={{ cursor: "pointer" }}>计算结果</summary>
            <pre style={{ fontSize: 11, maxHeight: 260, overflow: "auto" }}>
              {JSON.stringify(result, null, 2)}
            </pre>
          </details>
        )}
      </div>

      <div className="card">
        <h3>已注册指标（{items.length}）</h3>
        <table className="bd-table">
          <thead><tr><th>名称</th><th>说明</th><th>默认参数</th></tr></thead>
          <tbody>
            {items.map((f, i) => (
              <tr key={(f.name || f) + i}>
                <td className="code">{f.name || f}</td>
                <td>{f.desc || f.label || "—"}</td>
                <td className="code">{f.params ? JSON.stringify(f.params) : (f.default_params ? JSON.stringify(f.default_params) : "—")}</td>
              </tr>
            ))}
            {!items.length && !err && (
              <tr><td colSpan={3} className="muted">加载中…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
