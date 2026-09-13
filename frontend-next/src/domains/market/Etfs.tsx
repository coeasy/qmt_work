import { useMemo, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Input, Panel, Select, Spinner, type Column } from "@/design/primitives";
import { marketApi, type EtfResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtAmount, fmtPct, fmtPrice, toneColor } from "@/shared/format";
import s from "../domain.module.css";

type EtfRow = EtfResponse["items"][number];

/**
 * ETF 清单与行情。
 *
 * ★ 契约要点（aggregates.py:etfs）：
 *   - 返回 {items, count, groups(按代码段分组计数), quote_capped, quote_limit, source, ts}
 *   - with_quote=true 时后端只对**前 quote_limit 只**取快照（默认上限），
 *     超出部分不取价并在 quote_capped 标记 —— 本页显式提示，不让用户误以为全量有价
 *   - 清单本身有 300s TTL 缓存；实时价建议由 WS 订阅可见行，而非全量快照
 *
 * 本页做法：清单走 REST，实时价走 WS 订阅（useQuoteSubscription），
 * 两者互补 —— 既有全量代码，又不为全量打源。
 */
export function Etfs() {
  const [limit, setLimit] = useState("200");
  const [withQuote, setWithQuote] = useState("0");
  const [filter, setFilter] = useState("");

  const res = useAsync(
    () => marketApi.etfs(Number(limit) || 0, withQuote === "1"),
    [limit, withQuote],
  );

  const items = res.data?.items ?? [];
  const filtered = useMemo(() => {
    const q = filter.trim().toUpperCase();
    if (!q) return items;
    return items.filter((it) => it.code.includes(q) || (it.name ?? "").toUpperCase().includes(q));
  }, [items, filter]);

  /** 只订阅当前可见的前 80 只，避免一次订阅上千只 */
  const subCodes = useMemo(() => filtered.slice(0, 80).map((it) => it.code), [filtered]);
  useQuoteSubscription(subCodes);
  const quotes = useQuotesStore((st) => st.quotes);

  const cols: Column<EtfRow>[] = [
    { key: "code", header: "代码", width: 104, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", render: (r) => r.name || "--" },
    { key: "exchange", header: "交易所", width: 80, render: (r) => r.exchange ?? "--" },
    {
      key: "price",
      header: "最新价",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => {
        const q = quotes[r.code];
        return <span style={{ color: toneColor(q?.change_pct) }}>{fmtPrice(q?.price ?? r.last)}</span>;
      },
    },
    {
      key: "pct",
      header: "涨跌幅",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => {
        const q = quotes[r.code];
        const pct = q?.change_pct ?? r.change_pct;
        return <span style={{ color: toneColor(pct) }}>{fmtPct(pct)}</span>;
      },
    },
    {
      key: "amount",
      header: "成交额",
      width: 104,
      align: "right",
      mono: true,
      render: (r) => {
        const q = quotes[r.code];
        return fmtAmount(q?.amount ?? r.amount);
      },
    },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ width: 160 }}
          placeholder="按代码/名称过滤"
        />
        <span className={s.muted}>清单上限</span>
        <Input value={limit} onChange={(e) => setLimit(e.target.value)} mono style={{ width: 70 }} />
        <span className={s.muted}>（0=全量）</span>
        <span className={s.muted}>附快照</span>
        <Select
          value={withQuote}
          onChange={(e) => setWithQuote(e.target.value)}
          options={[
            { value: "0", label: "否（推荐，实时价走 WS）" },
            { value: "1", label: "是（仅前 N 只，可能被截断）" },
          ]}
        />
        <span className={s.spacer} />
        {res.data && <Badge tone="info">源 {res.data.source}</Badge>}
        <Button size="sm" variant="ghost" onClick={() => void res.reload()}>
          刷新
        </Button>
      </div>

      {res.error && <div className={`${s.note} ${s.noteWarn}`}>{res.error}</div>}

      {res.data && (
        <div className={s.stats}>
          <div className={s.stat}>
            <span className={s.statLabel}>ETF 总数</span>
            <span className={s.statValue}>{res.data.count}</span>
            <span className={s.statSub}>当前显示 {filtered.length}</span>
          </div>
          {Object.entries(res.data.groups)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 3)
            .map(([g, n]) => (
              <div className={s.stat} key={g}>
                <span className={s.statLabel}>代码段 {g}xx</span>
                <span className={s.statValue}>{n}</span>
                <span className={s.statSub}>只</span>
              </div>
            ))}
          <div className={s.stat}>
            <span className={s.statLabel}>实时订阅</span>
            <span className={s.statValue}>{subCodes.length}</span>
            <span className={s.statSub}>前 80 只（WS）</span>
          </div>
        </div>
      )}

      {res.data?.quote_capped && (
        <div className={`${s.note} ${s.noteWarn}`}>
          后端快照已截断：仅前 {res.data.quote_limit} 只带行情（quote_capped=true）。
          其余标的的实时价由本页的 WS 订阅补齐，未订阅到的显示「—」，不用 0 冒充。
        </div>
      )}

      <Panel flush className={s.grow} title={`ETF 清单（${filtered.length}）`}>
        <div className={s.tableArea}>
          {res.loading && !res.data ? (
            <Spinner label="加载 ETF 清单…" />
          ) : filtered.length === 0 ? (
            <EmptyState text="无匹配的 ETF" />
          ) : (
            <DataTable columns={cols} rows={filtered} rowKey={(r) => r.code} rowHeight={24} />
          )}
        </div>
      </Panel>
    </div>
  );
}

export default Etfs;
