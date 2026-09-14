import { useCallback, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Panel,
  Spinner,
  Tabs,
  type Column,
} from "@/design/primitives";
import { tradeApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useBrokerStore } from "@/stores/broker";
import { fmtAmount, fmtPrice, tone, toneColor } from "@/shared/format";
import type { Deal, Order, OrderStatus, Position } from "@/shared/types";
import s from "../domain.module.css";

type Tab = "positions" | "orders" | "deals";

const STATUS_TONE: Record<OrderStatus, "success" | "danger" | "neutral" | "info" | "warning"> = {
  filled: "success",
  rejected: "danger",
  canceled: "neutral",
  pending: "info",
  submitted: "info",
  part_filled: "warning",
  unknown: "neutral",
};

const STATUS_LABEL: Record<OrderStatus, string> = {
  filled: "已成交",
  rejected: "已拒",
  canceled: "已撤",
  pending: "待报",
  submitted: "已报",
  part_filled: "部成",
  unknown: "未知",
};

/**
 * 委托 / 持仓 / 成交（账户域核心查询页）。
 *
 * ★ 契约要点（已核对 trade.py）：
 *   - /trade/positions|orders|deals **只返回 active 连接**的数据，不支持按 conn_id 过滤；
 *     positions 的 query 参数是 symbol（按标的过滤），不是 conn_id。
 *   - 多账户汇总请用「多账户网格」页（/account/grid）。
 *   - 无券商连接时后端返回 503，本页显式展示引导，不显示空表冒充「无数据」。
 */
export function Positions() {
  const connections = useBrokerStore((st) => st.connections);
  const active = connections.find((c) => c.active);
  const connected = connections.some((c) => c.connected);

  const [tab, setTab] = useState<Tab>("positions");
  const [symbol, setSymbol] = useState("");

  const positions = useAsync<Position[]>(() => tradeApi.positions(symbol), [symbol]);
  const orders = useAsync<Order[]>(() => tradeApi.orders(), []);
  const deals = useAsync<Deal[]>(() => tradeApi.deals(), []);

  const reloadAll = useCallback(() => {
    void positions.reload();
    void orders.reload();
    void deals.reload();
  }, [positions, orders, deals]);

  const cancelOrder = useCallback(
    async (orderId: string) => {
      try {
        await tradeApi.cancel(orderId);
      } finally {
        void orders.reload();
      }
    },
    [orders],
  );

  const positionCols: Column<Position>[] = [
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 92, render: (r) => r.name ?? "--" },
    { key: "vol", header: "持仓", width: 76, align: "right", mono: true, render: (r) => String(r.volume) },
    {
      key: "avail",
      header: "可用",
      width: 76,
      align: "right",
      mono: true,
      render: (r) => (r.avail === undefined ? "--" : String(r.avail)),
    },
    { key: "cost", header: "成本", width: 74, align: "right", mono: true, render: (r) => fmtPrice(r.cost) },
    { key: "price", header: "现价", width: 74, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "mv", header: "市值", width: 100, align: "right", mono: true, render: (r) => fmtAmount(r.market_value) },
    {
      key: "profit",
      header: "盈亏",
      width: 100,
      align: "right",
      mono: true,
      render: (r) => (
        <span style={{ color: toneColor(r.profit) }}>{fmtAmount(r.profit)}</span>
      ),
    },
    {
      key: "pct",
      header: "盈亏比",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => (
        <span style={{ color: toneColor(r.profit_pct) }}>
          {r.profit_pct === undefined ? "--" : `${r.profit_pct >= 0 ? "+" : ""}${r.profit_pct.toFixed(2)}%`}
        </span>
      ),
    },
  ];

  const orderCols: Column<Order>[] = [
    { key: "oid", header: "委托号", width: 110, mono: true, render: (r) => r.order_id },
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 88, render: (r) => r.name ?? "--" },
    {
      key: "side",
      header: "方向",
      width: 54,
      render: (r) => (
        <span style={{ color: r.side === "buy" ? "var(--up)" : "var(--down)" }}>
          {r.side === "buy" ? "买入" : "卖出"}
        </span>
      ),
    },
    { key: "price", header: "委托价", width: 78, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "vol", header: "委托量", width: 76, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "filled", header: "已成交", width: 76, align: "right", mono: true, render: (r) => String(r.filled ?? 0) },
    {
      key: "status",
      header: "状态",
      width: 76,
      render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "neutral"}>{STATUS_LABEL[r.status] ?? r.status}</Badge>,
    },
    { key: "time", header: "时间", width: 88, mono: true, render: (r) => r.time ?? "--" },
    {
      key: "act",
      header: "操作",
      width: 62,
      render: (r) =>
        r.status === "pending" || r.status === "submitted" || r.status === "part_filled" ? (
          <Button size="sm" variant="ghost" onClick={() => void cancelOrder(r.order_id)}>
            撤单
          </Button>
        ) : null,
    },
  ];

  const dealCols: Column<Deal>[] = [
    { key: "did", header: "成交号", width: 110, mono: true, render: (r) => r.deal_id },
    { key: "oid", header: "委托号", width: 110, mono: true, render: (r) => r.order_id ?? "--" },
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    {
      key: "side",
      header: "方向",
      width: 54,
      render: (r) => (
        <span style={{ color: r.side === "buy" ? "var(--up)" : "var(--down)" }}>
          {r.side === "buy" ? "买入" : "卖出"}
        </span>
      ),
    },
    { key: "price", header: "成交价", width: 80, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "vol", header: "成交量", width: 76, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "amt", header: "金额", width: 104, align: "right", mono: true, render: (r) => fmtAmount(r.amount) },
    { key: "time", header: "时间", width: 88, mono: true, render: (r) => r.time ?? "--" },
  ];

  const cur = tab === "positions" ? positions : tab === "orders" ? orders : deals;
  const rows = (cur.data ?? []) as Array<Position | Order | Deal>;

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <span className={s.muted}>
          当前账户：
          {active ? (
            <span className={s.mono}>
              {active.broker_name ?? active.broker_id} / {active.account_id ?? active.conn_id}
            </span>
          ) : (
            <span>（无 active 连接）</span>
          )}
        </span>
        <span className={s.spacer} />
        <input
          className={s.mono}
          style={{
            width: 140,
            padding: "2px 6px",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            background: "var(--bg-2)",
            color: "var(--text)",
          }}
          placeholder="按标的过滤持仓"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.trim())}
        />
        <Button size="sm" onClick={reloadAll}>
          刷新
        </Button>
      </div>

      {!connected && (
        <div className={`${s.note} ${s.noteWarn}`}>
          当前无已连接券商。查询类端点会返回 503（零 mock 契约），请先到「连接管理」添加并连接券商。
        </div>
      )}

      {cur.error && <div className={`${s.note} ${s.noteError}`}>加载失败：{cur.error}</div>}

      <Panel
        flush
        className={s.grow}
        title="账户查询"
        extra={
          <Tabs
            items={[
              { key: "positions", label: `持仓 ${positions.data?.length ?? 0}` },
              { key: "orders", label: `委托 ${orders.data?.length ?? 0}` },
              { key: "deals", label: `成交 ${deals.data?.length ?? 0}` },
            ]}
            value={tab}
            onChange={setTab}
          />
        }
      >
        <div className={s.tableArea}>
          {cur.loading ? (
            <Spinner label="加载中…" />
          ) : rows.length === 0 ? (
            <EmptyState
              text={
                connected
                  ? tab === "positions"
                    ? "当前账户无持仓"
                    : tab === "orders"
                      ? "当前账户无委托"
                      : "当前账户无成交"
                  : "未连接券商，无法查询"
              }
            />
          ) : tab === "positions" ? (
            <DataTable
              columns={positionCols}
              rows={positions.data ?? []}
              rowKey={(r) => r.code}
              rowHeight={24}
              rowTone={(r) => tone(r.profit_pct)}
            />
          ) : tab === "orders" ? (
            <DataTable columns={orderCols} rows={orders.data ?? []} rowKey={(r) => r.order_id} rowHeight={24} />
          ) : (
            <DataTable columns={dealCols} rows={deals.data ?? []} rowKey={(r) => r.deal_id} rowHeight={24} />
          )}
        </div>
      </Panel>

      <div className={s.note}>
        说明：本页查询只作用于 <b>active 连接</b>（后端 trade.py 的 positions/orders/deals 无 conn_id 参数）。
        需要跨账户汇总持仓与委托，请使用「多账户网格」页。
      </div>
    </div>
  );
}

export default Positions;
