// 任务运行时面板（G6）：/runtime/jobs 的前端入口（孤儿端点补齐，2026-09 第三期）。
// 支持提交 sync/screen/backtest 任务、查看列表与详情、取消运行中任务。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useActiveInterval } from "../hooks/useActiveInterval.js";

const KINDS = ["sync", "screen", "backtest"];

export default function RuntimeJobs() {
  const [jobs, setJobs] = useState([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  // 提交表单
  const [kind, setKind] = useState("sync");
  const [name, setName] = useState("");
  const [paramsText, setParamsText] = useState("{}");
  const [msg, setMsg] = useState(null);

  // 详情
  const [detail, setDetail] = useState(null);

  const load = useCallback(async () => {
    setErr("");
    try {
      const r = await api.runtimeJobsList();
      setJobs(Array.isArray(r) ? r : (r?.jobs || r?.items || []));
    } catch (e) { setErr(e.message || "任务列表暂不可用"); }
  }, []);

  useEffect(() => { load(); }, [load]);
  // 5s 跟随刷新（页面可见时），避免运行中任务状态/进度停在提交时刻
  useActiveInterval(load, 5000, [load]);

  const submit = async () => {
    setMsg(null); setBusy(true);
    try {
      let params = {};
      try { params = JSON.parse(paramsText || "{}"); }
      catch { setMsg({ ok: false, t: "params 不是合法 JSON" }); setBusy(false); return; }
      if (kind === "screen" && typeof params.conditions !== "object") {
        setMsg({ ok: false, t: "screen 任务 params 须含 conditions 条件树对象" }); setBusy(false); return;
      }
      const r = await api.runtimeJobsSubmit({ kind, name: name.trim() || kind, params });
      setMsg({ ok: true, t: `已提交任务 ${r.id}（${r.status}）` });
      await load();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setBusy(false); }
  };

  const showDetail = async (id) => {
    try { setDetail(await api.runtimeJobDetail(id)); }
    catch (e) { setErr(e.message); }
  };

  const cancel = async (id) => {
    try {
      await api.runtimeJobCancel(id);
      setMsg({ ok: true, t: `已请求取消 ${id}` });
      await load();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  };

  return (
    <div className="hub-body">
      <h2 className="page-title">任务运行时</h2>
      <p className="page-sub">
        后台任务队列（sync 行情同步 / screen 选股 / backtest 回测）的提交、进度与取消。
        任务在服务端事件循环内运行，重启后历史任务保留。
      </p>
      {err && <div className="toast err">{err}</div>}
      {msg && <div className={`toast ${msg.ok ? "ok" : "err"}`}>{msg.t}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>提交任务</h3>
        <div className="row">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <input style={{ flex: 1 }} placeholder="任务名（可选）"
                 value={name} onChange={(e) => setName(e.target.value)} />
          <button onClick={submit} disabled={busy}>{busy ? "提交中…" : "提交"}</button>
        </div>
        <div className="row">
          <input style={{ flex: 1 }} placeholder='params JSON，如 {"codes":["600519.SH"]}'
                 value={paramsText} onChange={(e) => setParamsText(e.target.value)} />
        </div>
      </div>

      <div className="card">
        <h3>任务列表 <button className="ghost btn-sm" onClick={load} style={{ marginLeft: 8 }}>刷新</button></h3>
        <table className="bd-table">
          <thead><tr><th>ID</th><th>类型</th><th>名称</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td className="code">{j.id}</td>
                <td>{j.kind}</td>
                <td>{j.name}</td>
                <td>{j.status}</td>
                <td>
                  <button className="ghost btn-sm" onClick={() => showDetail(j.id)}>详情</button>
                  {["queued", "running"].includes(String(j.status)) && (
                    <button className="btn-danger-sm" style={{ marginLeft: 6 }}
                            onClick={() => cancel(j.id)}>取消</button>
                  )}
                </td>
              </tr>
            ))}
            {!jobs.length && <tr><td colSpan={5} className="muted">暂无任务</td></tr>}
          </tbody>
        </table>
        {detail && (
          <details open style={{ marginTop: 10 }}>
            <summary style={{ cursor: "pointer" }}>详情 {detail.id}</summary>
            <pre style={{ fontSize: 11, maxHeight: 260, overflow: "auto" }}>
              {JSON.stringify(detail, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </div>
  );
}
