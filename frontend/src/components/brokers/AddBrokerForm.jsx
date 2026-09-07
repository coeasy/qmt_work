// 「添加券商连接」卡片：券商/路径/账户表单 + 版本能力探测 + 本机 QMT 候选列表 + 可用性探测。
// 自 Brokers.jsx 原样搬移（行为零变更）；状态与动作均由编排层通过 props 传入。
import { ACCOUNT_TYPES, capLabel, friendlyErr, probeText } from "./constants.js";

export default function AddBrokerForm({
  profiles, profile, brokerId, setBrokerId,
  form, setForm,
  verInfo, verBusy, detectVersion,
  cands, detecting, pickCandidate,
  doDetect, doTest, doAdd,
  doLaunch, launching, busy, testRes,
}) {
  return (
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

      <label>客户端路径（userdata_mini / userdata）</label>
      <input value={form.client_path} placeholder="如 C:\国金证券QMT交易端\userdata_mini（完整版填 ...\userdata）"
        onChange={(e) => setForm({ ...form, client_path: e.target.value })} />
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }}>
        <button type="button" className="btn btn-sm" onClick={detectVersion} disabled={verBusy || !form.client_path}>
          {verBusy ? "探测中…" : "探测版本 / 能力"}
        </button>
        {verInfo && (
          <span className="muted" style={{ fontSize: 11 }}>
            {verInfo.version_str ? `客户端 v${verInfo.version_str}` : "版本未知"}
            {verInfo.sdk_version ? ` · SDK ${verInfo.sdk_version}` : ""}
          </span>
        )}
      </div>

      <label>客户端模式</label>
      <select value={form.client_mode}
        onChange={(e) => setForm({ ...form, client_mode: e.target.value })}>
        <option value="auto">自动识别（推荐）</option>
        <option value="mini">极速版 MiniQMT（userdata_mini）</option>
        <option value="full">完整版大客户端（userdata，交易+行情一体）</option>
      </select>
      <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        极速版用 <code>userdata_mini</code> 数据目录；完整版大客户端用 <code>userdata</code>。
        选「自动识别」时按路径后缀 / 运行进程 / 目录存在性智能判断。
      </p>

      {verInfo && !!(verInfo.capabilities && verInfo.capabilities_list && verInfo.capabilities_list.length) && (() => {
        const list = verInfo.capabilities_list || [];
        const t = verInfo.client_type;
        const typeLabel = t === "mini" ? "极速版 MiniQMT" : t === "full" ? "完整版大客户端" : t === "both" ? "完整版+极速版并列运行" : "类型未知";
        return (
          <div className="form-tip" style={{ marginTop: 6, padding: 8, background: "#0f2133", borderRadius: 6 }}>
            <div style={{ fontSize: 12, marginBottom: 4 }}>
              <b>检测到：{typeLabel}</b>
              {verInfo.detail ? <span className="muted"> — {verInfo.detail}</span> : null}
            </div>
            <div style={{ fontSize: 11, color: "#8fb4d4", wordBreak: "break-all" }}>
              能力：{list.map((k) => (
                <span key={k} style={{ marginRight: 6 }}>{capLabel(k)}</span>
              ))}
            </div>
          </div>
        );
      })()}

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
      <div className="btn-row" style={{ marginTop: 6 }}>
        <button className="btn-sm" onClick={() => doLaunch("full")} disabled={!!launching}
          title="启动完整版大客户端 XtItClient（交易+行情一体）">
          {launching === "full" ? "启动中…" : "启动完整版"}
        </button>
        <button className="btn-sm" onClick={() => doLaunch("mini")} disabled={!!launching}
          title="启动极速版 XtMiniQmt（MiniQMT，行情+极速交易）">
          {launching === "mini" ? "启动中…" : "启动极速版"}
        </button>
        <button className="btn-sm" onClick={() => doLaunch("quote")} disabled={!!launching}
          title="启动独立行情小窗口 miniquote（为完整版补齐 58610 行情服务）">
          {launching === "quote" ? "启动中…" : "启动独立行情"}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        以上按钮从客户端路径自动定位并启动对应模式的 exe（需在弹出窗口完成登录）。
      </p>
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
                {c.broker_name
                  ? <span className="tag run">{c.broker_name}</span>
                  : c.broker_id
                    ? <span className="tag run">疑似 {c.broker_id}</span>
                    : <span className="tag">需选券商</span>}
                <span className={`tag ${c.xtquant_importable ? "ok" : c.xtquant_found ? "warn" : "fail"}`}>
                  xtquant {c.xtquant_importable ? "可用" : c.xtquant_found ? "已定位·导入失败" : "未定位"}
                </span>
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 4, wordBreak: "break-all" }}>
                {c.root}
                {c.has_userdata_mini ? " · userdata_mini ✓" : ""}
                {c.has_userdata ? " · userdata ✓" : ""}
                {c.client_mode === "mini"
                  ? " · [极速版]" : c.client_mode === "full" ? " · [完整版]" : ""}
                {c.version_str ? ` · v${c.version_str}` : ""}
                {c.sdk_version ? ` · SDK ${c.sdk_version}` : ""}
              </div>
              {Array.isArray(c.accounts) && c.accounts.length > 0 && (
                <div style={{ marginTop: 4, fontSize: 11, wordBreak: "break-all" }}>
                  <span className="muted">资金账号：</span>
                  {c.accounts.map((a, i) => (
                    <span key={i} className="tag ok" style={{ marginRight: 4 }}>
                      {a.account_id} · {a.account_type}
                      {a.account_id === c.default_account_id ? " ✓默认" : ""}
                    </span>
                  ))}
                </div>
              )}
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
          {testRes.connected ? "探测成功：客户端可连接" : `探测失败：${friendlyErr(testRes.detail || "未知原因")}`}
          {testRes.probe && probeText(testRes.probe)}
        </div>
      )}
    </div>
  );
}
