import { useState } from "react";
import { api } from "../api.js";
import { t } from "../lib/i18n.js";

/* 行情工具（补齐 /market/quote、/market/crawl、/market/kline/cache 前端入口）
   - 实时报价：单票最新快照（最新价 / 涨跌幅 / 成交量 / 五档）
   - K 线缓存：查看缓存统计、按需清理（code/period 精确清理）
   - 手动抓取：触发行情落库（真实 K 线写入 market_cache） */

function changePctColor(v) {
  // A 股惯例：涨红跌绿
  if (v == null || isNaN(Number(v))) return "#9bb";
  const n = Number(v);
  return n > 0 ? "#f0413e" : n < 0 ? "#1bbf86" : "#9bb";
}

export default function MarketTools() {
  const [toast, setToast] = useState(null);

  // 实时报价
  const [qCode, setQCode] = useState("600519.SH");
  const [qConn, setQConn] = useState("");
  const [quote, setQuote] = useState(null);
  const [qBusy, setQBusy] = useState(false);

  // K 线缓存
  const [cache, setCache] = useState(null);
  const [cCode, setCCode] = useState("");
  const [cPeriod, setCPeriod] = useState("");
  const [cBusy, setCBusy] = useState(false);

  // 手动抓取
  const [crawlCodes, setCrawlCodes] = useState("600519.SH");
  const [crawlDays, setCrawlDays] = useState(30);
  const [crawlPeriod, setCrawlPeriod] = useState("1d");
  const [crawlAdjust, setCrawlAdjust] = useState("");
  const [crawlMsg, setCrawlMsg] = useState(null);
  const [crawlBusy, setCrawlBusy] = useState(false);

  // 定时更新
  const [sync, setSync] = useState(null);
  const [syncBusy, setSyncBusy] = useState(false);

  async function queryQuote() {
    setQBusy(true); setToast(null); setQuote(null);
    try {
      const r = await api.marketQuote({ code: qCode.trim(), conn_id: qConn.trim() });
      setQuote(r);
    } catch (e) { setToast({ ok: false, t: e.message }); }
    finally { setQBusy(false); }
  }

  async function loadCache() {
    setCBusy(true); setToast(null);
    try {
      setCache(await api.klineCacheStats());
      setSync(await api.klineSyncStatus());
    } catch (e) { setToast({ ok: false, t: e.message }); }
    finally { setCBusy(false); }
  }
  async function clearCache() {
    setCBusy(true); setToast(null);
    try {
      const r = await api.klineCacheClear(cCode.trim(), cPeriod.trim());
      setToast({ ok: true, t: `已清理 K 线缓存 ${r.deleted} 条` });
      setCache(null);
    } catch (e) { setToast({ ok: false, t: e.message }); }
    finally { setCBusy(false); }
  }

  // 定时更新开关/时间：写 runtime_config（market.sync.enabled / market.sync.time），热更新
  async function saveSync(next) {
    setSyncBusy(true); setToast(null);
    try {
      const changed = { ...next };
      if (changed.enabled === sync.enabled) delete changed.enabled;
      if (changed.sync_time === sync.sync_time) delete changed.sync_time;
      if (Object.keys(changed).length) {
        const map = { enabled: "market.sync.enabled", sync_time: "market.sync.time" };
        const body = {};
        for (const k of Object.keys(changed)) body[map[k]] = changed[k];
        await api.putRuntimeConfig(body);
      }
      const s = await api.klineSyncStatus();
      setSync(s);
      setToast({ ok: true, t: `定时更新已${s.enabled ? "开启" : "关闭"}${s.enabled ? `，每日 ${s.sync_time} 收盘后刷新` : ""}` });
    } catch (e) { setToast({ ok: false, t: e.message }); }
    finally { setSyncBusy(false); }
  }

  async function doCrawl() {
    setCrawlBusy(true); setCrawlMsg(null);
    const codes = crawlCodes.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    try {
      const r = await api.marketCrawl({
        conn_id: qConn.trim(), codes, days: Number(crawlDays) || 30,
        period: crawlPeriod, adjust: crawlAdjust,
      });
      setCrawlMsg({ ok: true, t: `抓取完成：标的 ${r.crawled_codes?.length || 0} 个，落库 ${r.bars_inserted || 0} 根 K 线（${crawlPeriod}${crawlAdjust ? "/" + crawlAdjust : ""}，写入 kline_cache/kline_archive）` });
    } catch (e) { setCrawlMsg({ ok: false, t: e.message }); }
    finally { setCrawlBusy(false); }
  }

  // K 线导出 / 全量同步 / 统一导出 / REST 订阅（孤儿端点补齐，2026-09 第三期）
  const [expDir, setExpDir] = useState("");
  const [expCodes, setExpCodes] = useState("");
  const [expPeriod, setExpPeriod] = useState("1d");
  const [expCount, setExpCount] = useState(250);
  const [expFmt, setExpFmt] = useState("csv");
  const [expRefresh, setExpRefresh] = useState(false);
  const [expMsg, setExpMsg] = useState(null);
  const [expBusy, setExpBusy] = useState(false);

  const [synDir, setSynDir] = useState("");
  const [synSector, setSynSector] = useState("沪深A股");
  const [synPeriods, setSynPeriods] = useState("1d,1w");
  const [synLimit, setSynLimit] = useState(0);
  const [synMsg, setSynMsg] = useState(null);
  const [synBusy, setSynBusy] = useState(false);

  // 统一导出：/market/export 契约为 {format, filename, rows:[dict]}（columns 缺省时由首行推导）。
  const [mexFilename, setMexFilename] = useState("export");
  const [mexFmt, setMexFmt] = useState("csv");
  const [mexRows, setMexRows] = useState("");
  const [mexMsg, setMexMsg] = useState(null);
  const [mexBusy, setMexBusy] = useState(false);

  const [subCodes, setSubCodes] = useState("");
  const [subMsg, setSubMsg] = useState(null);
  const [subBusy, setSubBusy] = useState(false);

  async function doKlineExport() {
    setExpBusy(true); setExpMsg(null);
    try {
      const body = { dest_dir: expDir.trim(), format: expFmt, count: Number(expCount) || 0, refresh: expRefresh };
      const codes = expCodes.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      if (codes.length) body.codes = codes;
      if (expPeriod) body.period = expPeriod;
      const r = await api.klineExport(body);
      setExpMsg({ ok: true, t: `导出完成：${r.exported} 个序列 / ${r.rows} 行 → ${r.dest_dir}` });
    } catch (e) { setExpMsg({ ok: false, t: e.message }); }
    finally { setExpBusy(false); }
  }

  async function doKlineSync() {
    setSynBusy(true); setSynMsg(null);
    try {
      const body = {
        dest_dir: synDir.trim(), sector: synSector.trim() || "沪深A股",
        periods: synPeriods.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        count: 250, limit: Number(synLimit) || 0, conn_id: qConn.trim(),
      };
      const r = await api.klineSync(body);
      setSynMsg({ ok: true, t: `同步完成：标的 ${r.codes_total} 个，导出 ${r.files_count} 个文件 / ${r.rows} 行${r.errors?.length ? `，失败 ${r.errors.length}` : ""} → ${r.dest_dir}` });
    } catch (e) { setSynMsg({ ok: false, t: e.message }); }
    finally { setSynBusy(false); }
  }

  async function doMarketExport() {
    setMexBusy(true); setMexMsg(null);
    try {
      let rows;
      try { rows = JSON.parse(mexRows || "[]"); }
      catch { setMexMsg({ ok: false, t: "rows 不是合法 JSON 数组" }); return; }
      if (!Array.isArray(rows) || !rows.length) {
        setMexMsg({ ok: false, t: "rows 须为非空对象数组" }); return;
      }
      const r = await api.marketExport({
        format: mexFmt, filename: mexFilename.trim() || "export", rows,
      });
      if (r?.content != null) {
        // csv/json：浏览器端直接落盘下载
        const blob = new Blob([r.content], { type: "text/plain;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = r.filename || "export";
        a.click();
        URL.revokeObjectURL(a.href);
        setMexMsg({ ok: true, t: `导出完成：${r.filename}（${r.count} 行，已下载）` });
      } else {
        setMexMsg({ ok: true, t: `导出完成：${r?.path || "（见后端返回）"}（${r?.count ?? ""} 行）` });
      }
    } catch (e) { setMexMsg({ ok: false, t: e.message }); }
    finally { setMexBusy(false); }
  }

  async function doSyncSubscribe() {
    setSubBusy(true); setSubMsg(null);
    try {
      const codes = subCodes.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      const r = await api.syncSubscribe(codes);
      setSubMsg({ ok: true, t: `已订阅 ${r.subscribed?.length || 0} 个代码（REST 入口，与 WS 共享引用计数）` });
    } catch (e) { setSubMsg({ ok: false, t: e.message }); }
    finally { setSubBusy(false); }
  }

  return (
    <div>
      <h2 className="page-title">{t(`page.markettools.title`)}</h2>
      <p className="page-sub">
        行情数据工具：单票实时报价、K 线缓存、手动抓取、K 线导出/全量同步、统一导出与 REST 订阅。
        未连接券商时统一返回 503 引导，不返回任何假数据。
      </p>
      {toast && <div className={`toast ${toast.ok ? "ok" : "err"}`}>{toast.t}</div>}

      <div className="grid grid-3">
        {/* 实时报价 */}
        <div className="card">
          <h3>实时报价</h3>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="代码 600519.SH"
                   value={qCode} onChange={(e) => setQCode(e.target.value)} />
          </div>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="连接ID（留空=活跃连接）"
                   value={qConn} onChange={(e) => setQConn(e.target.value)} />
            <button onClick={queryQuote} disabled={qBusy || !qCode.trim()}>
              {qBusy ? "查询中…" : "查询"}
            </button>
          </div>
          {quote && (
            <div style={{ marginTop: 10, fontSize: 13 }}>
              <QuoteSnapshot q={quote} />
            </div>
          )}
        </div>

        {/* K 线缓存 */}
        <div className="card">
          <h3>K 线缓存</h3>
          <div className="btn-row">
            <button onClick={loadCache} disabled={cBusy}>查看统计</button>
            <button className="btn-danger-sm" onClick={clearCache} disabled={cBusy}>清理缓存</button>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <input style={{ flex: 1 }} placeholder="指定代码（可选）"
                   value={cCode} onChange={(e) => setCCode(e.target.value)} />
          </div>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="周期（可选，如 1d）"
                   value={cPeriod} onChange={(e) => setCPeriod(e.target.value)} />
            <span className="muted" style={{ fontSize: 11 }}>清理时按填写范围精确清理</span>
          </div>
          {cache && (
            <div style={{ marginTop: 8, fontSize: 12, lineHeight: 1.8 }}>
              <CacheStats cache={cache} />
              <div className="row" style={{ justifyContent: "space-between", gap: 8, borderTop: "1px solid #2a3550", marginTop: 8, paddingTop: 6 }}>
                <span className="muted">定时更新（每日收盘后刷新今年热数据）</span>
                {sync && (
                  <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <input type="checkbox" checked={!!sync.enabled}
                           onChange={(e) => saveSync({ enabled: e.target.checked })} disabled={syncBusy} />
                    <input
                      style={{ width: 56 }} type="time" value={sync.sync_time}
                      onBlur={(e) => saveSync({ sync_time: e.target.value })} disabled={syncBusy} />
                  </label>
                )}
              </div>
              {sync?.last_run && (
                <div className="muted" style={{ fontSize: 11 }}>
                  最近运行：{sync.last_run.date}（标的 {sync.last_run.codes}，成功 {sync.last_run.ok} / 失败 {sync.last_run.fail}）
                </div>
              )}
            </div>
          )}
        </div>

        {/* 手动抓取 */}
        <div className="card">
          <h3>手动抓取行情</h3>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="代码，逗号分隔"
                   value={crawlCodes} onChange={(e) => setCrawlCodes(e.target.value)} />
          </div>
          <div className="row">
            <label style={{ width: 60 }}>天数</label>
            <input style={{ width: 72 }} value={crawlDays}
                   onChange={(e) => setCrawlDays(e.target.value)} />
            <select value={crawlPeriod} onChange={(e) => setCrawlPeriod(e.target.value)} style={{ flex: 1 }}>
              {["1d", "1w", "1mo", "1m", "5m", "15m"].map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
            <select value={crawlAdjust} onChange={(e) => setCrawlAdjust(e.target.value)} style={{ flex: 1 }}>
              <option value="">原始</option>
              <option value="qfq">前复权</option>
              <option value="hfq">后复权</option>
            </select>
            <button onClick={doCrawl} disabled={crawlBusy || !crawlCodes.trim()}>
              {crawlBusy ? "抓取中…" : "抓取落库"}
            </button>
          </div>
          <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            经真实券商拉取 K 线写入本地 kline_cache（今年）/kline_archive（去年以前），供图表与回测使用；后端抓取时优先复用已缓存的复权标记。
          </p>
          {crawlMsg && <div className={`toast ${crawlMsg.ok ? "ok" : "err"}`} style={{ marginTop: 8 }}>{crawlMsg.t}</div>}
        </div>
      </div>

      {/* K 线导出 / 全量同步 / 统一导出 / REST 订阅（孤儿端点补齐） */}
      <div className="grid grid-3" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>K 线导出</h3>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="导出目录 dest_dir（必填，如 D:/kline_data）"
                   value={expDir} onChange={(e) => setExpDir(e.target.value)} />
          </div>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="代码列表（可选，逗号分隔，空=全部序列）"
                   value={expCodes} onChange={(e) => setExpCodes(e.target.value)} />
          </div>
          <div className="row">
            <input style={{ width: 72 }} value={expPeriod}
                   onChange={(e) => setExpPeriod(e.target.value)} title="周期（可选）" />
            <input style={{ width: 72 }} value={expCount}
                   onChange={(e) => setExpCount(e.target.value)} title="每序列根数（0=全部）" />
            <select value={expFmt} onChange={(e) => setExpFmt(e.target.value)} style={{ flex: 1 }}>
              {["csv", "json", "feather"].map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <input type="checkbox" checked={expRefresh} onChange={(e) => setExpRefresh(e.target.checked)} />回源刷新
            </label>
          </div>
          <div className="btn-row">
            <button onClick={doKlineExport} disabled={expBusy || !expDir.trim()}>
              {expBusy ? "导出中…" : "导出"}
            </button>
          </div>
          <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            快速导出完全离线（读本地 kline_cache）；勾选「回源刷新」先向券商拉最新再导出（需连接）。
          </p>
          {expMsg && <div className={`toast ${expMsg.ok ? "ok" : "err"}`} style={{ marginTop: 8 }}>{expMsg.t}</div>}
        </div>

        <div className="card">
          <h3>K 线全量同步</h3>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="同步目录 dest_dir（必填）"
                   value={synDir} onChange={(e) => setSynDir(e.target.value)} />
          </div>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="板块（默认 沪深A股；留空 codes 时使用）"
                   value={synSector} onChange={(e) => setSynSector(e.target.value)} />
          </div>
          <div className="row">
            <input style={{ flex: 1 }} value={synPeriods}
                   onChange={(e) => setSynPeriods(e.target.value)} title="周期，逗号分隔" />
            <input style={{ width: 72 }} value={synLimit}
                   onChange={(e) => setSynLimit(e.target.value)} title="最大标的数（0=全部）" />
            <button onClick={doKlineSync} disabled={synBusy || !synDir.trim()}>
              {synBusy ? "同步中…" : "同步"}
            </button>
          </div>
          <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            逐只回源券商拉最新 K 线写缓存并导出到目录；原生周线缺失时用日线聚合兜底（不伪造）。全市场同步耗时较长，建议先设 limit 试跑。
          </p>
          {synMsg && <div className={`toast ${synMsg.ok ? "ok" : "err"}`} style={{ marginTop: 8 }}>{synMsg.t}</div>}
        </div>

        <div className="card">
          <h3>统一导出 / REST 订阅</h3>
          <div className="row">
            <input style={{ flex: 1 }} placeholder="文件名（默认 export）"
                   value={mexFilename} onChange={(e) => setMexFilename(e.target.value)} />
            <select value={mexFmt} onChange={(e) => setMexFmt(e.target.value)}>
              {["csv", "json", "xlsx"].map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <button onClick={doMarketExport} disabled={mexBusy}>
              {mexBusy ? "导出中…" : "导出"}
            </button>
          </div>
          <div className="row">
            <textarea style={{ flex: 1, minHeight: 56, fontFamily: "monospace", fontSize: 11 }}
              placeholder='行数据 JSON 数组，如 [{"code":"600519.SH","last":1700}]'
              value={mexRows} onChange={(e) => setMexRows(e.target.value)} />
          </div>
          <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            csv/json 直接下载；xlsx 由服务端落盘后返回路径（openpyxl 缺失时回退 CSV 内容）。
          </p>
          {mexMsg && <div className={`toast ${mexMsg.ok ? "ok" : "err"}`} style={{ marginTop: 8 }}>{mexMsg.t}</div>}
          <div className="row" style={{ borderTop: "1px solid #2a3550", marginTop: 10, paddingTop: 8 }}>
            <input style={{ flex: 1 }} placeholder="订阅代码，逗号分隔（REST 行情订阅）"
                   value={subCodes} onChange={(e) => setSubCodes(e.target.value)} />
            <button onClick={doSyncSubscribe} disabled={subBusy || !subCodes.trim()}>
              {subBusy ? "订阅中…" : "订阅"}
            </button>
          </div>
          {subMsg && <div className={`toast ${subMsg.ok ? "ok" : "err"}`} style={{ marginTop: 8 }}>{subMsg.t}</div>}
        </div>
      </div>
    </div>
  );
}

function CacheStats({ cache }) {
  if (!cache || typeof cache !== "object") return null;
  const rows = [
    ["总行数", cache.rows ?? "—"],
    ["热表(今年)", cache.hot_rows ?? "—"],
    ["归档(去年及以前)", cache.archive_rows ?? "—"],
    ["序列数", cache.series ?? "—"],
    ["今年分区", cache.current_year ?? "—"],
    ["命中率", cache.hit_rate != null ? `${(Number(cache.hit_rate) * 100).toFixed(1)}%` : "—"],
  ];
  return (
    <div>
      {rows.map(([k, v]) => (
        <div key={k} className="row" style={{ justifyContent: "space-between", gap: 8 }}>
          <span className="muted">{k}</span><span>{v}</span>
        </div>
      ))}
    </div>
  );
}

function QuoteSnapshot({ q }) {
  if (Array.isArray(q)) {
    return <pre style={{ fontSize: 11, maxHeight: 220, overflow: "auto" }}>{JSON.stringify(q, null, 2)}</pre>;
  }
  if (typeof q !== "object" || q === null) {
    return <pre style={{ fontSize: 11 }}>{String(q)}</pre>;
  }
  const last = q.last_price ?? q.last ?? q.price;
  const pct = q.change_pct ?? q.pct ?? q.change_percent;
  const vol = q.volume ?? q.vol;
  const known = [
    ["最新价", last != null ? Number(last).toFixed(2) : "—"],
    ["涨跌幅", pct != null ? `${Number(pct).toFixed(2)}%` : "—"],
    ["成交量", vol != null ? Number(vol).toLocaleString() : "—"],
    ["成交额", q.amount != null ? Number(q.amount).toLocaleString() : "—"],
  ];
  return (
    <div>
      <div style={{ display: "flex", gap: 16, marginBottom: 8, alignItems: "baseline" }}>
        {known.map(([k, v]) => (
          <div key={k}>
            <div className="muted" style={{ fontSize: 11 }}>{k}</div>
            <div style={{ fontSize: 15, fontWeight: 600, color: k === "涨跌幅" ? changePctColor(pct) : "#dfe" }}>{v}</div>
          </div>
        ))}
      </div>
      <details>
        <summary style={{ cursor: "pointer", fontSize: 12, color: "#8aa0c4" }}>原始字段（{(() => { try { return Object.keys(q).length; } catch { return 0; } })()}）</summary>
        <pre style={{ fontSize: 11, maxHeight: 220, overflow: "auto", marginTop: 6 }}>{JSON.stringify(q, null, 2)}</pre>
      </details>
    </div>
  );
}
