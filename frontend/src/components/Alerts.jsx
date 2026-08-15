import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 告警规则（Alerts）
   创建/编辑/删除告警规则 · 告警历史 · 触发测试
*/

export default function Alerts() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [rules, setRules] = useState([]);
  const [history, setHistory] = useState([]);
  const [editing, setEditing] = useState(null);

  const [form, setForm] = useState({
    name: "", enabled: true, event: "*", metric: "",
    op: ">", threshold: 0, channel: "*", cooldown_seconds: 300,
  });

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    api.alertRules().then(setRules).catch(() => setRules([]));
    api.alertHistory().then(setHistory).catch(() => setHistory([]));
  }

  function resetForm() {
    setEditing(null);
    setForm({ name: "", enabled: true, event: "*", metric: "",
      op: ">", threshold: 0, channel: "*", cooldown_seconds: 300 });
  }

  async function saveRule() {
    setLoading(true); setMsg(null);
    try {
      if (!form.name) throw new Error("请输入规则名称");
      const body = { ...form };
      if (editing) body.id = editing;
      await api.saveAlertRule(body);
      setMsg({ ok: true, t: `告警规则「${form.name}」已${editing ? "更新" : "创建"}` });
      resetForm(); loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function deleteRule(id) {
    setLoading(true);
    try {
      await api.deleteAlertRule(id);
      setMsg({ ok: true, t: "规则已删除" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function testAlert() {
    setLoading(true); setMsg(null);
    try {
      await api.testAlert({ event: "system.test", payload: { level: "info" } });
      setMsg({ ok: true, t: "告警测试事件已触发" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>告警规则</h2>
        <p>自定义告警条件 · 自动匹配事件 · 历史告警记录</p>
      </div>

      <div className="card">
        <div className="btn-group">
          <button className="btn-sm" onClick={testAlert} disabled={loading}>触发测试</button>
        </div>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      <div className="card">
        <h3 style={{ marginBottom: 12 }}>{editing ? "编辑规则" : "新建规则"}</h3>
        <div className="form-row">
          <div className="form-field">
            <label>名称</label>
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="单笔亏损超 5%" />
          </div>
          <div className="form-field">
            <label>事件</label>
            <input value={form.event} onChange={(e) => setForm({ ...form, event: e.target.value })} placeholder="*" />
          </div>
          <div className="form-field">
            <label>指标</label>
            <input value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })} placeholder="loss_pct" />
          </div>
          <div className="form-field">
            <label>条件</label>
            <div className="btn-group">
              <select value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })} style={{ width: 60 }}>
                <option value=">">{'>'}</option>
                <option value=">=">{'>='}</option>
                <option value="<">{'<'}</option>
                <option value="<=">{'<='}</option>
                <option value="==">==</option>
              </select>
              <input type="number" step="0.1" value={form.threshold}
                onChange={(e) => setForm({ ...form, threshold: +e.target.value })} style={{ width: 80 }} />
            </div>
          </div>
          <div className="form-field">
            <label>通道</label>
            <input value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })} placeholder="*" />
          </div>
          <div className="form-field">
            <label>冷却(秒)</label>
            <input type="number" value={form.cooldown_seconds}
              onChange={(e) => setForm({ ...form, cooldown_seconds: +e.target.value })} />
          </div>
        </div>
        <div className="form-row" style={{ marginTop: 8 }}>
          <label className="checkbox">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.checked })} />
            启用
          </label>
          <button className="btn-primary" onClick={saveRule} disabled={loading || !form.name}>
            {loading ? "保存中…" : (editing ? "更新" : "保存")}
          </button>
          {editing && <button className="btn-sm" onClick={resetForm}>取消</button>}
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>规则列表</h3>
        {!rules.length ? <Empty>暂无告警规则</Empty> : (
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr>
                <th>ID</th><th>名称</th><th>事件</th><th>条件</th><th>通道</th><th>冷却</th><th>状态</th><th>操作</th>
              </tr></thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.id}>
                    <td>{r.id}</td>
                    <td><strong>{r.name}</strong></td>
                    <td><code>{r.event}</code></td>
                    <td><code>{r.metric} {r.op} {r.threshold}</code></td>
                    <td>{r.channel}</td>
                    <td>{r.cooldown_seconds}s</td>
                    <td><span className={`tag ${r.enabled ? "ok" : ""}`}>{r.enabled ? "启用" : "停用"}</span></td>
                    <td style={{ display: "flex", gap: 4 }}>
                      <button className="btn-sm" onClick={() => { setEditing(r.id); setForm({ ...r, enabled: !!r.enabled }); }}>编辑</button>
                      <button className="btn-sm btn-danger-sm" onClick={() => deleteRule(r.id)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>告警历史</h3>
        {!history.length ? <Empty>暂无历史告警</Empty> : (
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>ID</th><th>时间</th><th>事件</th><th>规则</th><th>通道</th><th>状态</th></tr></thead>
              <tbody>
                {history.slice(0, 30).map((r) => (
                  <tr key={r.id}>
                    <td>{r.id}</td>
                    <td style={{ fontSize: 12, color: "#9bb" }}>{r.created_at || r.timestamp || ""}</td>
                    <td><code>{r.event}</code></td>
                    <td>{r.rule_name || r.rule_id}</td>
                    <td>{r.channel}</td>
                    <td><span className="tag">{r.status || "triggered"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "24px", textAlign: "center", color: "#556" }}>{children}</div>;
}