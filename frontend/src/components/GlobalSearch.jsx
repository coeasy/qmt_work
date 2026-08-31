// 顶栏全局标的搜索 —— 接入后端 /market/search（本地名称缓存，零网络、不依赖券商）。
// 输入代码 / 中文名 / 拼音首字母 / 板块名即模糊匹配；结果带类型徽章与命中方式。
// 键盘导航：↑↓ 移动高亮、Enter 打开高亮项、Esc 关闭；聚焦无输入时展示最近搜索历史。
import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { navToQuote, navTo } from "../lib/nav.js";
import { badgeOf } from "../lib/instrument.js";

const DEBOUNCE_MS = 280;
const HISTORY_KEY = "qmt.gsearch.history";
const HISTORY_MAX = 8;

function loadHistory() {
  try {
    const v = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => x && x.code) : [];
  } catch { return []; }
}

function saveHistory(list) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX))); }
  catch { /* 隐私模式等场景静默降级 */ }
}

export default function GlobalSearch() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hi, setHi] = useState(0);            // 键盘高亮行（rows 索引）
  const [history, setHistory] = useState(loadHistory);
  const timer = useRef(null);
  const boxRef = useRef(null);

  // 输入防抖查询
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const kw = q.trim();
    if (!kw) { setRows([]); setOpen(false); return; }
    timer.current = setTimeout(async () => {
      setBusy(true);
      try {
        const list = await api.marketSearch(kw, 12);
        setRows(Array.isArray(list) ? list : []);
        setHi(0);
        setOpen(true);
      } catch { setRows([]); setOpen(false); }
      finally { setBusy(false); }
    }, DEBOUNCE_MS);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [q]);

  // 点击外部关闭
  useEffect(() => {
    const onDoc = (e) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  // 历史去重置顶（code 维度，名称随最新一次）
  const remember = (code, name) => {
    const next = [{ code, name: name || code }, ...history.filter((h) => h.code !== code)];
    setHistory(next);
    saveHistory(next);
  };

  // 板块 → 市场结构-板块行情；股票/ETF/指数/债券 → 行情分析（现状保持）
  const pick = (row) => {
    if (!row || !row.code) return;
    remember(row.code, row.name);
    setQ(""); setRows([]); setOpen(false);
    if (row.type === "board") {
      navTo("mktstructure", { params: { tab: "boards" } });
    } else {
      navToQuote(row.code);
    }
  };

  const onKey = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (open && rows.length) setHi((i) => Math.min(i + 1, rows.length - 1));
      else if (history.length) setOpen(true);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (open && rows.length) setHi((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      if (open && rows[hi]) pick(rows[hi]);
      else if (rows[0]) pick(rows[0]);
    } else if (e.key === "Escape") {
      setOpen(false); boxRef.current?.blur();
    }
  };

  const showHistory = open && !q.trim() && history.length > 0;
  const showDrop = (open && rows.length > 0) || showHistory;

  return (
    <div className="gsearch" ref={boxRef}>
      <input
        className="gsearch-input"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => { if (q.trim() && rows.length) setOpen(true); else if (history.length) setOpen(true); }}
        onKeyDown={onKey}
        placeholder="搜索 代码/名称/拼音"
        title="输入代码、中文名、拼音首字母或板块名；↑↓ 选择，回车打开"
      />
      {busy && <span className="gsearch-busy">…</span>}
      {showDrop && (
        <div className="gsearch-drop">
          {showHistory ? (
            <>
              <div className="gsearch-sec">最近搜索</div>
              {history.map((h) => (
                <div key={h.code} className="gsearch-item"
                  onClick={() => pick(h)}
                  onMouseDown={(e) => e.preventDefault()}>
                  <span className="gsearch-main">
                    <span className={`gbadge ${badgeOf(h.type).cls}`}>{badgeOf(h.type).label}</span>
                    <span className="code">{h.code}</span>
                    <span className="muted">{h.name}</span>
                  </span>
                </div>
              ))}
            </>
          ) : rows.map((r, i) => {
            const b = badgeOf(r.type);
            return (
              <div key={r.code}
                className={`gsearch-item${i === hi ? " active" : ""}`}
                onMouseEnter={() => setHi(i)}
                onClick={() => pick(r)}
                onMouseDown={(e) => e.preventDefault()}>
                <span className="gsearch-main">
                  <span className={`gbadge ${b.cls}`}>{b.label}</span>
                  <span className="code">{r.code}</span>
                  <span className="muted">{r.name}</span>
                </span>
                <span className="gsearch-meta">{r.match === "pinyin" ? "拼音" : r.exchange}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
