import { useState } from "react";
import { api } from "../api.js";

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

  return (
    <div>
      <h2 className="page-title">行情工具</h2>
      <p className="page-sub">
        补齐行情类孤儿接口的前端入口：单票实时报价、K 线缓存查看与清理、手动触发行情抓取落库。
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
              {["1d", "1w", "1mon", "1m", "5m", "15m"].map((p) => (
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
