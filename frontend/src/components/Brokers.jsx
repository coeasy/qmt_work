// 券商连接管理：多券商 / 多客户端版本。添加、连接、断开、设为活跃、删除、探测。
// 所有券商逻辑在后端 BrokerManager；前端仅透传配置，绝不内置任何券商实现。
import { useEffect, useMemo, useState } from "react";
import { useBroker } from "../BrokerContext.jsx";

const ACCOUNT_TYPES = ["STOCK", "CREDIT", "OPTION", "FUTURES"];

// 将后端结构化探测诊断格式化为可读文本（sdk 定位/导入/目录线索）
function probeText(p) {
  const lines = [];
  lines.push(`\n—— 环境诊断 ——`);
  lines.push(`Python ${p.python_version || "?"}`);
  lines.push(`目录存在：${p.client_exists ? "是" : "否"}`);
  if (p.has_bin_x64 != null) lines.push(`含 bin.x64：${p.has_bin_x64 ? "是" : "否"} · 含 userdata_mini：${p.has_userdata_mini ? "是" : "否"}`);
  lines.push(`xtquant 定位：${p.xtquant_site || "未找到"}`);
  lines.push(`xtquant 可导入：${p.xtquant_importable ? "是" : "否"}`);
  if (p.import_error) lines.push(`导入错误：${String(p.import_error).slice(0, 180)}`);
  if (p.hint) lines.push(`提示：${p.hint}`);
  return lines.join("\n");
}

export default function Brokers() {
  const { profiles, brokers, activeId, add, connect, disconnect, remove, setActive, test, autoDetect } = useBroker();
  const [brokerId, setBrokerId] = useState("");
  const [form, setForm] = useState({
    client_path: "", account_id: "", account_type: "STOCK",
    session_id: "", min_version: "", active: false, autoconnect: true,
  });
  const [msg, setMsg] = useState(null);
  const [testRes, setTestRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [cands, setCands] = useState(null);   // 自动发现候选（null=未探测）
  const [detecting, setDetecting] = useState(false);
  const [runtimes, setRuntimes] = useState(null); // ABI 运行时矩阵
  const [health, setHealth] = useState({});       // conn_id -> 健康检查结果
  const [healthBusy, setHealthBusy] = useState("");

  async function loadRuntimes() {
    try { setRuntimes(await api.brokerRuntimes()); } catch (e) { setRuntimes({ error: e.message }); }
  }
  async function checkHealth(connId) {
    setHealthBusy(connId);
    try {
      const r = await api.brokerHealth(connId);
      setHealth((h) => ({ ...h, [connId]: { ok: true, data: r } }));
    } catch (e) { setHealth((h) => ({ ...h, [connId]: { ok: false, err: e.message } })); }
    finally { setHealthBusy(""); }
  }

  // 进入页面自动探测一次：发现本机 QMT 客户端则展示横幅（无需手动触发）
  useEffect(() => {
    let alive = true;
    autoDetect().then((r) => {
      if (alive && r && Array.isArray(r.candidates)) setCands(r.candidates);
    }).catch(() => {});
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function doDetect() {
    setDetecting(true); setCands(null);
    try {
      const r = await autoDetect();
      setCands(r.candidates || []);
      setMsg(r.candidates.length
        ? { ok: true, t: `发现 ${r.candidates.length} 个本机 QMT 客户端，点击候选即可一键接入` }
        : { ok: false, t: "未发现本机 QMT 客户端，请手动填写客户端路径（userdata_mini）" });
    } catch (e) {
      setCands([]);
      setMsg({ ok: false, t: `自动探测失败：${e.message}` });
    } finally { setDetecting(false); }
  }

  // 点击候选：填入券商档案 + 客户端路径 + 账户类型，并自动跑一次探测
  function pickCandidate(c) {
    setBrokerId(c.broker_id || "");
    setForm((f) => ({
      ...f,
      client_path: c.client_path || "",
      account_type: "STOCK",
    }));
    setTestRes(null);
    // 自动触发探测，验证该客户端可用性
    test({
      broker_id: c.broker_id || "",
      client_path: c.client_path || "",
      account_id: "",
      account_type: "STOCK",
      session_id: 0,
      min_version: "",
    }).then(setTestRes).catch((e) => setTestRes({ connected: false, detail: e.message }));
  }

  // 选择券商档案后回填默认客户端路径 / 账户类型
  useEffect(() => {
    const p = profiles.find((x) => x.id === brokerId);
    if (p) {
      setForm((f) => ({
        ...f,
        client_path: f.client_path || p.default_client_path || "",
        account_type: (p.supported_account_types || ["STOCK"]).includes(f.account_type)
          ? f.account_type : (p.supported_account_types || ["STOCK"])[0],
      }));
    }
  }, [brokerId, profiles]);

  const profile = useMemo(() => profiles.find((x) => x.id === brokerId), [brokerId, profiles]);

  async function doAdd() {
    setMsg(null); setBusy(true);
    try {
      const r = await add({
        broker_id: brokerId,
        client_path: form.client_path,
        account_id: form.account_id,
        account_type: form.account_type,
        session_id: parseInt(form.session_id || "0", 10) || 0,
        min_version: form.min_version,
        active: form.active,
        autoconnect: form.autoconnect,
      });
      setMsg({ ok: true, t: `已添加连接 ${r.name}（${r.connected ? "已连接" : "未连接，请检查客户端路径"}）` });
      setBrokerId(""); setForm({ client_path: "", account_id: "", account_type: "STOCK",
        session_id: "", min_version: "", active: false, autoconnect: true });
    } catch (e) {
      setMsg({ ok: false, t: e.message });
    } finally { setBusy(false); }
  }

  async function doTest() {
    setTestRes(null); setBusy(true);
    try {
      const r = await test({
        broker_id: brokerId,
        client_path: form.client_path,
        account_id: form.account_id,
        account_type: form.account_type,
        session_id: parseInt(form.session_id || "0", 10) || 0,
        min_version: form.min_version,
      });
      setTestRes(r);
    } catch (e) {
      setTestRes({ connected: false, detail: e.message });
    } finally { setBusy(false); }
  }

  return (
    <div>
      <h2 className="page-title">券商连接管理</h2>
      <p className="page-sub">
        支持多券商（国金 / 华鑫 / 银河 / 中信建投 / 兴业 / 广发 / 同花顺 / 恒生PTrade / 掘金）× 多客户端版本。
        所有下单 / 行情均经真实券商 SDK，未连接时页面给出明确提示，不返回任何假数据。
      </p>
      {msg && <div className={`toast ${msg.ok ? "ok" : "err"}`}>{msg.t}</div>}

      <div className="grid grid-2">
        {/* 新增连接 */}
        <div className="card">
          <h3>添加券商连接</h3>
          <label>券商</label>
          <select value={brokerId} onChange={(e) => setBrokerId(e.target.value)}>
            <option value="">— 选择券商 —</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>{p.name}（{p.adapter}）</option>
            ))}
          </select>

          {profile && (
            <p className="muted" style={{ marginTop: 8 }}>
              所需 SDK：<code>{profile.sdk_required || "—"}</code>
              {profile.min_version ? ` · 版本：${profile.min_version}` : ""}
              <br />{profile.note}
            </p>
          )}

          <label>客户端路径（userdata_mini）</label>
          <input value={form.client_path} placeholder="如 C:\国金证券QMT交易端\userdata_mini"
            onChange={(e) => setForm({ ...form, client_path: e.target.value })} />

          <label>资金账号</label>
          <input value={form.account_id} placeholder="如 55012345"
            onChange={(e) => setForm({ ...form, account_id: e.target.value })} />

          <label>账户类型</label>
          <select value={form.account_type} onChange={(e) => setForm({ ...form, account_type: e.target.value })}>
            {(profile?.supported_account_types || ACCOUNT_TYPES).map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>

          <div className="grid grid-2">
            <div>
              <label>Session ID（可选）</label>
              <input value={form.session_id} placeholder="0"
                onChange={(e) => setForm({ ...form, session_id: e.target.value })} />
            </div>
            <div>
              <label>客户端版本（可选）</label>
              <input value={form.min_version} placeholder="如 迅投 xtquant 2024"
                onChange={(e) => setForm({ ...form, min_version: e.target.value })} />
            </div>
          </div>

          <label className="row" style={{ marginTop: 12 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={form.active}
              onChange={(e) => setForm({ ...form, active: e.target.checked })} />
            设为活跃连接（驱动实时行情 / 默认操作目标）
          </label>
          <label className="row">
            <input type="checkbox" style={{ width: "auto" }} checked={form.autoconnect}
              onChange={(e) => setForm({ ...form, autoconnect: e.target.checked })} />
            添加后立即尝试连接
          </label>

          <div className="btn-row">
            <button onClick={doDetect} disabled={detecting}>
              {detecting ? "探测中…" : "自动探测本机 QMT"}
            </button>
            <button onClick={doTest} disabled={busy || !brokerId}>探测可用性</button>
            <button onClick={doAdd} disabled={busy || !brokerId}>添加连接</button>
          </div>
          {cands && cands.length > 0 && !detecting && (
            <div className="card" style={{ marginTop: 12, padding: 10 }}>
              <div className="row" style={{ marginBottom: 6 }}>
                <span style={{ fontWeight: 600 }}>发现 {cands.length} 个本机 QMT 客户端</span>
                <span className="muted" style={{ fontSize: 11 }}>点击候选一键填入（含券商猜测与路径）</span>
              </div>
              {cands.map((c) => (
                <div key={c.root}
                     onClick={() => pickCandidate(c)}
                     title={`点击使用：${c.client_path}`}
                     style={{
                       border: "1px solid #2a3a55", borderRadius: 8, padding: "8px 10px",
                       marginBottom: 6, cursor: "pointer", background: "#141c2c",
                     }}>
                  <div className="row" style={{ gap: 8 }}>
                    <span style={{ fontWeight: 600 }}>{c.name}</span>
                    {c.running && <span className="tag ok">运行中</span>}
                    {!c.running && <span className="tag warn">已安装</span>}
                    {c.broker_id ? <span className="tag run">疑似 {c.broker_id}</span> : <span className="tag">需选券商</span>}
                    <span className={`tag ${c.xtquant_importable ? "ok" : c.xtquant_found ? "warn" : "fail"}`}>
                      xtquant {c.xtquant_importable ? "可用" : c.xtquant_found ? "已定位·导入失败" : "未定位"}
                    </span>
                  </div>
                  <div className="muted" style={{ fontSize: 11, marginTop: 4, wordBreak: "break-all" }}>
                    {c.root}
                    {c.has_userdata_mini ? " · userdata_mini ✓" : ""}
                  </div>
                  {c.import_error && (
                    <div className="muted" style={{ fontSize: 11, marginTop: 2, color: "#e6a23c" }}>
                      导入错误：{String(c.import_error).slice(0, 160)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {cands && cands.length === 0 && !detecting && (
            <p className="muted" style={{ marginTop: 8, fontSize: 12 }}>
              未发现本机 QMT 客户端。可点击「自动探测本机 QMT」重试，或手动填写
              <code>userdata_mini</code> 目录路径。
            </p>
          )}
          {testRes && (
            <div className={`toast ${testRes.connected ? "ok" : "err"}`} style={{ position: "static", marginTop: 12, whiteSpace: "pre-wrap" }}>
              {testRes.connected ? "探测成功：客户端可连接" : `探测失败：${testRes.detail || "未知原因"}`}
              {testRes.probe && probeText(testRes.probe)}
            </div>
          )}
        </div>

        {/* ABI 运行时矩阵（排障：哪些券商 xtquant ABI 可被进程内直连/桥接覆盖） */}
        <div className="card">
          <div className="row" style={{ alignItems: "center" }}>
            <h3 style={{ margin: 0 }}>运行时矩阵</h3>
            <button className="btn-sm" onClick={loadRuntimes} style={{ marginLeft: "auto" }}>加载</button>
          </div>
          {runtimes == null && <p className="muted" style={{ marginTop: 8 }}>点击「加载」查看 ABI 运行时矩阵（主进程 Python / 桥接运行时 / 支持范围）。</p>}
          {runtimes?.error && <p className="muted" style={{ color: "#e6a23c" }}>加载失败：{runtimes.error}</p>}
          {runtimes && !runtimes.error && (
            <div style={{ marginTop: 8, fontSize: 13, lineHeight: 1.9 }}>
              <div>主进程 Python：<code>{runtimes.host_python}</code> · ABI <code>{runtimes.host_abi}</code></div>
              <div className="muted" style={{ fontSize: 11, margin: "4px 0" }}>{runtimes.supported_abi_note}</div>
              <div style={{ fontWeight: 600, marginTop: 6 }}>随包附带的桥接运行时</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                {Object.entries(runtimes.bundled_runtimes || {}).map(([k, v]) => (
                  <span key={k} className="tag">{v} · cp{k}</span>
                ))}
                {Object.keys(runtimes.bundled_runtimes || {}).length === 0 && (
                  <span className="muted">无（全部进程内直连）</span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* 已配置连接列表 */}
        <div className="card">
          <h3>已配置连接（{brokers.length}）</h3>
          {brokers.length === 0 ? (
            <p className="muted">尚无连接。在左侧添加你的第一个券商客户端。</p>
          ) : (
            <table>
              <thead><tr><th>名称</th><th>账户</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {brokers.map((b) => (
                  <tr key={b.conn_id}>
                    <td>
                      <div>{b.broker_name}</div>
                      <div className="muted" style={{ fontSize: 11 }}>
                        {b.adapter} · v{b.client_version || "?"} · {b.account_type}
                      </div>
                    </td>
                    <td>{b.account_id || "—"}</td>
                    <td>
                      <span className={`tag ${b.connected ? "ok" : "fail"}`}>{b.connected ? "已连接" : "未连接"}</span>
                      {b.active && <span className="tag run" style={{ marginLeft: 6 }}>活跃</span>}
                    </td>
                    <td>
                      <div className="op-stack">
                        {b.connected ? (
                          <button className="ghost" onClick={() => disconnect(b.conn_id)}>断开</button>
                        ) : (
                          <button onClick={() => connect(b.conn_id)}>连接</button>
                        )}
                        {!b.active && (
                          <button className="ghost" onClick={() => setActive(b.conn_id)}>设为活跃</button>
                        )}
                        <button className="ghost" onClick={() => checkHealth(b.conn_id)} disabled={healthBusy === b.conn_id}>
                          {healthBusy === b.conn_id ? "检查中…" : "健康检查"}
                        </button>
                        <button className="ghost danger" onClick={() => remove(b.conn_id)}>删除</button>
                      </div>
                      {health[b.conn_id] && (
                        <div className={`toast ${health[b.conn_id].ok ? "ok" : "err"}`} style={{ position: "static", marginTop: 6, fontSize: 12, whiteSpace: "pre-wrap" }}>
                          {health[b.conn_id].ok
                            ? `健康：连接正常 · ${JSON.stringify(health[b.conn_id].data).slice(0, 160)}`
                            : `不健康：${health[b.conn_id].err}`}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {profile && (
            <p className="muted" style={{ marginTop: 12 }}>
              支持周期：{profile.supported_periods.join(" / ")}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
