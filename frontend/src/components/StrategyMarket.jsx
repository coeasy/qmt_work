import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 策略市场（P1）
   DB 目录 + zip/json 导入导出，吸收 Rockyzsu/QMT 范式。
   发布本地策略到市场 / 安装市场策略 / 导出 zip 或 JSON / 从 zip 或 JSON 导入
*/

const TABS = [
  { key: "catalog", label: "目录" },
  { key: "publish", label: "发布" },
  { key: "export", label: "导出/导入" },
];

export default function StrategyMarket() {
  const [tab, setTab] = useState("catalog");
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);

  const [catalog, setCatalog] = useState([]);
  const [selected, setSelected] = useState(null);
  const [publishName, setPublishName] = useState("");
  const [publishDesc, setPublishDesc] = useState("");
  const [publishParams, setPublishParams] = useState("");
  const [publishFile, setPublishFile] = useState(null);
  const [importFile, setImportFile] = useState(null);
  const [importZipPath, setImportZipPath] = useState("");

  useEffect(() => { loadCatalog(); }, []);

  async function loadCatalog() {
    try {
      const [c, m] = await Promise.all([
        api.strategyCatalog().catch(() => []),
        api.strategyMarketList().catch(() => []),
      ]);
      setCatalog(Array.isArray(c) ? c : (m || []));
    } catch {}
  }

  async function install(id) {
    setLoading(true); setMsg(null);
    try {
      await api.strategyInstall({ id });
      setMsg({ ok: true, t: "策略已安装，可在策略库中查看" });
      loadCatalog();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function publishStrategy() {
    setLoading(true); setMsg(null);
    try {
      if (!publishName) throw new Error("请输入策略名称");
      const body = { name: publishName, description: publishDesc };
      if (publishParams) body.params = JSON.parse(publishParams);
      if (publishFile) body.file = publishFile;
      await api.strategyPublish(body);
      setMsg({ ok: true, t: "策略已发布到市场" });
      setPublishName(""); setPublishDesc(""); setPublishParams(""); setPublishFile(null);
      loadCatalog();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function exportJson() {
    setLoading(true); setMsg(null);
    try {
      const data = await api.strategyExportJson({});
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "strategies.json"; a.click();
      URL.revokeObjectURL(url);
      setMsg({ ok: true, t: "策略已导出为 JSON" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function exportZip() {
    setLoading(true); setMsg(null);
    try {
      const data = await api.strategyExport({});
      if (data && data.zip) {
        const url = URL.createObjectURL(data.zip);
        const a = document.createElement("a");
        a.href = url; a.download = "strategies.zip"; a.click();
        URL.revokeObjectURL(url);
      }
      setMsg({ ok: true, t: "策略已导出为 ZIP" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function importJson() {
    setLoading(true); setMsg(null);
    try {
      if (!importFile) throw new Error("请选择 JSON 文件");
      const text = await importFile.textContent;
      const data = JSON.parse(text);
      await api.strategyImportJson(data);
      setMsg({ ok: true, t: "JSON 策略导入成功" });
      setImportFile(null); loadCatalog();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function importZip() {
    setLoading(true); setMsg(null);
    try {
      if (!importZipPath.trim()) throw new Error("请输入 .zip bundle 的服务器路径");
      await api.strategyImport({ path: importZipPath.trim() });
      setMsg({ ok: true, t: "ZIP 策略包导入成功" });
      setImportZipPath(""); loadCatalog();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>策略市场</h2>
        <p>DB 目录 · 发布/安装策略 · zip/JSON 导入导出</p>
      </div>

      <div className="card">
        <div className="btn-group">
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      {tab === "catalog" && (
        <div className="card">
          {!catalog.length ? <Empty>暂无策略</Empty> : (
            <div className="strategy-grid">
              {catalog.map((s, i) => (
                <div key={i} className="strategy-card">
                  <h4>{s.name || `策略 ${i + 1}`}</h4>
                  <p>{s.description || s.desc || "无描述"}</p>
                  <div className="meta">
                    {s.author && <span>作者：{s.author}</span>}
                    {s.updated_at && <span>更新于 {s.updated_at}</span>}
                    {s.version && <span>v{s.version}</span>}
                  </div>
                  <button className="btn-sm" onClick={() => install(s.id)}>安装</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "publish" && (
        <div className="card" style={{ maxWidth: 560 }}>
          <div className="form-row">
            <div className="form-field">
              <label>策略名称</label>
              <input value={publishName} onChange={(e) => setPublishName(e.target.value)} placeholder="我的双均线策略" />
            </div>
            <div className="form-field">
              <label>描述</label>
              <textarea rows={3} value={publishDesc} onChange={(e) => setPublishDesc(e.target.value)}
                placeholder="策略逻辑说明…" />
            </div>
            <div className="form-field">
              <label>参数（JSON）</label>
              <textarea rows={3} value={publishParams} onChange={(e) => setPublishParams(e.target.value)}
                placeholder='{"ma_fast": 5, "ma_slow": 20}' />
            </div>
          </div>
          <button className="btn-primary" onClick={publishStrategy} disabled={loading}>
            {loading ? "发布中…" : "发布到市场"}
          </button>
        </div>
      )}

      {tab === "export" && (
        <div className="card" style={{ maxWidth: 480 }}>
          <div className="form-row">
            <div className="form-field">
              <label>导出方式</label>
              <div className="btn-group">
                <button className="btn-sm active" onClick={exportJson}>导出 JSON</button>
                <button className="btn-sm" onClick={exportZip}>导出 ZIP</button>
              </div>
            </div>
            <div className="form-field">
              <label>导入策略（JSON 文件）</label>
              <input type="file" accept=".json" onChange={(e) => setImportFile(e.target.files[0])} />
            </div>
            <button className="btn-sm" onClick={importJson} disabled={loading}>
              {loading ? "导入中…" : "导入"}
            </button>
          </div>
          <div className="form-row" style={{ marginTop: 10 }}>
            <div className="form-field">
              <label>导入 ZIP 策略包（服务器路径）</label>
              <input value={importZipPath} onChange={(e) => setImportZipPath(e.target.value)}
                     placeholder="如 C:\strategy_bundle_1234.zip" />
            </div>
            <button className="btn-sm" onClick={importZip} disabled={loading}>
              {loading ? "导入中…" : "导入 ZIP"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "32px 24px", textAlign: "center", color: "#556" }}>{children}</div>;
}