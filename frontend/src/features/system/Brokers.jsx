// 券商连接管理：多券商 / 多客户端版本。添加、连接、断开、设为活跃、删除、探测。
// 所有券商逻辑在后端 BrokerManager；前端仅透传配置，绝不内置任何券商实现。
// 本文件为编排层：持有状态与动作，卡片渲染拆分至 ./brokers/ 子组件。
import { useEffect, useMemo, useRef, useState } from "react";
import { useBroker } from "../../BrokerContext.jsx";
import { api } from "../../api.js";
import { useBatchSelection } from "../../hooks/useBatchSelection.js";
import { t as _t } from "../../lib/i18n.js";
import AddBrokerForm from "../../components/brokers/AddBrokerForm.jsx";
import ConnectionCard from "../../components/brokers/ConnectionCard.jsx";
import DiagnosticsPanel from "../../components/brokers/DiagnosticsPanel.jsx";
import RuntimeMatrixCard from "../../components/brokers/RuntimeMatrixCard.jsx";
import { friendlyErr } from "../../components/brokers/constants.js";

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
        <AddBrokerForm
          profiles={profiles}
          profile={profile}
          brokerId={brokerId}
          setBrokerId={setBrokerId}
          form={form}
          setForm={setForm}
          verInfo={verInfo}
          verBusy={verBusy}
          detectVersion={detectVersion}
          cands={cands}
          detecting={detecting}
          pickCandidate={pickCandidate}
          doDetect={doDetect}
          doTest={doTest}
          doAdd={doAdd}
          doLaunch={doLaunch}
          launching={launching}
          busy={busy}
          testRes={testRes}
        />

        <RuntimeMatrixCard runtimes={runtimes} loadRuntimes={loadRuntimes} />

        <DiagnosticsPanel diag={diag} diagBusy={diagBusy} loadDiagnostics={loadDiagnostics} />

        {/* 已配置连接列表 */}
        <ConnectionCard
          brokers={brokers}
          bsel={bsel}
          batchDeleteBrokers={batchDeleteBrokers}
          batchBusy={batchBusy}
          connecting={connecting}
          doConnect={doConnect}
          cancelConnect={cancelConnect}
          disconnect={disconnect}
          setActive={setActive}
          remove={remove}
          health={health}
          healthBusy={healthBusy}
          checkHealth={checkHealth}
          profile={profile}
        />
      </div>
    </div>
  );
}
