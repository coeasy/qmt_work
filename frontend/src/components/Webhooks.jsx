import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 出站 Webhook（B2）
   HMAC-SHA256 签名 + 指数退避重试
   订单/成交/账户/风控事件自动分发到外部 URL
*/

export default function Webhooks() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [subs, setSubs] = useState([]);
  const [deliveries, setDeliveries] = useState([]);

  const [form, setForm] = useState({
    url: "", events: "order,deal,account,risk", secret: "",
    enabled: true, retry_backoff: 2,
  });

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    api.webhookSubscriptions().then(setSubs).catch(() => setSubs([]));
    api.webhookDeliveries().then(setDeliveries).catch(() => setDeliveries([]));
  }

  async function saveWebhook() {
    setLoading(true); setMsg(null);
    try {
      if (!form.url) throw new Error("请输入 Webhook URL");
      if (!form.secret) throw new Error("请输入签名密钥");
      const body = { ...form, events: form.events.split(",").map((e) => e.trim()).filter(Boolean) };
      await api.webhookCreate(body);
      setMsg({ ok: true, t: "Webhook 订阅已创建" });
      setForm({ url: "", events: "order,deal,account,risk", secret: "", enabled: true, retry_backoff: 2 });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function deleteWebhook(sid) {
    setLoading(true);
    try {
      await api.webhookDelete(sid);
      setMsg({ ok: true, t: "订阅已删除" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function testWebhook(sid) {
    setLoading(true); setMsg(null);
    try {
      await api.webhookTest(sid);
      setMsg({ ok: true, t: "测试事件已发送" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>出站 Webhook</h2>
        <p>HMAC-SHA256 签名 · 指数退避重试 · 事件自动分发</p>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      <div className="card">
        <h3 style={{ marginBottom: 12 }}>新建订阅</h3>
        <div className="form-row">
          <div className="form-field">
            <label>URL</label>
            <input value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://your-server.com/hook" />
          </div>
          <div className="form-field">
            <label>事件</label>
            <input value={form.events} onChange={(e) => setForm({ ...form, events: e.target.value })} placeholder="order,deal,account,risk" />
          </div>
          <div className="form-field">
            <label>签名密钥</label>
            <input value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} placeholder="shared-secret" />
          </div>
          <div className="form-field">
            <label>退避倍数</label>
            <input type="number" step="0.1" min="1" value={form.retry_backoff}
              onChange={(e) => setForm({ ...form, retry_backoff: +e.target.value })} />
          </div>
        </div>
        <div className="form-row" style={{ marginTop: 8 }}>
          <label className="checkbox">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.checked })} />
            启用
          </label>
          <button className="btn-primary" onClick={saveWebhook} disabled={loading || !form.url}>
            {loading ? "保存中…" : "创建订阅"}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>订阅列表</h3>
        {!subs.length ? <Empty>暂无订阅</Empty> : (
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr>
                <th>ID</th><th>URL</th><th>事件</th><th>签名</th><th>退避</th><th>状态</th><th>上次送达</th><th>操作</th>
              </tr></thead>
              <tbody>
                {subs.map((s) => (
                  <tr key={s.id}>
                    <td>{s.id}</td>
                    <td style={{ fontSize: 12, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{s.url}</td>
                    <td><code>{Array.isArray(s.events) ? s.events.join(", ") : String(s.events)}</code></td>
                    <td>{s.secret ? "🔑" + s.secret.slice(0, 4) + "..." : "—"}</td>
                    <td>{s.retry_backoff}x</td>
                    <td><span className={`tag ${s.enabled ? "ok" : ""}`}>{s.enabled ? "启用" : "停用"}</span></td>
                    <td style={{ fontSize: 12, color: "#9bb" }}>{s.last_delivery || "—"}</td>
                    <td style={{ display: "flex", gap: 4 }}>
                      <button className="btn-sm" onClick={() => testWebhook(s.id)}>测试</button>
                      <button className="btn-sm btn-danger-sm" onClick={() => deleteWebhook(s.id)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginBottom: 12 }}>送达记录</h3>
        {!deliveries.length ? <Empty>暂无送达记录</Empty> : (
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr><th>ID</th><th>时间</th><th>事件</th><th>URL</th><th>重试次数</th><th>状态</th></tr></thead>
              <tbody>
                {deliveries.slice(0, 30).map((d) => (
                  <tr key={d.id}>
                    <td>{d.id}</td>
                    <td style={{ fontSize: 12, color: "#9bb" }}>{d.created_at || d.timestamp || ""}</td>
                    <td>{d.event_type || d.event}</td>
                    <td style={{ fontSize: 12 }}>{d.url || ""}</td>
                    <td>{d.retry_count ?? 0}</td>
                    <td><span className={`tag ${d.success ? "ok" : ""}`}>{d.success ? "成功" : "失败"}</span></td>
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