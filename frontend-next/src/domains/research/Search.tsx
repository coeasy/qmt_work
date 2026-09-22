import { useMemo, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Input, Panel, Spinner, type Column } from "@/design/primitives";
import { marketApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { useWatchlistStore } from "@/stores/watchlist";
import { fmtPct, fmtPrice, toneColor } from "@/shared/format";
import type { Instrument } from "@/shared/types";
import s from "../domain.module.css";

/**
 * 标的检索与深度画像。
 *
 * ★ 契约要点（market.py）：
 *   - GET /market/search?q=&limit=&include_boards= → 标的（含板块）匹配
 *   - GET /market/resolve?q=&limit= → 更严格的代码解析（用于深链）
 *   - GET /market/analysis?code=&conn_id=&source= → 单请求并发聚合 6 维
 *     （快照 / 画像 / 股本 / 表现 / 资金流 / 估值），返回结构由 services/market/analysis.py 决定，
 *     故本页对分析结果做通用键值渲染，不硬编码字段名（避免字段演进即页面失效）
 *
 * 检索入口已在顶部全局搜索提供；本页的价值在于「检索 + 深度画像」一体化。
 */
export function Search() {
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [selected, setSelected] = useState<Instrument | null>(null);

  const results = useAsync<Instrument[]>(
    () => (submitted ? marketApi.search(submitted, 30) : Promise.resolve([])),
    [submitted],
  );
  const analysis = useAsync<Record<string, unknown>>(
    () => (selected ? marketApi.analysis(selected.code) : Promise.resolve({})),
    [selected?.code],
  );

  // 检索结果叠加实时行情：搜索接口只返回静态档案（代码/名称/交易所/类别），
  // 没有价格 —— 修复前这页「看不到价」，用户还得再点进 K 线才知道是涨是跌。
  const codes = useMemo(() => (results.data ?? []).map((r) => r.code), [results.data]);
  const quotes = useLiveQuotes(codes);

  const toggle = useWatchlistStore((st) => st.toggle);
  const watchCodes = useWatchlistStore((st) => st.codes);

  const cols: Column<Instrument>[] = [
    { key: "code", header: "代码", width: 110, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", render: (r) => r.name },
    {
      key: "last",
      header: "最新",
      width: 78,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(quotes[r.code]?.price),
    },
    {
      key: "chg",
      header: "涨跌幅",
      width: 82,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = quotes[r.code]?.change_pct;
        return <span style={{ color: toneColor(pct) }}>{fmtPct(pct)}</span>;
      },
    },
    { key: "exchange", header: "交易所", width: 90, render: (r) => r.exchange ?? "--" },
    { key: "category", header: "类别", width: 110, render: (r) => r.category ?? "--" },
    {
      key: "act",
      header: "操作",
      width: 168,
      render: (r) => (
        <div style={{ display: "flex", gap: 4 }}>
          <Button size="sm" variant="ghost" onClick={() => setSelected(r)}>
            深度画像
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => toggle(r.code)}
            title={watchCodes.includes(r.code) ? "从自选股移除" : "加入自选股"}
          >
            {watchCodes.includes(r.code) ? "移出自选" : "加自选"}
          </Button>
        </div>
      ),
    },
  ];

  const analysisRows = Object.entries(analysis.data ?? {});

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ width: 280 }}
          placeholder="代码 / 名称 / 拼音首字母"
          onKeyDown={(e) => {
            if (e.key === "Enter") setSubmitted(q.trim());
          }}
        />
        <Button size="sm" disabled={!q.trim()} onClick={() => setSubmitted(q.trim())}>
          检索
        </Button>
        <span className={s.spacer} />
        {results.data && <Badge tone="info">{results.data.length} 条结果</Badge>}
      </div>

      <div className={s.split}>
        <Panel flush title="检索结果" className={s.grow}>
          <div className={s.tableArea}>
            {results.loading ? (
              <Spinner label="检索中…" />
            ) : results.error ? (
              <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                {results.error}
              </div>
            ) : !submitted ? (
              <EmptyState text="输入关键字开始检索" />
            ) : (results.data?.length ?? 0) === 0 ? (
              // 后端 `/market/search` 的口径（market.py docstring）：代码 / 中文名 /
              // 拼音首字母 / 板块名 模糊匹配，**完全离线、基于本地缓存**。
              // 所以「检索不到」有两种成因 —— 关键字不对，或本地标的库还没同步。
              // 只写「无匹配标的」会让用户以为自己把代码记错了。
              <EmptyState text="无匹配标的 —— 支持代码 / 中文名 / 拼音首字母 / 板块名；检索走本地缓存，若本地标的库尚未同步也会检索不到" />
            ) : (
              <DataTable
                columns={cols}
                rows={results.data ?? []}
                rowKey={(r) => r.code}
                rowHeight={26}
                activeKey={selected?.code}
                onRowClick={(r) => setSelected(r)}
              />
            )}
          </div>
        </Panel>

        <Panel title={selected ? `深度画像 · ${selected.name}（${selected.code}）` : "深度画像"}>
          {!selected ? (
            <EmptyState text="从左侧选择一个标的" />
          ) : analysis.loading && analysisRows.length === 0 ? (
            <Spinner label="聚合 6 维数据…" />
          ) : analysis.error ? (
            <div className={`${s.note} ${s.noteWarn}`}>{analysis.error}</div>
          ) : analysisRows.length === 0 ? (
            <EmptyState text="无画像数据" />
          ) : (
            <div className={s.kv}>
              {analysisRows.map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <span className={s.kvKey}>{k}</span>
                  <span className={s.kvVal}>{renderAnalysis(v)}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}

/** 画像值渲染：数字着色、对象降级为紧凑 JSON、null 显示「—」 */
function renderAnalysis(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return String(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export default Search;
