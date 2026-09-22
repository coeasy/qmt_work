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
import { useWorkspaceStore } from "@/stores/workspace";
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
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
import { isLivePrice, staleQuoteNote } from "@/shared/freshness";
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
  const open = useWorkspaceStore((st) => st.open);
  // 持仓 / 委托 / 成交 点一行 ⇒ 直接进行情工作台看这只票（唯一出口，勿各写一遍）
  const openWorkbench = useOpenWorkbench();
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
   *
   * ★ 必须显式带上 ``conn_id``（2026-09-20）：后端已把「指定了 conn_id 却取不到该
   * 连接」改成**报错而不是静默回退到 active**（防止多账户下撤错单）；但前端一直不传
   * ⇒ 这道保险等于没接上（本项目反复出现的「能力存在但没人调用」）。列表本来就是
   * active 连接的数据，这里把 active 的 conn_id 一起传下去：若期间用户切了活跃账户，
   * 后端会明确拒绝，而不是把撤单打到另一个账户上。
   */
  const cancelOrder = useCallback(
    async (orderId: string) => {
      setActError(null);
      try {
        await tradeApi.cancel(orderId, active?.conn_id ?? "");
      } catch (e) {
        setActError(
          `撤单失败（${orderId}）：${e instanceof Error ? e.message : String(e)}`,
        );
      } finally {
        void orders.reload();
      }
    },
    [orders, active?.conn_id],
  );

  /** 最新价：实时行情优先，缺失回退券商快照（都不是 0） */
  const lastPrice = (r: Position): number | undefined => {
    const q = quotes[r.code]?.price;
    return isLivePrice(q) ? q : r.price;
  };
  /** 今日涨跌幅：只有实时行情才有，券商持仓接口不返回 */
  const lastPct = (r: Position): number | undefined => quotes[r.code]?.change_pct;

  /**
   * ★ 这一行显示的到底是不是**实时价**。
   *
   * 项目里反复强调「非空 ≠ 够新」，但修复前本页「最新价」列把「实时行情价」和
   * 「券商查询快照价」渲染得一模一样，而市值 / 盈亏 / 盈亏比三列又都由同一个
   * `lastPrice` 派生 ⇒ 用户会把一次查询时的快照（非交易时段就是最近收盘价）
   * 当成实时价，据此判断盈亏。金额类误判代价高，界面必须能区分。
   *
   * 注意这里**不猜测原因**：只陈述「这个值不是实时行情」，原因可能是没连券商、
   * 没订阅到、或刚打开还没推过来 —— 三种都成立，不编故事。
   */
  const isLive = (r: Position): boolean => isLivePrice(quotes[r.code]?.price);
  const posRows = positions.data ?? [];
  const staleCount = posRows.filter((r) => !isLive(r)).length;

  const positionCols: Column<Position>[] = [
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 92, render: (r) => r.name || r.code || "--" },
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
      render: (r) =>
        isLive(r) ? (
          // 实时行情价 —— 正常渲染
          <span style={{ color: toneColor(lastPct(r)) }}>{fmtPrice(lastPrice(r))}</span>
        ) : (
          // 券商查询快照价 —— 加虚线下划线 + hover 说明，避免被当成实时价
          <span
            className={s.stalePrice}
            style={{ color: toneColor(lastPct(r)) }}
            title="非实时：未取到实时行情，这是券商查询快照价（非交易时段即最近收盘价）；市值/盈亏由同一价格派生"
          >
            {fmtPrice(lastPrice(r))}
          </span>
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
        const mv = isLivePrice(p) ? p * r.volume : r.market_value;
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
        const v = isLivePrice(p) && r.cost !== undefined ? (p - r.cost) * r.volume : r.profit;
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
          isLivePrice(p) && r.cost !== undefined && r.cost > 0
            ? ((p - r.cost) / r.cost) * 100
            : r.profit_pct;
        return <span style={{ color: toneColor(v) }}>{fmtPct(v)}</span>;
      },
    },
  ];

  const orderCols: Column<Order>[] = [
    { key: "oid", header: "委托号", width: 110, mono: true, render: (r) => r.order_id },
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 88, render: (r) => r.name || r.code || "--" },
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

      {/* 提示里点名了「连接管理」，就该**真的能点过去** —— 只写在文案里等于让用户
          自己去菜单里找（本项目反复出现的「说了去哪但没入口」）。 */}
      {!connected && (
        <div className={`${s.note} ${s.noteWarn}`}>
          当前无已连接券商。查询类端点会返回 503（零 mock 契约），请先到「连接管理」添加并连接券商。
          <Button
            size="sm"
            variant="ghost"
            onClick={() => open("brokers", {}, { title: "连接管理" })}
          >
            去连接管理
          </Button>
        </div>
      )}

      {/* ★ 快照冒充实时价是本页最隐蔽的误判源：持仓列表里每一行都可能停在不同时刻，
          只看数字完全分辨不出来。没连券商时上面已经有提示，这里只补「连着但仍无行情」。 */}
      {tab === "positions" && connected && staleCount > 0 && (
        <div className={s.note}>
          {staleQuoteNote(
            staleCount,
            posRows.length,
            "非交易时段即最近收盘价；「最新价」（带虚线下划线者）与由其派生的市值、盈亏、盈亏比都不是实时值。",
          )}
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
              onRowClick={(r) => openWorkbench(r.code, r.name)}
              /* 行底色跟「今日涨跌」（行情），盈亏由盈亏列自己着色 ——
                 一处只表达一件事，否则同一行两种颜色互相打架。 */
              rowTone={(r) => tone(lastPct(r))}
            />
          ) : tab === "orders" ? (
            <DataTable
              columns={orderCols}
              rows={orders.data ?? []}
              rowKey={(r) => r.order_id}
              rowHeight={24}
              onRowClick={(r) => openWorkbench(r.code, r.name)}
            />
          ) : (
            <DataTable
              columns={dealCols}
              rows={deals.data ?? []}
              rowKey={(r) => r.deal_id}
              rowHeight={24}
              /* Deal 行没有 name 字段（后端成交记录只带 code） */
              onRowClick={(r) => openWorkbench(r.code)}
            />
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
