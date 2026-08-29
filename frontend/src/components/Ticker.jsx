// 指数跑马灯（通达信顶栏右侧）：订阅核心指数，滚动展示 名称/最新/涨跌幅 + 迷你走势。
// 链路强化（E2）：统一走 QuoteHub 引用计数多路复用（断线重连/心跳由 Hub 治理）。
// 指数集可配置：挂载时拉 /market/indices（后端 DEFAULT_INDICES）作为权威清单，
// 该端点此前是「死端点」，现正式接线 —— 增减指数只需改后端常量，前端自动跟随。
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { useQuotes } from "../lib/quoteHub.jsx";
import { navToQuote } from "../lib/nav.js";

// 默认指数集（与后端 DEFAULT_INDICES 对齐；后端返回后以服务端为准）
const DEFAULT_CODES = [
  "000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000688.SH", "899050.BJ",
];
const DEFAULT_NAMES = {
  "000001.SH": "上证", "399001.SZ": "深证", "399006.SZ": "创业",
  "000300.SH": "沪深300", "000688.SH": "科创50", "899050.BJ": "北证50",
};

function Sparkline({ points, up }) {
  if (!points || points.length < 2) return null;
  const vals = points.map((p) => Number(p.price)).filter((v) => v != null && !Number.isNaN(v));
  if (vals.length < 2) return null;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const w = 42, h = 14;
  const step = w / (vals.length - 1);
  const d = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`).join(" ");
  return (
    <svg className={`ticker-spark ${up ? "up" : "down"}`} width={w} height={h}
      viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <polyline points={d} fill="none" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

export default function Ticker() {
  const [codes, setCodes] = useState(DEFAULT_CODES);
  const [names, setNames] = useState(DEFAULT_NAMES);
  const [sparks, setSparks] = useState({});   // code -> [{price}]

  // 权威指数清单：接线 /market/indices（此前死端点），失败退回默认集
  useEffect(() => {
    let alive = true;
    api.marketIndices({ ttl: 30 })
      .then((r) => {
        if (!alive) return;
        const cs = (r.codes && r.codes.length) ? r.codes
          : (r.items || []).map((i) => i?.code).filter(Boolean);
        if (cs.length) {
          setCodes(cs);
          const nm = {};
          (r.items || []).forEach((it) => {
            if (it?.code) nm[it.code] = it.name || DEFAULT_NAMES[it.code] || it.code;
          });
          setNames(nm);
        }
      })
      .catch(() => { /* 用默认集 */ });
    return () => { alive = false; };
  }, []);

  // QuoteHub：声明式订阅 + 全局快照缓存（新订阅者立刻拿到最近报价，无闪白）
  const { quotes, state } = useQuotes(codes);

  // 迷你走势：每只指数拉一次当日分时（失败静默降级，不影响主信息）
  useEffect(() => {
    if (!codes.length) return;
    let alive = true;
    Promise.all(codes.map((c) =>
      api.marketMinutes({ code: c }).then((r) => [c, r.points || []]).catch(() => [c, []])))
      .then((res) => { if (alive) setSparks(Object.fromEntries(res)); })
      .catch(() => {});
    return () => { alive = false; };
    // 仅在指数集变化时重新拉取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [codes.join(",")]);

  const offline = state !== "connected";

  const renderItems = () => codes.map((c) => {
    const q = quotes[c];
    const last = q?.last != null ? Number(q.last) : null;
    let pct = q?.change_pct != null ? Number(q.change_pct) : null;
    if (pct == null && last != null) {
      const pre = q?.preClose != null ? Number(q.preClose)
        : (q?.lastClose != null ? Number(q.lastClose) : null);
      if (pre) pct = ((last - pre) / pre) * 100;
    }
    const cls = pct == null ? "" : pct >= 0 ? "up" : "down";
    const sp = sparks[c];
    const spUp = sp && sp.length >= 2
      ? Number(sp[sp.length - 1].price) >= Number(sp[0].price) : (pct == null ? true : pct >= 0);
    return (
      <span className={`ticker-item ${offline ? "dim" : ""}`} key={c}
        title={`${names[c] || c}指数（${state}），点击查看行情`}
        style={{ cursor: "pointer" }}
        onClick={() => navToQuote(c, { period: "tick" })}>
        <span className="ticker-name">{names[c] || c}</span>
        <span className="ticker-val">{last != null ? last.toFixed(2) : "—"}</span>
        <span className={cls}>{pct != null ? (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%" : "—"}</span>
        <Sparkline points={sp} up={spUp} />
      </span>
    );
  });

  return (
    <div className={`ticker ${offline ? "offline" : ""}`} aria-label={`指数行情（${state}）`}>
      <div className="ticker-track">
        <div className="ticker-group">{renderItems()}</div>
        <div className="ticker-group" aria-hidden="true">{renderItems()}</div>
      </div>
    </div>
  );
}
