import { useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";

export default function Rebalance() {
  const { activeId, activeBroker } = useBroker();
  const [rows, setRows] = useState([{ code: "600519.SH", target_ratio: 0.3 }, { code: "000001.SZ", target_ratio: 0.2 }]);
  const [doTrade, setDoTrade] = useState(false);
  const [deltaMin, setDeltaMin] = useState(3000);
  const [deltaMax, setDeltaMax] = useState(30000);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  function update(i, field, val) {
    setRows((prev) => prev.map((r, idx) => idx === i ? { ...r, [field]: field === "target_ratio" ? parseFloat(val) || 0 : val } : r));
  }
  function addRow() { setRows((p) => [...p, { code: "", target_ratio: 0 }]); }
  function removeRow(i) { setRows((p) => p.filter((_, idx) => idx !== i)); }

  async function submit() {
    setErr(""); setBusy(true);
    try {
      const r = await api.post("/rebalance", {
        targets: rows.filter((r) => r.code),
        conn_id: activeId,
        do_trade: doTrade,
        delta_min: Number(deltaMin),
        delta_max: Number(deltaMax),
      });
      setResult(r);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  return (
    <div>
      <h2 className="page-title">分仓再平衡</h2>
      <p className="page-sub">
        按目标市值占比计算差额并生成调仓单（EzQmt Reblance 等权篮子；阈值过滤 + 拆单 + 涨跌停跳过）
        {activeBroker && <span className="muted"> · 当前连接：{activeBroker.broker_name}</span>}
      </p>
      {err && <div className="toast err">{err}</div>}

      <div className="card">
        <table>
          <thead><tr><th>代码</th><th>目标占比</th><th></th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td><input value={r.code} placeholder="600519.SH" onChange={(e) => update(i, "code", e.target.value)} /></td>
                <td><input type="number" step="0.01" value={r.target_ratio} onChange={(e) => update(i, "target_ratio", e.target.value)} /></td>
                <td><button className="ghost" onClick={() => removeRow(i)}>删除</button></td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="grid grid-3" style={{ marginTop: 14 }}>
          <div>
            <label>最小调仓额（元）</label>
            <input type="number" value={deltaMin} onChange={(e) => setDeltaMin(e.target.value)} />
          </div>
          <div>
            <label>单笔最大额（元）</label>
            <input type="number" value={deltaMax} onChange={(e) => setDeltaMax(e.target.value)} />
          </div>
          <div>
            <label className="row" style={{ marginTop: 22 }}>
              <input type="checkbox" style={{ width: "auto" }} checked={doTrade}
                onChange={(e) => setDoTrade(e.target.checked)} />
              真实下单（否则仅生成计划）
            </label>
          </div>
        </div>

        <div className="btn-row">
          <button className="ghost" onClick={addRow}>添加标的</button>
          <button onClick={submit} disabled={busy}>{busy ? "生成中…" : (doTrade ? "生成并下单" : "生成调仓单")}</button>
        </div>
      </div>

      {result && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>调仓建议（共 {result.generated} 笔）</h3>
          {result.orders.length === 0 ? <p className="muted">当前持仓已接近目标，无需调仓</p> : (
            <table>
              <thead><tr><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>差额</th><th>状态</th></tr></thead>
              <tbody>
                {result.orders.map((o, i) => (
                  <tr key={o.code + i}>
                    <td>{o.code}</td>
                    <td className={o.direction === "buy" ? "up" : "down"}>{o.direction === "buy" ? "买入" : "卖出"}</td>
                    <td>{o.volume ?? "—"}</td>
                    <td>{o.price ?? "—"}</td>
                    <td>{o.diff ?? "—"}</td>
                    <td>{o.skipped ? <span className="tag warn">涨跌停跳过</span>
                      : (o.order ? <span className="tag ok">已报</span> : <span className="tag run">待执行</span>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
