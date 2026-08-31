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
    <div className="gsearch" ref={boxRef}>
      <input
        className="gsearch-input"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => { if (q.trim() && rows.length) setOpen(true); }}
        onKeyDown={onKey}
        placeholder="搜索 代码/名称"
        title="输入股票代码或中文名，回车打开行情"
      />
      {busy && <span className="gsearch-busy">…</span>}
      {open && rows.length > 0 && (
        <div className="gsearch-drop">
          {rows.map((r) => (
            <div key={r.code}
              className="gsearch-item"
              onClick={() => pick(r.code)}
              onMouseDown={(e) => e.preventDefault()}
            >
              <span className="code">{r.code}</span>
              <span className="muted">{r.name}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}