import { Fragment, useMemo, type ReactNode } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  type Column,
} from "@/design/primitives";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtMoney, fmtPct, fmtPrice, toneColor } from "@/shared/format";
import type { AccountGrid, AccountGridPosition } from "@/shared/types";
import s from "../domain.module.css";

/**
 * 资产汇总卡 + 跨账户持仓表 —— **仪表盘与「多账户网格」页共用同一份实现**。
 *
 * ## 为什么必须抽出来
 *
 * 这两块内容原先各写了一份：
 * - 「多账户网格」页（`account/Accounts.tsx`）直接吃 `GET /account/grid`；
 * - 仪表盘（`domains/Dashboard.tsx`）吃 `GET /account/status`，然后用
 *   `positions.reduce(...)` 自己把持仓市值和盈亏加出来。
 *
 * 后果是**同一时刻两页给出不同的总资产**：`/account/status` 只看
 * 「第一个可用连接」的账户，`/account/grid` 才是跨账户汇总。前端自己 reduce
 * 还会把「后端已经算好的权威值」再算一遍 —— 一旦两边口径不一致（例如券商
 * `get_account().assets` 恒为 0 需要退化成 cash+市值），用户看到的就是两个数。
 *
 * 现在的纪律：**后端算，前端只格式化**。本文件里不再出现任何 `reduce` 求和。
 */

/** 持仓最新价：实时行情 > 后端快照价 > 市值/股数反推（仅自洽时）。 */
export function positionPrice(
  p: AccountGridPosition,
  live: number | undefined,
): number | undefined {
  if (live !== undefined && live > 0) return live;
  if (typeof p.price === "number" && p.price > 0) return p.price;
  return p.total_volume > 0 ? p.total_market_value / p.total_volume : undefined;
}

/**
 * 持仓浮动盈亏。
 *
 * 有成本就按「当前价 × 股数 − 合计成本」现算，保证与相邻的「最新价 / 合计市值」
 * 两列出自同一个价格；没有成本才退回后端聚合值。两者都拿不到返回 undefined
 * ⇒ 界面显示「—」，**不显示 0**（0 会被读成「真的不赚不亏」）。
 */
export function positionProfit(
  p: AccountGridPosition,
  price: number | undefined,
): number | undefined {
  if (typeof p.total_cost === "number" && p.total_cost > 0 && price !== undefined && price > 0) {
    return price * p.total_volume - p.total_cost;
  }
  return typeof p.profit === "number" ? p.profit : undefined;
}

/** 盈亏比的显示值：优先用现算的（与盈亏列同源），否则后端值。 */
export function positionProfitPct(
  p: AccountGridPosition,
  price: number | undefined,
): number | undefined {
  if (typeof p.total_cost === "number" && p.total_cost > 0 && price !== undefined && price > 0) {
    return ((price * p.total_volume - p.total_cost) / p.total_cost) * 100;
  }
  return typeof p.profit_pct === "number" ? p.profit_pct : undefined;
}

/** 「—」占位：金额/比率为空时统一用它，绝不用 0 冒充。 */
export function Dash() {
  return <span className={s.muted}>—</span>;
}

export interface AssetSummaryCardsProps {
  grid: AccountGrid | null;
  loading?: boolean;
  /** 卡片之外要补的内容（如仪表盘的「去连接」入口） */
  footer?: ReactNode;
}

/**
 * 资产汇总卡：总资产 / 可用资金 / 持仓市值 / 浮动盈亏。
 *
 * ★ 数值一律 `fmtMoney`（2 位小数）。`fmtAmount` 是给成交额/成交量做「万/亿」
 * 缩写的，`<1 万` 时会 `toFixed(0)` 丢掉角分 —— 总资产与券商对账单永远差几元，
 * 用户会以为账算错了。
 */
export function AssetSummaryCards({ grid, loading, footer }: AssetSummaryCardsProps) {
  const posCount = grid?.positions.length ?? 0;
  /** 未取到数据时显示「…」（加载中）或「—」（确知无数据），绝不用 0 冒充 */
  const money = (v: number | null | undefined) =>
    grid === null ? (loading ? "…" : "—") : v === null || v === undefined ? "—" : fmtMoney(v);
  return (
    <div className={s.stats}>
      <div className={s.stat}>
        <span className={s.statLabel}>总资产</span>
        <span className={s.statValue}>{money(grid?.total_assets)}</span>
        <span className={s.statSub}>
          {grid ? `已连接 ${grid.connected_count} / ${grid.account_count} 个账户` : "无数据"}
        </span>
      </div>
      <div className={s.stat}>
        <span className={s.statLabel}>可用资金合计</span>
        <span className={s.statValue}>{money(grid?.total_cash)}</span>
        <span className={s.statSub}>全部账户现金</span>
      </div>
      <div className={s.stat}>
        <span className={s.statLabel}>持仓市值合计</span>
        <span className={s.statValue}>{money(grid?.total_market_value)}</span>
        <span className={s.statSub}>{grid ? `按标的合并 ${posCount} 只` : "无数据"}</span>
      </div>
      <div className={s.stat}>
        <span className={s.statLabel}>浮动盈亏合计</span>
        <span className={s.statValue} style={{ color: toneColor(grid?.total_profit) }}>
          {money(grid?.total_profit)}
        </span>
        <span className={s.statSub}>需成本价才可计算</span>
      </div>
      {footer && <div className={s.stat}>{footer}</div>}
    </div>
  );
}

export interface CrossAccountPositionsProps {
  positions: AccountGridPosition[];
  /** 叠加实时行情（默认开）。关掉则只用后端快照价。 */
  live?: boolean;
  /** 「查看分布」回调；不传则不渲染该列 */
  onInspect?: (p: AccountGridPosition) => void;
  emptyText?: string;
}

/**
 * 跨账户持仓汇总表：按标的合并，给出合计股数/市值/盈亏与分布账户数。
 *
 * ★ 叠加实时行情：后端的持仓快照只在**查询那一刻**有价，之后不会自己更新。
 * 订阅可用时用实时价重算市值与盈亏，不可用时如实退回快照 —— 两者都不是 0。
 */
export function CrossAccountPositions({
  positions,
  live = true,
  onInspect,
  emptyText = "无跨账户持仓",
}: CrossAccountPositionsProps) {
  // 依赖用逗号拼接字符串，避免每次渲染重新订阅（useLiveQuotes 内部已做，这里
  // 只需保证 positions 引用稳定即可）。
  const codes = useMemo(() => positions.map((p) => p.code), [positions]);
  const quotes = useLiveQuotes(live ? codes : []);

  const cols: Column<AccountGridPosition>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 110, render: (r) => r.name || "--" },
    {
      key: "last",
      header: "最新价",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => fmtPrice(positionPrice(r, quotes[r.code]?.price)),
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
    {
      key: "vol",
      header: "合计持仓",
      width: 100,
      align: "right",
      mono: true,
      render: (r) => String(r.total_volume),
    },
    {
      key: "mv",
      header: "合计市值",
      width: 120,
      align: "right",
      mono: true,
      // 有实时价就按最新价重算（市值是价格的即时函数），否则用后端快照
      render: (r) => {
        const p = positionPrice(r, quotes[r.code]?.price);
        return fmtMoney(p !== undefined && p > 0 ? p * r.total_volume : r.total_market_value);
      },
    },
    {
      key: "pnl",
      header: "浮动盈亏",
      width: 110,
      align: "right",
      mono: true,
      render: (r) => {
        const v = positionProfit(r, positionPrice(r, quotes[r.code]?.price));
        return v === undefined ? <Dash /> : <span style={{ color: toneColor(v) }}>{fmtMoney(v)}</span>;
      },
    },
    {
      key: "pnlpct",
      header: "盈亏比",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => {
        const v = positionProfitPct(r, positionPrice(r, quotes[r.code]?.price));
        return v === undefined ? <Dash /> : <span style={{ color: toneColor(v) }}>{fmtPct(v)}</span>;
      },
    },
    {
      key: "n",
      header: "分布账户",
      width: 90,
      align: "right",
      mono: true,
      render: (r) => `${r.accounts.length} 个`,
    },
  ];
  if (onInspect) {
    cols.push({
      key: "act",
      header: "操作",
      width: 80,
      render: (r) => (
        <Button size="sm" variant="ghost" onClick={() => onInspect(r)}>
          查看分布
        </Button>
      ),
    });
  }

  if (!positions.length) return <EmptyState text={emptyText} />;
  return (
    <DataTable columns={cols} rows={positions} rowKey={(r) => r.code} rowHeight={24} />
  );
}

/** 单只标的的分布明细（「查看分布」弹层内容）。 */
export function PositionDistribution({ position }: { position: AccountGridPosition }) {
  return (
    <>
      <div className={s.kv}>
        {position.accounts.map((a) => (
          <Fragment key={a.conn_id}>
            <span className={s.kvKey}>{a.name || a.conn_id}</span>
            <span className={s.kvVal}>
              <span className={s.mono}>{a.volume}</span> 股 · 市值{" "}
              <span className={s.mono}>{fmtMoney(a.market_value)}</span>
              {typeof a.profit === "number" && (
                <>
                  {" · 盈亏 "}
                  <span className={s.mono} style={{ color: toneColor(a.profit) }}>
                    {fmtMoney(a.profit)}
                  </span>
                </>
              )}
            </span>
          </Fragment>
        ))}
      </div>
      <div className={s.note} style={{ marginTop: 8 }}>
        合计持仓 <span className={s.mono}>{position.total_volume}</span> 股，合计市值{" "}
        <span className={s.mono}>{fmtMoney(position.total_market_value)}</span>
      </div>
    </>
  );
}

/** 账户连接状态徽标（逐账户行用）。 */
export function ConnBadge({ connected }: { connected: boolean }) {
  return connected ? <Badge tone="success">已连接</Badge> : <Badge tone="danger">未连接</Badge>;
}
