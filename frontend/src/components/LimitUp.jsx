import { useState } from "react";
import { api } from "../api.js";
import { useServerEvents } from "../hooks/useSystemWS.js";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
import { fmtAmount, fmtLimitDur } from "../lib/format.js";
import { quickTradeNavigate } from "../lib/trade.js";
import ConfirmTradeModal from "./ui/ConfirmTradeModal.jsx";

// 涨停板 / 打板助手：
//  - 涨停板：真实行情扫描板块内涨停（或接近涨停）个股，列出最新数据，点击可快速下单
//  - 打板监控：股票池 + 三因子触发（涨停价/时间窗/tick涨幅）+ 可选自动买入
const SECTORS = ["沪深A股", "沪深京A股", "创业板", "科创板", "上证50", "沪深300", "中证500", "中证1000"];

export default function LimitUp() {
  const [view, setView] = useState("board"); // board | monitor

  // ---------- 涨停板（盘口扫描） ----------
  const [board, setBoard] = useState([]);
  const [meta, setMeta] = useState({});
  const [sector, setSector] = useState("沪深A股");
  const [onlyLimit, setOnlyLimit] = useState(true);
  const [minPct, setMinPct] = useState(9.5);
  const [boardErr, setBoardErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [breadth, setBreadth] = useState(null);
  const [breadthErr, setBreadthErr] = useState("");

  async function loadBoard() {
    setLoading(true);
    try {
      const data = await api.marketLimitup({
        sector, min_pct: minPct, only_limit: onlyLimit, limit: 200, sort: "change",
      });
      setBoard(data.rows || []);
      setMeta({ sector: data.sector, count: data.count });
      setBoardErr("");
    } catch (e) { setBoardErr(e.message); }
    finally { setLoading(false); }
  }
  async function loadBreadth() {
    try {
      const d = await api.get("/market/breadth");
      setBreadth(d);
      setBreadthErr("");
    } catch (e) { setBreadthErr(e.message); }
  }
  // G5：仅在「涨停板」视图下轮询，且后台 Tab 停表（delay=0 + immediate:false = 完全静默）
  useActiveInterval(
    () => { loadBoard(); loadBreadth(); },
    view === "board" ? 30000 : 0,
    [view, sector, onlyLimit, minPct],
    { immediate: view === "board" },
  );

  function quickTrade(stock, direction) {
    // 切到「交易」Hub 的手动交易子页，把代码 + 参考价 + 买卖方向带过去快速下单：
    // 买入优先带涨停价（挂涨停抢封板），卖出优先带最新价（避免用涨停价限卖提交失败）。
    const price = Number(stock.limit_price) > 0
      ? (direction === "buy" ? Number(stock.limit_price) : Number(stock.last || stock.limit_price))
      : (Number(stock.last) || undefined);
    quickTradeNavigate(stock.code, price, direction);
  }

  // ---------- 打板监控（原有） ----------
  const [st, setSt] = useState(null);
  const [code, setCode] = useState("");
  const [err, setErr] = useState("");
  const [confirm, setConfirm] = useState(null);   // T3 自动买入二次确认
  const [params, setParams] = useState({
    limit_pct: 0.1, cutoff: "10:00", min_rise: 0.03,
    buy_volume: 0, do_trade: false, interval: 2,
  });

  async function load() {
    try { setSt(await api.limitupStatus()); } catch (e) { setErr(e.message); }
  }
  useActiveInterval(load, 30000);
  // P2-2：limitup/limitup_order 事件驱动近实时刷新（触发/自动买入时立即更新），保留 30s 兜底轮询
  useServerEvents(["limitup"], () => { loadBoard(); loadBreadth(); load(); });

  async function add() {
    const c = code.trim();
    if (!c) return;
    try { await api.limitupPoolAdd(c); setCode(""); setErr(""); load(); }
    catch (e) { setErr(e.message); }
  }
  async function remove(c) {
    try { await api.limitupPoolRemove(c); load(); } catch (e) { setErr(e.message); }
  }
  async function doStart() {
    try { await api.limitupStart(params); setErr(""); load(); }
    catch (e) { setErr(e.message); }
  }
  async function start() {
    // T3：打板监控含「自动买入」开关 → 开启即可能真实下单，必须二次确认
    if (params.do_trade) {
      setConfirm({
        title: "启动打板监控（含自动买入）",
        rows: [
          { k: "股票池", v: `${(st?.pool || []).length} 只` },
          { k: "触发价", v: `${params.price_pct}%` },
          { k: "时间窗", v: `${params.window_min} 分钟` },
          { k: "买入量", v: `${params.buy_volume} 股` },
          { k: "检查间隔", v: `${params.interval}s` },
        ],
        note: "「自动买入」已开启：触发条件满足时将自动真实提交买入委托（过风控）。",
        confirmText: "确认启动监控",
        onConfirm: async () => { await doStart(); setConfirm(null); },
      });
    } else {
      await doStart();
    }
  }
  async function stop() {
    try { await api.limitupStop(); load(); } catch (e) { setErr(e.message); }
  }
  async function resetTrig() {
    try { await api.limitupReset(); load(); } catch (e) { setErr(e.message); }
  }

  const pool = st?.pool || [];
  const events = st?.events || [];

  return (
    <div>
      <h2 className="page-title">涨停板 · 打板助手</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={() => setView("board")}
                style={view === "board" ? { background: "var(--accent)", color: "#fff" } : {}}>涨停板</button>
        <button onClick={() => setView("monitor")}
                style={view === "monitor" ? { background: "var(--accent)", color: "#fff" } : {}}>打板监控</button>
      </div>

      {view === "board" ? (
        <div>
          <p className="page-sub">实时行情扫描板块内涨停（或接近涨停）个股，列出最新盘口数据；点击「买入/卖出」可带入下单页快速下单。</p>

          <div className="card" style={{ marginBottom: 16 }}>
            {breadthErr ? (
              <p className="muted">涨跌停数量暂不可用：{breadthErr}。请到「券商连接」页连接券商后自动恢复。</p>
            ) : breadth?.overall ? (
              <>
                <h3>
                  大盘涨跌停 {(breadth.overall.limit_up ?? 0)} 涨停 / {(breadth.overall.limit_down ?? 0)} 跌停
                  <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}> （{(breadth.overall.total ?? 0)} 只 · {breadth.ts}）</span>
                </h3>
                <div className="grid grid-4">
                  <div className="stat-value">{breadth.overall.limit_up ?? 0}<span className="muted" style={{ fontSize: 12 }}> 涨停</span></div>
                  <div className="stat-value">{breadth.overall.limit_down ?? 0}<span className="muted" style={{ fontSize: 12 }}> 跌停</span></div>
                  <div className="stat-value up">↑{breadth.overall.up ?? 0}</div>
                  <div className="stat-value down">↓{breadth.overall.down ?? 0}</div>
                </div>
                <BreadthTable data={breadth.boards} kind="board" />
                <BreadthTable data={breadth.indexes} kind="index" />
                <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
                  涨跌停数量按板块默认幅度估算（ST/新股不识别名称，按代码区间判幅）。
                </p>
              </>
            ) : (
              <p className="muted">涨跌停数量加载中或当前无数据……</p>
            )}
          </div>

          <div className="card">
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <label>板块</label>
              <select value={sector} onChange={(e) => setSector(e.target.value)}>
                {SECTORS.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <label>接近涨停阈值(%)</label>
              <input type="number" step="0.1" value={minPct}
                     onChange={(e) => setMinPct(+e.target.value)} style={{ width: 90 }} />
              <label><input type="checkbox" checked={onlyLimit}
                            onChange={(e) => setOnlyLimit(e.target.checked)} /> 仅看涨停</label>
              <button onClick={loadBoard} disabled={loading}>{loading ? "刷新中…" : "刷新"}</button>
              <span className="muted">事件驱动刷新 · 兜底 30s</span>
            </div>
            {boardErr && <div className="toast err">{boardErr}</div>}
            {!boardErr && (
              <p className="muted" style={{ marginTop: 8 }}>
                板块：{meta.sector || sector} · 命中 <b>{meta.count ?? board.length}</b> 只
              </p>
            )}
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            {boardErr ? (
              <p className="muted">未能获取涨停数据（未连接券商或接口异常）。请到「券商连接」页添加并连接券商后重试。</p>
            ) : board.length === 0 ? (
              <p className="muted">当前板块无涨停（或接近涨停）个股。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th>
                    <th>涨停价</th><th>封单(手)</th><th>封单额</th><th>成交额</th><th>涨停时长</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {board.map((r) => (
                    <tr key={r.code}>
                      <td className="code">{r.code}</td>
                      <td>{r.name}</td>
                      <td className="up">{r.last}</td>
                      <td className="up">+{r.change_pct}%</td>
                      <td>{r.limit_price}</td>
                      <td>{r.bid_vol != null ? r.bid_vol.toLocaleString() : "—"}</td>
                      <td>{fmtAmount(r.bid_amount)}</td>
                      <td>{fmtAmount(r.amount)}</td>
                      <td>{fmtLimitDur(r.limit_seconds)}</td>
                      <td className="row" style={{ gap: 6 }}>
                        <button className="btn-sm buy" onClick={() => quickTrade(r, "buy")}>买入</button>
                        <button className="btn-sm sell" onClick={() => quickTrade(r, "sell")}>卖出</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : (
        <div>
          <p className="page-sub">借鉴 QMT-QuantLimit：实时轮询真实行情，last≥涨停价 且 时间≤截止 且 近25 tick 涨幅达标即触发；可自动涨停价买入（走风控）</p>
          {err && <div className="toast err">{err}</div>}

          <div className="grid grid-2">
            <div className="card">
              <h3>股票池（{pool.length}）</h3>
              <div className="row">
                <input style={{ flex: 1 }} placeholder="代码，如 600519.SH" value={code}
                       onChange={(e) => setCode(e.target.value)}
                       onKeyDown={(e) => e.key === "Enter" && add()} />
                <button onClick={add}>添加</button>
              </div>
              {pool.length === 0 ? <p className="muted">暂无监控标的</p> : (
                <table>
                  <thead><tr><th>代码</th><th>名称</th><th></th></tr></thead>
                  <tbody>
                    {pool.map((p) => (
                      <tr key={p.code}>
                        <td className="code">{p.code}</td><td>{p.name}</td>
                        <td><button onClick={() => remove(p.code)}>移除</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="card">
              <h3>触发参数与状态</h3>
              <div className="row">
                <label>涨停幅度</label>
                <input type="number" step="0.01" value={params.limit_pct}
                       onChange={(e) => setParams({ ...params, limit_pct: +e.target.value })} />
                <label>截止时间</label>
                <input type="text" value={params.cutoff}
                       onChange={(e) => setParams({ ...params, cutoff: e.target.value })} />
              </div>
              <div className="row">
                <label>tick涨幅</label>
                <input type="number" step="0.01" value={params.min_rise}
                       onChange={(e) => setParams({ ...params, min_rise: +e.target.value })} />
                <label>轮询秒</label>
                <input type="number" step="0.5" value={params.interval}
                       onChange={(e) => setParams({ ...params, interval: +e.target.value })} />
              </div>
              <div className="row">
                <label>买入股数</label>
                <input type="number" step="100" value={params.buy_volume}
                       onChange={(e) => setParams({ ...params, buy_volume: +e.target.value })} />
                <label><input type="checkbox" checked={params.do_trade}
                              onChange={(e) => setParams({ ...params, do_trade: e.target.checked })} /> 自动买入</label>
              </div>
              <div className="btn-row">
                <button onClick={start} disabled={st?.running}>启动监控</button>
                <button onClick={stop} disabled={!st?.running}>停止</button>
                <button onClick={resetTrig}>重置触发</button>
              </div>
              <p className="muted">
                状态：<span className={`tag ${st?.running ? "run" : "fail"}`}>{st?.running ? "监控中" : "已停止"}</span>
                {" "}已触发：<b>{st?.total_triggered ?? 0}</b>
                {st?.do_trade && <span className="tag warn">自动买入已开启</span>}
              </p>
            </div>
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h3>触发事件（最新 50 条）</h3>
            {events.length === 0 ? <p className="muted">暂无触发事件</p> : (
              <table>
                <thead><tr><th>时间</th><th>代码</th><th>涨停价</th><th>触发价</th></tr></thead>
                <tbody>
                  {events.slice().reverse().map((e, i) => (
                    <tr key={i}>
                      <td>{e.ts}</td><td className="code">{e.code}</td>
                      <td>{e.limit}</td><td className="up">{e.price}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      <ConfirmTradeModal pending={confirm} busy={false} onClose={() => setConfirm(null)} risk="high" />
    </div>
  );
}

// 板块 / 指数 涨跌停家数小表
function BreadthTable({ data, kind }) {
  if (!data || data.length === 0) return null;
  return (
    <table style={{ marginTop: 10 }}>
      <thead>
        <tr><th>{kind === "board" ? "板块" : "指数"}</th><th>涨停</th><th>跌停</th><th>总数</th></tr>
      </thead>
      <tbody>
        {data.map((b) => (
          <tr key={b.name}>
            <td>{b.name}</td>
            <td className="up">{b.limit_up ?? 0}</td>
            <td className="down">{b.limit_down ?? 0}</td>
            <td className="muted">{b.total ?? 0}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
