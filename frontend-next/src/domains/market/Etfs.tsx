import { useMemo, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Input, Panel, Select, Spinner, type Column } from "@/design/primitives";
import { marketApi, type EtfResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtAmount, fmtPct, fmtPrice, toneColor } from "@/shared/format";
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
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
  // ETF 列表点一行 ⇒ 直接进行情工作台看这只标的（唯一出口，勿各写一遍）
  const openWorkbench = useOpenWorkbench();

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

      {/* 项数是动态的（ETF 总数 + 最多 3 个代码段 + 订阅数）⇒ 用一行条：
          卡片网格在项数变化时会重排成「3+2」这种参差的两行。 */}
      {res.data && (
        <div className={`${s.statRow} ${s.statRowDense}`}>
          <div className={s.statRowItem} title={`当前显示 ${filtered.length} 只`}>
            <span className={s.statRowLabel}>ETF 总数</span>
            <span className={s.statRowValue}>{res.data.count}</span>
          </div>
          {Object.entries(res.data.groups)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 3)
            .map(([g, n]) => (
              <div className={s.statRowItem} key={g} title={`代码段 ${g}xx 共 ${n} 只`}>
                <span className={s.statRowLabel}>{g}xx</span>
                <span className={s.statRowValue}>{n}</span>
              </div>
            ))}
          <div className={s.statRowItem} title="实时订阅上限 80 只（WS）">
            <span className={s.statRowLabel}>实时订阅</span>
            <span className={s.statRowValue}>{subCodes.length}</span>
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
            // ★ 空列表有**两种**成因，文案不能一刀切：
            //   ① 有筛选 ⇒ 筛窄了，给「清除筛选」；
            //   ② 没筛选还空 ⇒ 是清单本身没数据（行情源按代码段 51/56/58/15/16 枚举，
            //      源不可用/未同步时为空）。若 ② 也显示 ① 的文案，等于把「没数据」
            //      说成「你筛错了」—— 正是本项目反复禁止的错误归因。
            filter.trim() ? (
              <EmptyState
                text="无匹配的 ETF —— 当前筛选按代码或名称子串匹配，可清除筛选看全部"
                actionText="清除筛选"
                onAction={() => setFilter("")}
              />
            ) : (
              <EmptyState text="暂无 ETF 清单 —— 清单由行情源按代码段（51/56/58/15/16）枚举，行情源不可用或未同步时为空" />
            )
          ) : (
            <DataTable
              columns={cols}
              rows={filtered}
              rowKey={(r) => r.code}
              rowHeight={24}
              onRowClick={(r) => openWorkbench(r.code, r.name ?? "")}
            />
          )}
        </div>
      </Panel>
    </div>
  );
}

export default Etfs;
