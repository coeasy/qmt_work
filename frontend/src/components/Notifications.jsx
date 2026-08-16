import { useEffect, useState } from "react";
import { api } from "../api.js";

// 通知渠道配置（P2）：钉钉/企微/飞书/邮件/Webhook，含测试与发送日志。
const CHANNELS = [
  { id: "webhook", label: "Webhook", fields: [["url", "URL", ""]] },
  { id: "dingtalk", label: "钉钉", fields: [["url", "Webhook URL", ""], ["secret", "加签 Secret", ""]] },
  { id: "wecom", label: "企业微信", fields: [["url", "Webhook URL", ""]] },
  { id: "feishu", label: "飞书", fields: [["url", "Webhook URL", ""]] },
  { id: "email", label: "邮件", fields: [
    ["host", "SMTP 主机", ""], ["port", "端口", "587"], ["user", "账号", ""],
    ["password", "密码", ""], ["to", "收件人", ""]] },
];

export default function Notifications() {
  const [list, setList] = useState([]);
  const [logs, setLogs] = useState([]);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [channel, setChannel] = useState("webhook");
  const [name, setName] = useState("");
  const [events, setEvents] = useState("*");
  const [params, setParams] = useState({});
  const [testResult, setTestResult] = useState(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try { setList(await api.notifications()); } catch (e) { setErr(e.message); }
  }
  async function refreshLogs() {
    try { setLogs(await api.notificationLogs()); } catch (e) { setErr(e.message); }
  }
  useEffect(() => { refresh(); refreshLogs(); /* eslint-disable */ }, []);

  function onChannel(c) {
    setChannel(c);
    const ch = CHANNELS.find((x) => x.id === c);
    const init = {};
    ch.fields.forEach(([k]) => { init[k] = k === "port" ? "587" : ""; });
    setParams(init);
  }

  async function save() {
    setErr(""); setMsg(""); setBusy(true);
    const ch = CHANNELS.find((x) => x.id === channel);
    const payload = { name: name || `${ch.label}通知`, channel, events,
                      params, enabled: true };
    try {
      await api.createNotification(payload);
      setMsg("已保存通知渠道"); setName("");
      await refresh();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function test(cfg) {
    setTestResult(null);
    try {
      const r = await api.testNotification({ config: cfg });
      setTestResult({ ok: true, preview: r?.preview });
    } catch (e) { setTestResult({ ok: false, err: e.message }); }
  }

  async function del(id) {
    setErr("");
    try { await api.deleteNotification(id); await refresh(); } catch (e) { setErr(e.message); }
  }

  const ch = CHANNELS.find((x) => x.id === channel);

  return (
    <div>
      <h2 className="page-title">通知渠道配置</h2>
      <p className="page-sub">
        配置钉钉 / 企业微信 / 飞书 / 邮件 / Webhook 推送，订阅订单成交、风控拦截、涨停触发等事件，支持发送测试与查看发送日志。
      </p>
      {err && <div className="toast err">{err}</div>}
      {msg && <div className="toast ok">{msg}</div>}

      <div className="grid grid-2">
        <div className="card">
          <h3>新增 / 编辑渠道</h3>
          <div className="row">
            <select value={channel} onChange={(e) => onChannel(e.target.value)} style={{ width: 160 }}>
              {CHANNELS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
            <input placeholder="渠道名称（可选）" value={name}
                   onChange={(e) => setName(e.target.value)} style={{ flex: 1 }} />
          </div>
          <div className="row">
            <label style={{ width: 70 }}>订阅事件</label>
            <input style={{ flex: 1 }} value={events}
                   onChange={(e) => setEvents(e.target.value)}
                   placeholder="* 或 order.* / risk.blocked" />
          </div>
          {ch.fields.map(([k, label, def]) => (
            <div className="row" key={k}>
              <label style={{ width: 90 }}>{label}</label>
              <input style={{ flex: 1 }} type={k === "password" ? "password" : "text"}
                     value={params[k] ?? def ?? ""}
                     onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))} />
            </div>
          ))}
          <div className="btn-row">
            <button className="btn-primary" disabled={busy} onClick={save}>保存渠道</button>
          </div>
          {testResult && (
            <div className={`toast ${testResult.ok ? "ok" : "err"}`} style={{ marginTop: 10 }}>
              {testResult.ok ? `测试已发送 ✔ 预览：${testResult.preview}` : `测试失败：${testResult.err}`}
            </div>
          )}
        </div>

        <div className="card">
          <h3>已配置渠道（{list.length}）</h3>
          {list.length === 0 && <p className="muted">暂无渠道。</p>}
          <div className="table" style={{ marginTop: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr><th>名称</th><th>渠道</th><th>事件</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {list.map((c) => (
                  <tr key={c.id}>
                    <td>{c.name}</td>
                    <td>{c.channel}</td>
                    <td style={{ fontSize: 12 }}>{c.events}</td>
                    <td>
                      {c.enabled
                        ? <span className="tag ok">启用</span>
                        : <span className="tag">停用</span>}
                    </td>
                    <td>
                      <div className="btn-group">
                        <button className="btn-sm" onClick={() => test(c)}>测试</button>
                        <button className="btn-danger-sm" onClick={() => del(c.id)}>删除</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h3 style={{ marginTop: 16 }}>发送日志（{logs.length}）</h3>
          <div style={{ maxHeight: 200, overflow: "auto", fontSize: 12, fontFamily: "monospace" }}>
            {logs.length === 0 && <p className="muted">暂无发送记录。</p>}
            {logs.map((l, i) => (
              <div key={i}>
                <span className="muted">[{l.created_at}]</span>{" "}
                <span className={`tag ${l.status === "ok" ? "ok" : l.status === "failed" ? "fail" : ""}`}>
                  {l.status}
                </span>{" "}
                {l.title}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
