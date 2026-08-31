// 券商连接管理：多券商 / 多客户端版本。添加、连接、断开、设为活跃、删除、探测。
// 所有券商逻辑在后端 BrokerManager；前端仅透传配置，绝不内置任何券商实现。
import { useEffect, useMemo, useRef, useState } from "react";
import { useBroker } from "../BrokerContext.jsx";
import { api } from "../api.js";
import { useBatchSelection } from "../hooks/useBatchSelection.js";
import BatchDeleteBar from "./BatchDeleteBar.jsx";
import { t as _t } from "../lib/i18n.js";

const ACCOUNT_TYPES = ["STOCK", "CREDIT", "OPTION", "FUTURES"];

// 把后端/SDK 的底层报错翻译成用户可操作的指引（解决「连接异常：无法连接行情服务！」
// 「桥接子进程握手失败：None」这类信息失明问题）。
function friendlyErr(detail) {
  if (!detail) return "连接失败，请检查券商客户端状态后重试。";
  const d = String(detail);
  // 后端已给出分步排查指引（行情/交易连接失败均如此）时直接透出，
  // 不再叠加前端提示——否则同一条建议出现两遍，反而降低可读性。
  if (d.includes("请按顺序排查") || d.includes("请按以下顺序排查") || d.includes("官方四步排查")) return d;
  if (d.includes("未登录") || d.includes("行情服务") || d.includes("无法连接行情") || d.includes("未启动"))
    return d + "\n\n→ 请先登录 QMT 客户端（极速/普通模式均可），保持客户端运行，再重试连接。";
  if (d.includes("握手失败"))
    return d + "\n\n→ 桥接子进程未在 90s 内就绪，最常见原因是 QMT 客户端未登录导致 SDK 阻塞。请登录客户端后重试；仍失败则重启客户端与桌面端。";
  if (d.includes("未配置") || d.includes("account_id"))
    return d + "\n\n→ 请在「资金账号」填写券商资金账号后再连接（行情模式可不填，但交易/持仓/下单需账号）。";
  if (d.includes("xtquant") || d.includes("SDK"))
    return d + "\n\n→ 未找到/无法加载 xtquant。请确认客户端路径指向 userdata_mini 目录且对应券商客户端已安装。";
  return d;
}

// 能力矩阵标签（与后端 QmtCapabilities 字段名对应）
const CAP_LABELS = {
  quote: "行情", kline: "K线", stock_list: "股票列表", sector: "板块",
  trading_calendar: "交易日历", trade: "交易", account: "账户",
  condition_order: "条件单", credit: "信用", option: "期权",
  futures: "期货", l2_tick: "L2逐笔", financial: "财务", realtime_push: "实时推送",
};
function capLabel(k) { return CAP_LABELS[k] || k; }

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
  const { profiles, brokers, activeId, add, connect, disconnect, remove, batchRemove, setActive, test, autoDetect } = useBroker();
  const [brokerId, setBrokerId] = useState("");
  const [batchBusy, setBatchBusy] = useState(false);
  const bsel = useBatchSelection(brokers, "conn_id");
  async function batchDeleteBrokers() {
    setBatchBusy(true);
    try { await batchRemove(bsel.selected); bsel.clear(); }
    catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setBatchBusy(false); }
  }
  const [form, setForm] = useState({
    client_path: "", client_mode: "auto", account_id: "", account_type: "STOCK",
    session_id: "", min_version: "", active: false, autoconnect: true,
  });
  const [msg, setMsg] = useState(null);
  const [testRes, setTestRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [cands, setCands] = useState(null);   // 自动发现候选（null=未探测）
  const [detecting, setDetecting] = useState(false);
  const [runtimes, setRuntimes] = useState(null); // ABI 运行时矩阵
  const [diag, setDiag] = useState(null);         // 端到端诊断快照
  const [diagBusy, setDiagBusy] = useState(false);
  const [health, setHealth] = useState({});       // conn_id -> 健康检查结果
  const [healthBusy, setHealthBusy] = useState("");
  // QMT 客户端版本画像（识别完整版/极速版、版本号、能力矩阵）
  const [verInfo, setVerInfo] = useState(null);
  const [verBusy, setVerBusy] = useState(false);
  // 「连接中」状态：按 conn_id 维度记录——避免同一连接重复点击、显示"连接中…"
  // 提示，并支持取消。必须用 ref + state 双轨：state 触发重渲染，ref 持有
  // AbortController 让"取消"能真正打断后端连接请求（不依赖用户在 spinner
  // 上傻等 30s+）。是解决「无法点击连接」的最关键 UX 修复。
  const [connecting, setConnecting] = useState({});  // conn_id -> { startedAt, hint }
  const connectAbortRef = useRef({});                 // conn_id -> AbortController

  async function loadRuntimes() {
    try { setRuntimes(await api.brokerRuntimes()); } catch (e) { setRuntimes({ error: e.message }); }
  }
  // 端到端诊断快照（排障）：宿主 ABI / 桥接运行时 / 各连接状态与行情泵健康
  async function loadDiagnostics() {
    setDiagBusy(true);
    try { setDiag(await api.brokerDiagnostics(false)); }
    catch (e) { setDiag({ error: e.message }); }
    finally { setDiagBusy(false); }
  }
  // 阶段 4 修复：连接按钮必须可点击、可取消、有可见反馈。
  // 后端握手最坏 30s（短链），按 1s tick 刷新计时器文案，避免用户"以为卡死"。
  // 关键改动：fetch + AbortController.abort() 会立即中断请求，不再 spinner 死转。
  async function doConnect(connId) {
    setMsg(null);
    const ac = new AbortController();
    connectAbortRef.current[connId] = ac;
    const startedAt = Date.now();
    setConnecting((c) => ({ ...c, [connId]: {
      startedAt,
      hint: "正在拉起桥接子进程（30s 内返回）；若一直停留，请确认 QMT 客户端已登录。",
    }}));
    // 1s 刷新"连接中…"秒数（避免被误判为卡死）
    // 刻意不用 useActiveInterval：生命周期与一次握手绑定（finally 里清表），
    // 且用户发起连接后切走再回来看，期望看到的仍是实时秒数而非冻结文案。
    const tick = setInterval(() => {
      setConnecting((c) => c[connId] ? { ...c, [connId]: { ...c[connId] } } : c);
    }, 1000);
    try {
      const r = await connect(connId, { signal: ac.signal });
      const okc = !!(r && r.connected);
      // 连接成功携版本画像：展示检测到的客户端类型/版本/能力
      if (r && r.version_profile) setVerInfo(r.version_profile);
      setMsg({ ok: okc, t: okc ? "连接成功" : friendlyErr(r && r.detail) });
    } catch (e) {
      const txt = String(e && e.message || e);
      if (ac.signal.aborted) {
        setMsg({ ok: false, t: "已取消连接" });
      } else {
        setMsg({ ok: false, t: friendlyErr(txt) });
      }
    } finally {
      clearInterval(tick);
      setConnecting((c) => { const { [connId]: _, ...rest } = c; return rest; });
      delete connectAbortRef.current[connId];
    }
  }
  function cancelConnect(connId) {
    const ac = connectAbortRef.current[connId];
    if (ac) ac.abort();
    setMsg({ ok: false, t: "已请求取消连接（如后端仍忙，请稍候片刻）" });
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

  // 点击候选：填入券商档案 + 客户端路径 + 客户端模式 + 账户，并自动跑一次探测
  function pickCandidate(c) {
    setBrokerId(c.broker_id || "");
    // 用回调形式读取最新 form.account_id（避免闭包捕获旧值）
    setForm((prev) => ({
      ...prev,
      client_path: c.client_path || "",
      client_mode: c.client_mode || "auto",
      account_type: "STOCK",
      // 自动发现到资金账号时回填；未发现则留空（后端将自动补全）
      account_id: c.default_account_id || prev.account_id || "",
    }));
    setTestRes(null);
    // 候选已带版本指纹（版本号/SDK 版本），先展示；完整画像延后探测
    setVerInfo(c.version_str || c.sdk_version
      ? {
          version_str: c.version_str || "",
          sdk_version: c.sdk_version || "",
          capabilities_list: [],
          detail: "",
        }
      : null);
    // 延迟读取最新 state：setForm 是批量的，这里用 setTimeout 让它先落盘
    test({
      broker_id: c.broker_id || "",
      client_path: c.client_path || "",
      client_mode: c.client_mode || "auto",
      account_id: c.default_account_id || "",  // 候选自动带资金账号
      account_type: "STOCK",
      session_id: 0,
      min_version: "",
    }).then(setTestRes).catch((e) => setTestRes({ connected: false, detail: e.message }));
  }

  // 探测 / 刷新客户端版本画像（前端按能力展示：完整版 vs 极速版、支持哪些功能）
  async function detectVersion() {
    const path = form.client_path;
    if (!path) { setVerInfo(null); return; }
    setVerBusy(true);
    try {
      const r = await api.brokerVersionInfo({
        client_path: path,
        client_mode: form.client_mode || "auto",
        account_id: form.account_id || "",
        account_type: form.account_type || "STOCK",
        realtime_push: false,
      });
      setVerInfo(r);
    } catch (e) {
      setVerInfo(null);
    } finally { setVerBusy(false); }
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
        client_mode: form.client_mode,
        account_id: form.account_id,
        account_type: form.account_type,
        session_id: parseInt(form.session_id || "0", 10) || 0,
        min_version: form.min_version,
        active: form.active,
        autoconnect: form.autoconnect,
      });
      setMsg({ ok: true, t: `已添加连接 ${r.name}（${r.connected ? "已连接" : "未连接，请检查客户端路径"}）` });
      setBrokerId(""); setForm({ client_path: "", client_mode: "auto", account_id: "",
        account_type: "STOCK", session_id: "", min_version: "", active: false,
        autoconnect: true });
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
        client_mode: form.client_mode,
        account_id: form.account_id,
        account_type: form.account_type,
        session_id: parseInt(form.session_id || "0", 10) || 0,
        min_version: form.min_version,
      });
      // 探测结果携版本画像：展示检测到的客户端类型/版本/能力
      if (r && r.version_profile) setVerInfo(r.version_profile);
      setTestRes(r);
    } catch (e) {
      setTestRes({ connected: false, detail: e.message });
    } finally { setBusy(false); }
  }

  // 按模式启动 QMT 客户端主程序（完整版 / 极速版 / 独立行情），需用户在弹出的窗口登录
  const [launching, setLaunching] = useState("");
  async function doLaunch(mode) {
    setMsg(null); setLaunching(mode);
    try {
      const r = await api.launchBrokerClient({ client_path: form.client_path, mode });
      setMsg({ ok: !!(r.launched || r.already_running), t: r.hint || (r.launched ? "已启动" : "启动失败") });
    } catch (e) {
      setMsg({ ok: false, t: `启动失败：${e.message}` });
    } finally { setLaunching(""); }
  }

  return (
    <div>
      <h2 className="page-title">{_t(`page.brokers.title`)}</h2>
      <p className="page-sub">
        支持多券商（国金 / 华鑫 / 银河 / 中信建投 / 兴业 / 广发 / 同花顺 / 恒生PTrade / 掘金）× 多客户端版本。
        所有下单 / 行情均经真实券商 SDK，未连接时页面给出明确提示，不返回任何假数据。
      </p>
      {/* 阶段 4 修复：连接前最强提示。
          用户反馈「无法点击连接 / 点击无响应」，根本原因几乎都是 QMT 客户端未登录
          导致 SDK 在子进程内阻塞——这里用红框 + 加粗把"必须先登录"放在最显眼处。 */}
      <div className="toast err" style={{ position: "static", maxWidth: "none",
        marginBottom: 12, padding: "10px 14px", whiteSpace: "pre-wrap", lineHeight: 1.7 }}>
        <b>连接前必读</b>
        {"\n"}1) 打开并<b>登录</b> QMT 客户端（极速/普通模式均可），保持客户端运行；
        {"\n"}2) 在客户端里完成<b>行情 + 交易</b> 登录（任一未登录都会让连接失败）；
        {"\n"}3) 点击下方「连接」按钮，最长 30 秒内返回结果（可随时取消）。
        {"\n"}若 30 秒后仍提示「握手超时」，几乎都是 QMT 客户端未登录或客户端路径错误。
      </div>
      {/* 提示渲染规则：.toast 默认是右下角 fixed + max-width 360px，装不下
          多行排查指引（会被截断，且与常驻诊断横幅重叠在同一位置）。
          因此多行文案改为页面内静态展示并保留换行；单行仍用轻量角标提示。 */}
      {msg && (
        <div
          className={`toast ${msg.ok ? "ok" : "err"}`}
          style={
            String(msg.t || "").includes("\n")
              ? { position: "static", maxWidth: "none", marginBottom: 12, whiteSpace: "pre-wrap", lineHeight: 1.7 }
              : { whiteSpace: "pre-wrap" }
          }
        >
          {msg.t}
        </div>
      )}
      {cands && cands.some((c) => c.running) && (
        <div
          className="toast"
          style={{
            position: "static", maxWidth: "none", marginBottom: 12,
            borderColor: "#2a3a55", background: "#101826", lineHeight: 1.7,
          }}
        >
          本机已发现<b>运行中的 QMT 客户端</b>：{cands.filter((c) => c.running).map((c) => c.root).join("、")}。
          连接前请先在该客户端<b>登录（行情 + 交易服务）</b>；未登录时连接会失败并提示「无法连接行情服务 / 交易连接失败」。
        </div>
      )}

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

        {/* 端到端诊断快照（浅层）：宿主 ABI / 桥接运行时 / 连接状态 / 行情泵健康 */}
        <div className="card">
          <div className="row" style={{ alignItems: "center" }}>
            <h3 style={{ margin: 0 }}>诊断快照</h3>
            <button className="btn-sm" onClick={loadDiagnostics} disabled={diagBusy} style={{ marginLeft: "auto" }}>
              {diagBusy ? "诊断中…" : "诊断"}
            </button>
          </div>
          {diag == null && !diagBusy && <p className="muted" style={{ marginTop: 8 }}>生成端到端诊断快照（不依赖真实券商连接），用于排障与长时段稳定性观察。</p>}
          {diag?.error && <p className="muted" style={{ color: "#e6a23c", marginTop: 8 }}>诊断失败：{diag.error}</p>}
          {diag && !diag.error && (
            <div style={{ marginTop: 8, fontSize: 13, lineHeight: 1.9 }}>
              <div>宿主 ABI：<code>{diag.host_abi}</code> · Python <code>{diag.host_python}</code></div>
              <div>桥接运行时：{Object.keys(diag.bundled_runtimes || {}).length
                ? Object.entries(diag.bundled_runtimes).map(([k, v]) => `${v} · cp${k}`).join("、")
                : "无（进程内直连）"}</div>
              <div className="muted" style={{ fontSize: 11 }}>生成于 {new Date((diag.generated_at || 0) * 1000).toLocaleTimeString()}</div>
              {(diag.connections || []).map((c) => (
                <div key={c.conn_id} style={{ borderTop: "1px solid #22304a", padding: "6px 0", fontSize: 12 }}>
                  <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                    <span>{c.name} <span className="muted">[{c.broker_id}]</span></span>
                    <span className={`tag ${c.connected ? "ok" : "fail"}`}>{c.connected ? "已连接" : "未连接"}</span>
                  </div>
                  <div className="muted" style={{ fontSize: 11 }}>
                    适配 {c.adapter} · 健康 {c.health_status ?? "—"} · 重连 {c.reconnect_attempts ?? 0} 次 · 行情泵 {c.pump_running ? "运行中" : "停止"}
                    {c.active ? " · 活跃" : ""}
                  </div>
                  {c.last_error && <div className="muted" style={{ fontSize: 11, color: "#e6a23c" }}>最近错误：{c.last_error}</div>}
                </div>
              ))}
              {(diag.connections || []).length === 0 && <p className="muted" style={{ marginTop: 6 }}>尚无已配置连接。</p>}
            </div>
          )}
        </div>

        {/* 已配置连接列表 */}
        <div className="card">
          <h3>已配置连接（{brokers.length}）</h3>
          <BatchDeleteBar count={bsel.selected.length} onDelete={batchDeleteBrokers} onClear={bsel.clear} busy={batchBusy} label="连接" />
          {brokers.length === 0 ? (
            <p className="muted">尚无连接。在左侧添加你的第一个券商客户端。</p>
          ) : (
            <table>
              <thead><tr>
                <th style={{ width: 32 }}><input type="checkbox" checked={bsel.allSelected} onChange={bsel.toggleAll} /></th>
                <th>名称</th><th>账户</th><th>状态</th><th>操作</th>
              </tr></thead>
              <tbody>
                {brokers.map((b) => (
                  <tr key={b.conn_id}>
                    <td><input type="checkbox" checked={bsel.sel.has(b.conn_id)} onChange={() => bsel.toggleOne(b.conn_id)} /></td>
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
                        ) : connecting[b.conn_id] ? (
                          <div className="op-stack" style={{ gap: 6 }}>
                            <button className="ghost" disabled>
                              <span className="spin">⟳</span> 连接中…
                              <span className="muted" style={{ fontSize: 11 }}>
                                {Math.max(0, Math.floor((Date.now() - connecting[b.conn_id].startedAt) / 1000))}s
                              </span>
                            </button>
                            <button className="ghost danger" onClick={() => cancelConnect(b.conn_id)}>取消</button>
                            <p className="muted" style={{ fontSize: 11, marginTop: 4, lineHeight: 1.5 }}>
                              {connecting[b.conn_id].hint}
                            </p>
                          </div>
                        ) : (
                          <button onClick={() => doConnect(b.conn_id)}>连接</button>
                        )}
                        {!b.active && !connecting[b.conn_id] && (
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
