import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 目标持仓差量同步
   保存/删除持仓计划 · 按计划执行差量同步
*/

export default function TargetPortfolio() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [plans, setPlans] = useState([]);
  const [selectedPlan, setSelectedPlan] = useState(null);
  const [syncMode, setSyncMode] = useState("volume");
  const [dryRun, setDryRun] = useState(true);
  const [totalCapital, setTotalCapital] = useState("");

  /* 新建计划 */
  const [planName, setPlanName] = useState("");
  const [planWeights, setPlanWeights] = useState("");

  useEffect(() => { loadPlans(); }, []);

  async function loadPlans() {
    api.targetPlans().then(setPlans).catch(() => setPlans([]));
  }

  async function savePlan() {
    setLoading(true); setMsg(null);
    try {
      if (!planName) throw new Error("请输入计划名称");
      const weights = JSON.parse(planWeights || "{}");
      if (!Object.keys(weights).length) throw new Error("请至少输入一个持仓权重");
      await api.targetCreatePlan({ name: planName, weights });
      setMsg({ ok: true, t: `持仓计划「${planName}」已保存` });
      setPlanName(""); setPlanWeights(""); loadPlans();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function deletePlan(pid) {
    setLoading(true);
    try {
      await api.targetDeletePlan(pid);
      setMsg({ ok: true, t: "计划已删除" });
      if (selectedPlan === pid) setSelectedPlan(null);
      loadPlans();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function syncPortfolio() {
    setLoading(true); setMsg(null);
    try {
      if (!selectedPlan) throw new Error("请选择持仓计划");
      const plan = plans.find((p) => p.id === selectedPlan);
      if (!plan) throw new Error("计划不存在");
      const body = { targets: plan.weights || plan.plan_weights || {}, mode: syncMode, dry_run: dryRun };
      if (totalCapital) body.total_capital = +totalCapital;
      const r = await api.targetSync(body);
      setMsg({ ok: true, t: `同步完成：差量 ${r.diff_count || r.orders || 0} 笔` });
      loadPlans();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>目标持仓差量同步</h2>
        <p>设定目标权重 → 计算差量 → 自动下单调仓（支持 dry-run 预览）</p>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      <div className="card">
        <h3 style={{ marginBottom: 12 }}>新建持仓计划</h3>
        <div className="form-row">
          <div className="form-field">
            <label>计划名称</label>
            <input value={planName} onChange={(e) => setPlanName(e.target.value)} placeholder="沪深300等权重" />
          </div>
          <div className="form-field" style={{ flex: 1 }}>
            <label>目标权重（JSON）</label>
            <textarea rows={3} value={planWeights} onChange={(e) => setPlanWeights(e.target.value)}
              placeholder='{"600519.SH": 0.3, "000001.SZ": 0.2, "300750.SZ": 0.15, "601318.SH": 0.1, "600036.SH": 0.08, "601166.SH": 0.07}' />
          </div>
        </div>
        <button className="btn-primary" onClick={savePlan} disabled={loading || !planName}>
          {loading ? "保存中…" : "保存计划"}
        </button>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>持仓计划列表</h3>
        {!plans.length ? <Empty>暂无持仓计划</Empty> : (
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>ID</th><th>名称</th><th>目标权重</th><th>操作</th></tr></thead>
              <tbody>
                {plans.map((p) => (
                  <tr key={p.id} className={selectedPlan === p.id ? "selected" : ""}>
                    <td>{p.id}</td>
                    <td><strong>{p.name}</strong></td>
                    <td style={{ fontSize: 11, color: "#9bb" }}>
                      {JSON.stringify(p.weights || p.plan_weights || {})}
                    </td>
                    <td style={{ display: "flex", gap: 4 }}>
                      <button className="btn-sm" onClick={() => setSelectedPlan(p.id)}>选为执行计划</button>
                      <button className="btn-sm btn-danger-sm" onClick={() => deletePlan(p.id)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>差量同步</h3>
        <div className="form-row">
          <div className="form-field">
            <label>总资金</label>
            <input type="number" value={totalCapital} onChange={(e) => setTotalCapital(e.target.value)} placeholder="1000000" />
          </div>
          <div className="form-field">
            <label>模式</label>
            <div className="btn-group">
              <button className={syncMode === "volume" ? "active" : ""} onClick={() => setSyncMode("volume")}>按股数</button>
              <button className={syncMode === "value" ? "active" : ""} onClick={() => setSyncMode("value")}>按金额</button>
              <button className={syncMode === "weight" ? "active" : ""} onClick={() => setSyncMode("weight")}>按比例</button>
            </div>
          </div>
          <div className="form-field">
            <label className="checkbox">
              <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.checked)} />
              模拟运行（不实际下单）
            </label>
          </div>
        </div>
        <button className="btn-primary" onClick={syncPortfolio} disabled={loading || !selectedPlan}>
          {loading ? "同步中…" : "执行差量同步"}
        </button>
        {selectedPlan && <p className="muted" style={{ marginTop: 8 }}>
          已选计划：{plans.find((p) => p.id === selectedPlan)?.name} · {syncMode} 模式 · {dryRun ? "预览" : "实盘"}
        </p>}
      </div>
    </div>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "24px", textAlign: "center", color: "#556" }}>{children}</div>;
}