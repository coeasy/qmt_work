// 顶栏全局股票搜索 —— 接入后端 /market/search（本地名称缓存，零网络、不依赖券商）。
// 输入代码 / 中文名即模糊匹配，选中条目通过 navToQuote 直接打开行情分析。
import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { navToQuote } from "../lib/nav.js";

const DEBOUNCE_MS = 280;

export default function GlobalSearch() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
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

  const pick = (code) => {
    setQ(""); setRows([]); setOpen(false);
    navToQuote(code);
  };

  const onKey = (e) => {
    if (e.key === "Enter") { if (rows[0]) pick(rows[0].code); }
    else if (e.key === "Escape") { setOpen(false); boxRef.current?.blur(); }
  };

  return (
    <div className="gsearch" ref={boxRef} style={{ position: "relative", marginRight: 6 }}>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => { if (q.trim() && rows.length) setOpen(true); }}
        onKeyDown={onKey}
        placeholder="搜索 代码/名称"
        title="输入股票代码或中文名，回车打开行情"
        style={{
          width: 170, padding: "4px 10px", borderRadius: 6,
          border: "1px solid #334155", background: "#0d1526", color: "#e2e8f0",
          fontSize: 12, outline: "none",
        }}
      />
      {busy && <span style={{ position: "absolute", right: 8, top: 6, color: "#64748b", fontSize: 11 }}>…</span>}
      {open && rows.length > 0 && (
        <div style={{
          position: "absolute", top: "100%", left: 0, right: 0, marginTop: 4,
          background: "#111a2e", border: "1px solid #334155", borderRadius: 8,
          boxShadow: "0 8px 24px rgba(0,0,0,.4)", zIndex: 200, overflow: "hidden",
        }}>
          {rows.map((r) => (
            <div key={r.code}
              onClick={() => pick(r.code)}
              onMouseDown={(e) => e.preventDefault()}
              style={{
                padding: "6px 10px", cursor: "pointer", display: "flex",
                justifyContent: "space-between", gap: 8, fontSize: 12,
              }}
              className="gsearch-item"
            >
              <span style={{ color: "#4f8cff" }} className="code">{r.code}</span>
              <span style={{ color: "#cbd5e1" }} className="muted">{r.name}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}