import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 对账核销 + WAL
   委托对账核销（比对 WAL 与券商当日委托/成交）
   WAL 统计、轮转、快照
*/

export default function Reconcile() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [connId, setConnId] = useState("");
  const [lastResult, setLastResult] = useState(null);
  const [walStats, setWalStats] = useState(null);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    api.reconcileLast().then(setLastResult).catch(() => setLastResult(null));
    api.reconcileWalStats().then(setWalStats).catch(() => setWalStats(null));
  }

  async function reconcile() {
    setLoading(true); setMsg(null);
    try {
      const r = await api.reconcileRun({ conn_id: connId || undefined });
      setMsg({ ok: true, t: `对账完成：检查 ${r.checked || 0} 条委托` });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function walCheckpoint() {
    setLoading(true); setMsg(null);
    try {
      await api.reconcileWalCheckpoint();
      setMsg({ ok: true, t: "WAL 归档轮转已执行" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>对账核销 <span style={{ fontSize: 13, color: "#8aa0c4" }}>WAL / 核销</span></h2>
        <p>委托对账核销（比对 WAL 与券商当日委托/成交）· WAL 统计与归档</p>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      <div className="card">
        <div className="btn-group">
          <button className="btn-sm" onClick={reconcile} disabled={loading}>{loading ? "执行中…" : "立即对账"}</button>
          <button className="btn-sm" onClick={walCheckpoint} disabled={loading}>WAL 归档轮转</button>
          <input value={connId} onChange={(e) => setConnId(e.target.value)} placeholder="指定连接（留空=全部）" style={{ marginLeft: "auto", width: 160 }} />
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>最近一次对账结果</h3>
        {lastResult ? <KVList data={lastResult} /> : <Empty>暂无对账记录</Empty>}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>WAL 统计</h3>
        {walStats ? <KVList data={walStats} /> : <Empty>WAL 未初始化</Empty>}
      </div>
    </div>
  );
}

function KVList({ data }) {
  const entries = Object.entries(data).filter(([k]) => k !== "error");
  return (
    <table className="table">
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td style={{ width: 180, color: "#8aa0c4" }}>{k}</td>
            <td><strong>{typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)}</strong></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "24px", textAlign: "center", color: "#556" }}>{children}</div>;
}