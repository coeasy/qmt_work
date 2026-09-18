import { useCallback, useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  DataTable,
  EmptyState,
  Panel,
  Spinner,
  Tabs,
  type Column,
} from "@/design/primitives";
import { tradeApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { useBrokerStore } from "@/stores/broker";
import {
  fmtAmount,
  fmtMoney,
  fmtPct,
  fmtPrice,
  orderStatusLabel,
  orderStatusTone,
  tone,
  toneColor,
} from "@/shared/format";
import type { Deal, Order, Position } from "@/shared/types";
import s from "../domain.module.css";

type Tab = "positions" | "orders" | "deals";

/**
 * 订单状态显示走**共享唯一入口** `shared/format.ts::orderStatusLabel/Tone`。
 *
 * ★ 此处不再自维护映射表：本页原先的键是 `part_filled` / `canceled` / `submitted`，
 * 与后端平台标准词表（xtquant_client/order_status.py 的 partial/cancelled/pending）
 * 对不上 —— 状态列会静默显示原始英文串、撤单按钮不出现。两处各写一套必然再次漂移，
 * 故统一收敛到 format.ts。
 */

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
  /**
   * 待撤单委托。
   *
   * ⚠️ 撤单是**不可逆的资金动作**（已成交部分不会回滚），修复前它是行内一个
   * 点一下就生效的按钮，与「刷新」之类的无害操作并列 ⇒ 误触代价极高。
   * 按既有规范，资金类操作走模态确认（列表逐条删除才用 ConfirmButton 两段式）。
   */
  const [cancelTarget, setCancelTarget] = useState<Order | null>(null);
  /** 操作失败提示（撤单等）；必须显式展示，不能只刷新列表了事 */
  const [actError, setActError] = useState<string | null>(null);

  const positions = useAsync<Position[]>(() => tradeApi.positions(symbol), [symbol]);
  const orders = useAsync<Order[]>(() => tradeApi.orders(), []);
  const deals = useAsync<Deal[]>(() => tradeApi.deals(), []);

  // ★ 持仓叠加实时行情：券商接口只在**查询那一刻**给一次现价，之后不会自己更新，
  //   修复前这一页必须手动点「刷新」数字才动。有行情时优先用实时价，
  //   没有（无券商连接 / 未订阅到）则如实回退券商快照 —— 两者都不能用 0 冒充。
  const positionCodes = useMemo(
    () => (positions.data ?? []).map((p) => p.code),
    [positions.data],
  );
  const quotes = useLiveQuotes(positionCodes);

  const reloadAll = useCallback(() => {
    void positions.reload();
    void orders.reload();
    void deals.reload();
  }, [positions, orders, deals]);

  /**
   * ★ 撤单失败**必须让用户看见**。
   *
   * 此前这里只有 try/finally、没有 catch：券商拒单 / 委托已成不可撤 / 连接断开
   * 都会被静默吞掉，界面只是刷新一次列表 —— 用户以为「已经撤掉了」，
   * 实际委托还挂在市场里。撤单是**有真实后果**的操作，静默失败等于骗人。
   */
  const cancelOrder = useCallback(
    async (orderId: string) => {
      setActError(null);
      try {
        await tradeApi.cancel(orderId);
      } catch (e) {
        setActError(
          `撤单失败（${orderId}）：${e instanceof Error ? e.message : String(e)}`,
        );
      } finally {
        void orders.reload();
      }
    },
    [orders],
  );

  /** 最新价：实时行情优先，缺失回退券商快照（都不是 0） */
  const lastPrice = (r: Position): number | undefined => {
    const q = quotes[r.code];
    if (q !== undefined && q.price > 0) return q.price;
    return r.price;
  };
  /** 今日涨跌幅：只有实时行情才有，券商持仓接口不返回 */
  const lastPct = (r: Position): number | undefined => quotes[r.code]?.change_pct;

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
    {
      key: "last",
      header: "最新",
      width: 74,
      align: "right",
      mono: true,
      render: (r) => (
        <span style={{ color: toneColor(lastPct(r)) }}>{fmtPrice(lastPrice(r))}</span>
      ),
    },
    {
      key: "chg",
      header: "涨跌",
      width: 78,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(lastPct(r)) }}>{fmtPct(lastPct(r))}</span>,
    },
    {
      key: "mv",
      header: "市值",
      width: 100,
      align: "right",
      mono: true,
      // 有实时价就按最新价重算（市值本就是价格的即时函数），否则用券商快照
      render: (r) => {
        const p = lastPrice(r);
        const mv = p !== undefined && p > 0 ? p * r.volume : r.market_value;
        return fmtMoney(mv);
      },
    },
    {
      key: "profit",
      header: "盈亏",
      width: 100,
      align: "right",
      mono: true,
      render: (r) => {
        const p = lastPrice(r);
        const v = p !== undefined && p > 0 && r.cost !== undefined ? (p - r.cost) * r.volume : r.profit;
        return <span style={{ color: toneColor(v) }}>{fmtMoney(v)}</span>;
      },
    },
    {
      key: "pct",
      header: "盈亏比",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => {
        const p = lastPrice(r);
        const v =
          p !== undefined && p > 0 && r.cost !== undefined && r.cost > 0
            ? ((p - r.cost) / r.cost) * 100
            : r.profit_pct;
        return <span style={{ color: toneColor(v) }}>{fmtPct(v)}</span>;
      },
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
      render: (r) => (
        <Badge tone={orderStatusTone(r.status)}>{orderStatusLabel(r.status)}</Badge>
      ),
    },
    { key: "time", header: "时间", width: 88, mono: true, render: (r) => r.time ?? "--" },
    {
      key: "act",
      header: "操作",
      width: 62,
      render: (r) =>
        // 活跃单才可撤：pending（未报/已报/待成交）与 partial（部成）
        r.status === "pending" || r.status === "partial" ? (
          <Button size="sm" variant="ghost" onClick={() => setCancelTarget(r)}>
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
      {actError && (
        <div className={`${s.note} ${s.noteError}`}>
          {actError}
          <Button size="sm" variant="ghost" onClick={() => setActError(null)}>
            知道了
          </Button>
        </div>
      )}

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
              /* 行底色跟「今日涨跌」（行情），盈亏由盈亏列自己着色 ——
                 一处只表达一件事，否则同一行两种颜色互相打架。 */
              rowTone={(r) => tone(lastPct(r))}
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
        <br />
        「最新 / 涨跌」来自<b>实时行情</b>；市值与盈亏在有实时价时按最新价即时重算，
        无行情时回退券商快照值。重算口径为 <code>(最新价 - 成本) × 持仓</code>，
        未计入手续费与分红，与券商对账单可能有细微差异。
      </div>

      <ConfirmModal
        open={cancelTarget !== null}
        title="撤单确认"
        danger
        confirmText="确认撤单"
        message={
          cancelTarget ? (
            <>
              即将撤销委托 <b>{cancelTarget.order_id}</b>
              {cancelTarget.name ? `（${cancelTarget.name} ${cancelTarget.code}）` : `（${cancelTarget.code}）`}
              ：{cancelTarget.side === "buy" ? "买入" : "卖出"} {cancelTarget.volume} 股 @ {fmtPrice(cancelTarget.price)}。
              <br />
              已成交的 <b>{cancelTarget.filled ?? 0}</b> 股<b>不会回滚</b>。
            </>
          ) : null
        }
        warn="撤单不可撤销"
        onConfirm={() => {
          const id = cancelTarget?.order_id;
          setCancelTarget(null);
          if (id) void cancelOrder(id);
        }}
        onCancel={() => setCancelTarget(null)}
      />
    </div>
  );
}

export default Positions;
