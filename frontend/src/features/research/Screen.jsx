// G7 条件选股（通达信条件选股 / 东财问财对标）。
// 数据：/market/indicators 指标目录（条件构建器元数据）、/market/screen 本地仓
//       全市场向量化扫描、/market/screen/boards 动态板块存取。
// 约束：零轮询；零 mock——仓为空时后端返回 503 引导先同步；条件可存为动态板块。
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import EmptyState from "../../components/ui/EmptyState.jsx";
import { useVirtualList } from "../../lib/useVirtualList.js";

const OPS = [
  { v: "gt", label: ">" },
  { v: "gte", label: "≥" },
  { v: "lt", label: "<" },
  { v: "lte", label: "≤" },
  { v: "eq", label: "=" },
  { v: "ne", label: "≠" },
];
const FIELD_NAMES = [
  { v: "close", label: "收盘价" },
  { v: "open", label: "开盘价" },
  { v: "high", label: "最高价" },
  { v: "low", label: "最低价" },
  { v: "volume", label: "成交量" },
];
const SORTS = [
  { v: "score", label: "命中条件数" },
  { v: "change_pct", label: "涨跌幅" },
  { v: "close", label: "收盘价" },
  { v: "volume", label: "成交量" },
];
let _seq = 0;
const newRow = () => ({
  id: ++_seq,
  kind: "indicator",
  name: "roc",
  output: "",
  op: "gt",
  value: "0",
  window: "-1",
  params: {},
});

const pctCls = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
const fmt = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : Number(v));

export default function Screen() {
  const [inds, setInds] = useState([]);
  const [indsErr, setIndsErr] = useState("");
  const [rows, setRows] = useState([newRow()]);
  const [join, setJoin] = useState("and");
  const [filters, setFilters] = useState({
    min_price: "", max_price: "", sort_by: "score", sort_desc: true, limit: 100,
  });
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [boardName, setBoardName] = useState("");
  const [boards, setBoards] = useState([]);
  const [boardsErr, setBoardsErr] = useState("");
  const [msg, setMsg] = useState("");
  // V9 Phase 8（D12 验收点）：数据源选择器 + 结果溯源徽标。
  // source_policy 透传后端 SourcePolicy：auto/prefer_qmt/qmt_only/local_only/explicit:<id>
  const [providers, setProviders] = useState([]);
  const [sourcePolicy, setSourcePolicy] = useState("auto");
  const [offline, setOffline] = useState(false);
  // T5（G8 闭环）：自然语言输入（放量上涨 / RSI 超卖 / 均线金叉…）
  const [nlText, setNlText] = useState("");
  const [nlBusy, setNlBusy] = useState(false);
  // G11-4：结果 >200 行时虚拟化（5000+ 全市场选股不卡顿）
  const big = (results?.results || []).length > 200;

  // T5：NL → 可编辑条件树（回显到构建器，不黑箱）
  const nlRun = useCallback(async () => {
    const text = nlText.trim();
    if (!text) { setErr("请输入选股描述，如：放量上涨 / RSI(14) 超卖 / 5日均线金叉10日均线"); return; }
    setNlBusy(true); setErr("");
    try {
      const r = await api.screenNL({ text });
      if (r.conditions) {
        const tree = r.conditions;
        // AND 顶层 → 条件行；否则整体 OR 单组（DSL 返回多为 and 树）
        if (tree.and && Array.isArray(tree.and)) {
          setRows(tree.and.map((leaf) => rowFromLeaf(leaf)));
          setJoin("and");
        } else {
          setRows([rowFromLeaf(tree)]);
          setJoin("and");
        }
        setMsg(`已按「${text}」生成条件，可继续调整后运行`);
      } else if (r.unsupported && r.unsupported.length) {
        setErr(`暂不支持：${r.unsupported.join("、")}（可改用条件构建器）`);
      } else {
        setErr("未能识别该描述，请改用条件构建器");
      }
    } catch (e) {
      setErr(e.message || "自然语言解析失败");
    } finally { setNlBusy(false); }
  }, [nlText]);
  const vl = useVirtualList(big ? results.results : [], { itemHeight: 28, height: 420 });

  const indById = useCallback((n) => inds.find((i) => i.name === n), [inds]);

  const loadBoards = useCallback(() => {
    api.screenBoards().then((d) => setBoards(d.items || [])).catch((e) => setBoardsErr(e.message || ""));
  }, []);

  useEffect(() => {
    api.marketIndicators().then((d) => setInds(d.items || [])).catch((e) => setIndsErr(e.message || ""));
    loadBoards();
    // V9 Phase 8：数据源目录加载失败不阻断选股（选择器退化为内置 4 项）
    api.dataProviders()
      .then((d) => setProviders((d.data && d.data.providers) || d.providers || []))
      .catch(() => setProviders([]));
  }, [loadBoards]);

  const buildConditions = () => {
    const leaves = rows
      .filter((r) => r.name && String(r.value) !== "")
      .map((r) => {
        if (r.kind === "indicator") {
          const spec = indById(r.name) || {};
          const params = {};
          (spec.params || []).forEach((p) => {
            const key = p.name === "period" ? "win" : p.name;
            const v = r.params && r.params[key];
            if (v !== "" && v != null) params[key] = Number(v);
          });
          return {
            indicator: {
              name: r.name,
              params,
              output: r.output || (spec.outputs && spec.outputs[0]) || "",
              op: r.op,
              value: Number(r.value),
              window: Number(r.window || -1),
            },
          };
        }
        return { field: { name: r.name, op: r.op, value: Number(r.value), window: Number(r.window || -1) } };
      });
    return join === "or" ? { or: leaves } : { and: leaves };
  };

  const run = async () => {
    setErr("");
    setResults(null);
    setMsg("");
    setLoading(true);
    try {
      const cond = buildConditions();
      const d = await api.marketScreen({
        conditions: JSON.stringify(cond),
        limit: filters.limit,
        sort_by: filters.sort_by,
        sort_desc: filters.sort_desc ? 1 : 0,
        min_price: filters.min_price,
        max_price: filters.max_price,
        source_policy: sourcePolicy,
        offline: offline ? 1 : 0,
      });
      setResults(d);
    } catch (e) {
      setErr(String((e && e.message) || e));
    }
    setLoading(false);
  };

  // T5：NL/DSL 条件树叶子 → 构建器行（回显可编辑，不黑箱）
  const rowFromLeaf = (leaf) => {
    const node = leaf.indicator || leaf.field || leaf.compare;
    if (!node) {
      // and/or 嵌套的叶子 → 取最内层单条件
      const inner = leaf.and ? leaf.and[0] : leaf.or ? leaf.or[0] : null;
      return inner ? rowFromLeaf(inner) : newRow();
    }
    const row = newRow();
    if (leaf.indicator) {
      const spec = indById(node.name) || {};
      row.kind = "indicator";
      row.name = node.name;
      row.output = node.output || (spec.outputs && spec.outputs[0]) || "";
      row.op = node.op || "gt";
      row.value = String(node.value ?? "");
      row.window = String(node.window ?? -1);
      row.params = node.params || {};
    } else if (leaf.compare) {
      // 双序列比较（如 VOLUME > VOL-MA(20)）→ 映射为指标行（右侧 indicator）
      const right = node.right && node.right.kind === "indicator" ? node.right : null;
      if (right) {
        const spec = indById(right.name) || {};
        row.kind = "indicator";
        row.name = right.name;
        row.output = right.output || (spec.outputs && spec.outputs[0]) || "";
        row.op = node.op || "gt";
        row.value = "0";                       // 与序列比较 → 与 0 比（volume_ma 系）
        row.window = String((node.left && node.left.window) ?? -1);
        row.params = right.params || {};
      } else {
        row.kind = "field";
        row.name = (node.left && node.left.name) || "close";
        row.op = node.op || "gt";
        row.value = String((node.right && node.right.value) ?? "");
        row.window = "-1";
      }
    } else {
      row.kind = "field";
      row.name = node.name || "close";
      row.op = node.op || "gt";
      row.value = String(node.value ?? "");
      row.window = String(node.window ?? -1);
    }
    return row;
  };

  const save = async () => {
    if (!boardName.trim() || !results || !results.results || !results.results.length) {
      setMsg("请先运行选股并输入板块名称");
      return;
    }
    setMsg("");
    try {
      await api.screenBoardsSave({
        name: boardName.trim(),
        conditions: buildConditions(),
        results: results.results,
      });
      setMsg(`已存为动态板块「${boardName.trim()}」`);
      loadBoards();
    } catch (e) {
      setMsg("保存失败：" + String((e && e.message) || e));
    }
  };

  const patchRow = (id, patch) =>
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  const onRowKind = (r, kind) => {
    patchRow(r.id, { kind, name: kind === "field" ? "close" : (inds[0] && inds[0].name) || "roc", output: "", params: {} });
  };
  const onRowName = (r, name) => {
    const spec = indById(name) || {};
    const params = {};
    (spec.params || []).forEach((p) => { params[p.name === "period" ? "win" : p.name] = p.default; });
    patchRow(r.id, { name, output: (spec.outputs && spec.outputs[0]) || "", params });
  };

  return (
    <div className="market-page sc-page">
      {/* T5（G8 闭环）：自然语言选股入口 → 生成可编辑条件树 */}
      <div className="bd-toolbar sc-toolbar sc-nlbar">
        <span className="sc-title">自然语言选股</span>
        <input
          className="mp-code-input sc-input sc-nlinput"
          placeholder="试试：放量上涨 / 近5日下跌 / RSI(14) 超卖 / 5日均线金叉 / 站上20日均线"
          value={nlText}
          onChange={(e) => setNlText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && nlRun()}
          maxLength={120}
        />
        <button className="ib-btn" onClick={nlRun} disabled={nlBusy || !nlText.trim()}>
          {nlBusy ? "解析中…" : "生成条件"}
        </button>
      </div>

      {/* 条件构建 */}
      <div className="bd-toolbar sc-toolbar">
        <span className="sc-title">条件选股</span>
        <select className="ib-btn" value={join} onChange={(e) => setJoin(e.target.value)} title="条件组合方式">
          <option value="and">全部满足 (AND)</option>
          <option value="or">任一满足 (OR)</option>
        </select>
        <button className="ib-btn" onClick={() => setRows((p) => [...p, newRow()])}>+ 添加条件</button>
        <span className="sc-spacer" />
        <select className="ib-btn" value={filters.sort_by} onChange={(e) => setFilters({ ...filters, sort_by: e.target.value })}>
          {SORTS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        <label className="checkbox sc-chk">
          <input type="checkbox" checked={filters.sort_desc}
            onChange={(e) => setFilters({ ...filters, sort_desc: e.target.checked })} /> 降序
        </label>
        <input className="mp-code-input sc-input" placeholder="最低价" value={filters.min_price}
          onChange={(e) => setFilters({ ...filters, min_price: e.target.value })} />
        <input className="mp-code-input sc-input" placeholder="最高价" value={filters.max_price}
          onChange={(e) => setFilters({ ...filters, max_price: e.target.value })} />
        <input className="mp-code-input sc-input" style={{ width: 64 }} placeholder="条数" value={filters.limit}
          onChange={(e) => setFilters({ ...filters, limit: e.target.value })} />
        {/* V9 Phase 8：数据源选择器（未连接券商时选股页必须可用 —— D12 验收点） */}
        <select className="ib-btn" value={sourcePolicy} title="数据源策略"
          onChange={(e) => setSourcePolicy(e.target.value)}>
          <option value="auto">数据源：自动</option>
          <option value="prefer_qmt">优先 QMT</option>
          <option value="qmt_only">仅 QMT</option>
          <option value="local_only">仅本地仓</option>
          {(providers || []).map((p) => {
            const id = typeof p === "string" ? p : p.id;
            const label = typeof p === "string" ? p : (p.name || p.id);
            return <option key={id} value={`explicit:${id}`}>指定：{label}</option>;
          })}
        </select>
        <label className="checkbox sc-chk" title="离线模式：只读本地仓，不请求在线数据源">
          <input type="checkbox" checked={offline}
            onChange={(e) => setOffline(e.target.checked)} /> 离线
        </label>
        <button className="btn-primary" onClick={run} disabled={loading}>
          {loading ? "扫描中…" : "运行选股"}
        </button>
      </div>

      {/* 条件行 */}
      <div className="sc-rows">
        {rows.map((r) => {
          const spec = r.kind === "indicator" ? indById(r.name) : null;
          return (
            <div key={r.id} className="form-row sc-row">
              <select className="ib-btn" value={r.kind} onChange={(e) => onRowKind(r, e.target.value)}>
                <option value="indicator">指标</option>
                <option value="field">字段</option>
              </select>
              {r.kind === "indicator" ? (
                <>
                  <select className="ib-btn" value={r.name} onChange={(e) => onRowName(r, e.target.value)}>
                    {inds.map((i) => <option key={i.name} value={i.name}>{i.label}</option>)}
                    {indsErr && <option disabled>{indsErr}</option>}
                  </select>
                  <select className="ib-btn" value={r.output} onChange={(e) => patchRow(r.id, { output: e.target.value })}>
                    {(spec && spec.outputs || []).map((o) => <option key={o} value={o}>{o.toUpperCase()}</option>)}
                  </select>
                  {(spec && spec.params || []).map((p) => (
                    <input key={p.name} className="mp-code-input sc-input" style={{ width: 64 }}
                      placeholder={p.name === "period" ? "周期" : p.name}
                      value={(r.params && r.params[p.name === "period" ? "win" : p.name]) ?? ""}
                      onChange={(e) => patchRow(r.id, { params: { ...(r.params || {}), [p.name === "period" ? "win" : p.name]: e.target.value } })} />
                  ))}
                </>
              ) : (
                <select className="ib-btn" value={r.name} onChange={(e) => patchRow(r.id, { name: e.target.value })}>
                  {FIELD_NAMES.map((f) => <option key={f.v} value={f.v}>{f.label}</option>)}
                </select>
              )}
              <select className="ib-btn" value={r.op} onChange={(e) => patchRow(r.id, { op: e.target.value })}>
                {OPS.map((o) => <option key={o.v} value={o.v}>{o.label}</option>)}
              </select>
              <input className="mp-code-input sc-input" placeholder="阈值" value={r.value}
                onChange={(e) => patchRow(r.id, { value: e.target.value })} />
              <input className="mp-code-input sc-input" style={{ width: 56 }} placeholder="偏移" title="K线偏移，-1=最新"
                value={r.window} onChange={(e) => patchRow(r.id, { window: e.target.value })} />
              <button className="btn-danger-sm" onClick={() => setRows((p) => p.filter((x) => x.id !== r.id))}>删</button>
            </div>
          );
        })}
      </div>

      {err && <div className="sc-msg sc-err">{err}</div>}
      {msg && <div className="sc-msg">{msg}</div>}

      {/* 结果 */}
      {results && (
        <div className="sc-results">
          <div className="bd-toolbar">
            <span className="sc-title">命中 {results.count} 只（扫描 {results.total_scanned} 只，{results.elapsed_ms}ms）</span>
            {/* V9 Phase 8：数据来源徽标 —— 来源 · 截至 · 降级原因 */}
            {results.provenance && (
              <span className={`prov-badge ${results.degraded ? "prov-degraded" : ""}`}
                title={results.provider_policy_version ? `策略版本 ${results.provider_policy_version}` : ""}>
                来源: {results.provenance.provider_used || "—"}
                {results.provenance.as_of ? ` · 截至 ${results.provenance.as_of}` : ""}
                {results.degraded
                  ? ` · 已降级（${results.degraded_reason || "详见 fallback_tried"}）`
                  : ""}
              </span>
            )}
            <input className="mp-code-input sc-input" placeholder="存为板块名称" value={boardName}
              onChange={(e) => setBoardName(e.target.value)} />
            <button className="btn-primary" onClick={save}>存为动态板块</button>
          </div>
          {results.count === 0 ? (
            <EmptyState title="无命中" hint="放宽条件，或先运行全市场同步（本地仓为空时选股返回 503）" />
          ) : big ? (
            <div className="sc-vlist" style={{ height: 420, overflow: "auto" }} onScroll={vl.onScroll}>
              <div style={{ height: vl.totalHeight, position: "relative" }}>
                {vl.visible.map((r, i) => (
                  <div key={r.code} className="sc-vrow" style={{ top: (vl.startIndex + i) * 28 }}>
                    <span className="sc-code">{r.code}</span>
                    <span>{r.name}</span>
                    <span>{fmt(r.close)}</span>
                    <span className={pctCls(r.change_pct)}>{r.change_pct == null ? "—" : `${r.change_pct}%`}</span>
                    <span>{fmt(r.volume)}</span>
                    <span>{r.score}/{r.total_conditions}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <table className="table sc-table">
              <thead>
                <tr><th>代码</th><th>名称</th><th>收盘</th><th>涨跌幅</th><th>成交量</th><th>命中</th></tr>
              </thead>
              <tbody>
                {results.results.map((r) => (
                  <tr key={r.code}>
                    <td className="sc-code">{r.code}</td>
                    <td>{r.name}</td>
                    <td>{fmt(r.close)}</td>
                    <td className={pctCls(r.change_pct)}>{r.change_pct == null ? "—" : `${r.change_pct}%`}</td>
                    <td>{fmt(r.volume)}</td>
                    <td>{r.score}/{r.total_conditions}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* 已存动态板块 */}
      <div className="sc-boards">
        <div className="sc-title">已存动态板块</div>
        {boards.length === 0
          ? <span className="sc-muted">{boardsErr ? `加载失败：${boardsErr}` : "（暂无，运行选股后可存为板块）"}</span>
          : <ul className="sc-board-list">{boards.map((b) => (
            <li key={b.kind}>{b.name} · {b.count} 只</li>
          ))}</ul>}
      </div>
    </div>
  );
}
