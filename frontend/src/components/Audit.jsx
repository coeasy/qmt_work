import { useEffect, useState } from "react";
import { api } from "../api.js";

// 审计日志：所有交易动作（下单/撤单/算法单/打板/风控拒绝/连接变更）全程可追溯
const ACTIONS = ["", "order.submitted", "order.rejected", "order.cancel", "order.cancel_price",
                 "algo.slice", "limitup.buy", "limitup.buy_rejected", "limitup.buy_failed",
                 "broker.connect", "broker.disconnect", "broker.set_active",
                 "risk_config.update", "rebalance.order"];

export default function Audit() {
  const [rows, setRows] = useState([]);
  const [action, setAction] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    try {
      setRows(await api.audit({ action, limit: 100 }));
      setErr("");
    } catch (e) { setErr(e.message); }
  }
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t); }, [action]);

  function fmtParams(p) {
    try { return JSON.stringify(JSON.parse(p)); } catch { return p; }
  }

  return (
    <div>
      <h2 className="page-title">审计日志</h2>
      <p className="page-sub">全部交易动作（下单/撤单/算法单/打板/风控拒绝/连接变更/风控配置）可追溯，8s 自动刷新</p>
      {err && <div className="toast err">{err}</div>}

      <div className="card">
        <div className="row">
          <select value={action} onChange={(e) => setAction(e.target.value)} style={{ width: 240 }}>
            {ACTIONS.map((a) => <option key={a} value={a}>{a || "全部动作"}</option>)}
          </select>
          <button onClick={load}>刷新</button>
          <span className="muted">共 {rows.length} 条</span>
        </div>
        {rows.length === 0 ? <p className="muted" style={{ marginTop: 12 }}>暂无审计记录</p> : (
          <table style={{ marginTop: 12 }}>
            <thead>
              <tr><th>时间</th><th>来源</th><th>动作</th><th>对象</th><th>参数</th><th>结果</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="code" style={{ whiteSpace: "nowrap" }}>{r.created_at}</td>
                  <td>{r.actor}</td>
                  <td><span className="tag run">{r.action}</span></td>
                  <td className="code">{r.target}</td>
                  <td className="muted" style={{ fontSize: 11, maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {fmtParams(r.params_json)}
                  </td>
                  <td>{r.result}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
