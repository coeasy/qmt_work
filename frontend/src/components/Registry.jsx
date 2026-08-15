import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 注册表 V2（P2）
   能力协商（negotiate）+ 热插拔券商档案（hotplug），纯配置化新增券商。
*/

export default function Registry() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [profiles, setProfiles] = useState([]);
  const [negotiateId, setNegotiateId] = useState("");
  const [negotiateAccount, setNegotiateAccount] = useState("STOCK");
  const [negotiateResult, setNegotiateResult] = useState(null);
  const [hotplugProfile, setHotplugProfile] = useState("");

  useEffect(() => { loadProfiles(); }, []);

  async function loadProfiles() {
    try { setProfiles(await api.registryProfiles()); } catch { setProfiles([]); }
  }

  async function negotiate() {
    setLoading(true); setMsg(null); setNegotiateResult(null);
    try {
      if (!negotiateId) throw new Error("请选择券商");
      const r = await api.registryNegotiate({ broker_id: negotiateId, account_type: negotiateAccount });
      setNegotiateResult(r);
      setMsg({ ok: true, t: "能力协商完成" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function hotplug() {
    setLoading(true); setMsg(null);
    try {
      if (!hotplugProfile) throw new Error("请输入券商档案 JSON");
      const profile = JSON.parse(hotplugProfile);
      await api.registryHotplug(profile);
      setMsg({ ok: true, t: "券商档案已热插拔，无需重启" });
      setHotplugProfile(""); loadProfiles();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function reload() {
    setLoading(true); setMsg(null);
    try {
      await api.registryReload();
      setMsg({ ok: true, t: "券商档案已重新加载" });
      loadProfiles();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>注册表 V2 <span style={{ fontSize: 13, color: "#8aa0c4" }}>Negotiate / Hotplug</span></h2>
        <p>能力协商（按账户类型推导能力）· 热插拔券商档案（无需重启）</p>
      </div>

      <div className="card">
        <div className="btn-group">
          <button className="btn-sm" onClick={reload} disabled={loading}>重新加载</button>
        </div>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      {/* 券商档案列表 */}
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>券商档案</h3>
        {!profiles.length ? <Empty>暂无档案</Empty> : (
          <div className="registry-grid">
            {profiles.map((p, i) => (
              <div key={i} className="registry-card">
                <h4>{p.name || p.broker_id}</h4>
                <p><code>{p.broker_id}</code></p>
                {p.sdk_required && <p>SDK：<code>{p.sdk_required}</code></p>}
                <p>账户类型：<span className="tag">{p.account_types ? p.account_types.join(", ") : "STOCK"}</span></p>
                {p.note && <p style={{ fontSize: 11, color: "#9bb" }}>{p.note}</p>}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 能力协商 */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>能力协商（Negotiate）</h3>
        <div className="form-row">
          <div className="form-field">
            <label>券商</label>
            <select value={negotiateId} onChange={(e) => setNegotiateId(e.target.value)}>
              <option value="">请选择</option>
              {profiles.map((p) => (
                <option key={p.broker_id} value={p.broker_id}>{p.name || p.broker_id}</option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label>账户类型</label>
            <select value={negotiateAccount} onChange={(e) => setNegotiateAccount(e.target.value)}>
              <option value="STOCK">STOCK 股票</option>
              <option value="CREDIT">CREDIT 信用</option>
              <option value="OPTION">OPTION 期权</option>
              <option value="FUTURES">FUTURES 期货</option>
            </select>
          </div>
        </div>
        <button className="btn-primary" onClick={negotiate} disabled={loading}>
          {loading ? "协商中…" : "协商能力"}
        </button>

        {negotiateResult && (
          <div className="card" style={{ marginTop: 12, padding: 12 }}>
            <table className="table">
              <tbody>
                <tr><td>支持能力</td><td>
                  <span className="tag ok">{(negotiateResult.capabilities || []).join(", ")}</span>
                </td></tr>
                <tr><td>不支持项</td><td>
                  <span className="tag">{(negotiateResult.unsupported || []).join(", ") || "无"}</span>
                </td></tr>
                <tr><td>适配说明</td><td>{negotiateResult.note || "—"}</td></tr>
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 热插拔 */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>热插拔券商档案（Hotplug）</h3>
        <p style={{ fontSize: 12, color: "#8aa0c4", marginBottom: 8 }}>
          粘贴券商档案 JSON，提交后即时生效，无需重启后端
        </p>
        <textarea rows={6} style={{ width: "100%", fontFamily: "monospace", fontSize: 12 }}
          value={hotplugProfile} onChange={(e) => setHotplugProfile(e.target.value)}
          placeholder={JSON.stringify({
            broker_id: "my_broker",
            name: "我的券商",
            sdk_required: "my_sdk",
            account_types: ["STOCK"],
            note: "自定义券商",
          }, null, 2)} />
        <button className="btn-primary" onClick={hotplug} disabled={loading}>
          {loading ? "提交中…" : "热插拔"}
        </button>
      </div>
    </div>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "24px", textAlign: "center", color: "#556" }}>{children}</div>;
}