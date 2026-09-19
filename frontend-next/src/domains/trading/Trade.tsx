import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Tabs,
  type Column,
} from "@/design/primitives";
import { tradeApi } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import { fmtPct, fmtPrice, normalizeCode, orderStatusLabel, orderStatusTone } from "@/shared/format";
import type { Deal, Order, Position } from "@/shared/types";
import { OrderForm } from "./OrderForm";
import s from "./trade.module.css";

type ResultTone = "success" | "danger" | "warning" | "info";

interface Banner {
  tone: ResultTone;
  text: string;
}

/**
 * 手动交易页。
 *
 * ★ 完整下单流程（对齐方案 §5 流程 3）：
 *   预检 → 提交 → （大额）二次确认（含 TOTP）→ 结果分流
 *
 * 结果分流铁律（与后端 trade.py:80-90 的语义一致）：
 *   - 券商不可用（503）→ 引导去连接，绝不显示为「已报」
 *   - 风控/柜台拒绝（400）→ 显示真实原因
 *   - 成功 → 显示委托号
 * 绝不把失败粉饰成成功。
 *
 * ★ 下单表单已抽到 `./OrderForm`，与「行情工作台」共用同一份实现 ——
 *   否则结果分流 / 大额二次确认 / 市价无行情时不按 0 算金额这三条最容易分叉，
 *   任何一条走偏都会把「没下成功」显示成「已报」。本页只负责**下单后的账户视图**：
 *   持仓 / 委托 / 成交三表 + 撤单（不可逆，模态确认）+ 目标持仓调仓。
 */
export function Trade() {
  const connections = useBrokerStore((st) => st.connections);
  const activeConn = connections.find((c) => c.active)?.conn_id ?? "";

  /** 撤单结果与撤单相关提示（下单自身的提示在 OrderForm 内） */
  const [banner, setBanner] = useState<Banner | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  /** 待撤单委托：撤单不可逆（已成交部分不回滚），必须模态确认后才发请求 */
  const [cancelTarget, setCancelTarget] = useState<Order | null>(null);

  /** 目标持仓调仓（POST /trade/target） */
  const [targetCode, setTargetCode] = useState("");
  const [targetPct, setTargetPct] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [targetDoTrade, setTargetDoTrade] = useState(false);
  const [targetBusy, setTargetBusy] = useState(false);
  const [targetBanner, setTargetBanner] = useState<Banner | null>(null);

  const [tab, setTab] = useState<"positions" | "orders" | "deals">("positions");
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [deals, setDeals] = useState<Deal[]>([]);

  const refresh = useCallback(async () => {
    try {
      // 契约：positions/orders/deals 只作用于 active 连接；
      // positions 的 query 参数是 symbol（按标的过滤），不是 conn_id。
      const [p, o, d] = await Promise.all([
        tradeApi.positions(),
        tradeApi.orders(),
        tradeApi.deals(),
      ]);
      setPositions(p ?? []);
      setOrders(o ?? []);
      setDeals(d ?? []);
    } catch {
      /* 未连接券商时静默，banner 已由下单流程给出提示 */
    }
  }, []);

  // 挂载时拉取一次账户持仓/委托/成交。此前 refresh 只在下单后与手动点「刷新」时
  // 触发，导致首次打开页面三个页签恒为空（阶段 3 本地实测发现的 UX 缺陷）。
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const positionCols: Column<Position>[] = [
    { key: "code", header: "代码", width: 92, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 88, render: (r) => r.name ?? "--" },
    { key: "volume", header: "持仓", width: 70, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "avail", header: "可用", width: 70, align: "right", mono: true, render: (r) => String(r.avail ?? "--") },
    { key: "cost", header: "成本", width: 72, align: "right", mono: true, render: (r) => fmtPrice(r.cost) },
    { key: "price", header: "现价", width: 72, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "mv", header: "市值", width: 90, align: "right", mono: true, render: (r) => fmtPrice(r.market_value) },
    {
      key: "pct",
      header: "盈亏比",
      width: 78,
      align: "right",
      mono: true,
      render: (r) => fmtPct(r.profit_pct),
    },
  ];

  const orderCols: Column<Order>[] = [
    { key: "oid", header: "委托号", width: 100, mono: true, render: (r) => r.order_id },
    { key: "code", header: "代码", width: 92, mono: true, render: (r) => r.code },
    {
      key: "side",
      header: "方向",
      width: 52,
      render: (r) => (
        <span style={{ color: r.side === "buy" ? "var(--up)" : "var(--down)" }}>
          {r.side === "buy" ? "买入" : "卖出"}
        </span>
      ),
    },
    { key: "price", header: "价格", width: 72, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "vol", header: "数量", width: 68, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "filled", header: "已成交", width: 68, align: "right", mono: true, render: (r) => String(r.filled ?? 0) },
    {
      key: "status",
      header: "状态",
      width: 76,
      render: (r) => (
        // ★ 走唯一入口：此前裸渲染 {r.status}，用户看到 partial/cancelled 原始词
        <Badge tone={orderStatusTone(r.status)}>{orderStatusLabel(r.status)}</Badge>
      ),
    },
    { key: "time", header: "时间", width: 82, mono: true, render: (r) => r.time ?? "--" },
    {
      key: "act",
      header: "",
      width: 54,
      render: (r) => (
        <Button size="sm" variant="ghost" onClick={() => setCancelTarget(r)}>
          撤单
        </Button>
      ),
    },
  ];

  const dealCols: Column<Deal>[] = [
    { key: "did", header: "成交号", width: 100, mono: true, render: (r) => r.deal_id },
    { key: "code", header: "代码", width: 92, mono: true, render: (r) => r.code },
    { key: "side", header: "方向", width: 52, render: (r) => (r.side === "buy" ? "买入" : "卖出") },
    { key: "price", header: "价格", width: 72, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "vol", header: "数量", width: 68, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "amt", header: "金额", width: 96, align: "right", mono: true, render: (r) => fmtPrice(r.amount) },
    { key: "time", header: "时间", width: 82, mono: true, render: (r) => r.time ?? "--" },
  ];

  /** 目标持仓调仓：把某标的调整到目标仓位比例（do_trade=false 仅测算）。 */
  const onTarget = async () => {
    const code = normalizeCode(targetCode);
    if (!code) {
      setTargetBanner({ tone: "warning", text: "请填写标的代码" });
      return;
    }
    const pct = Number(targetPct) || 0;
    setTargetBusy(true);
    setTargetBanner(null);
    try {
      const res = await tradeApi.target({
        code,
        target_pct: pct,
        price: targetPrice ? Number(targetPrice) || 0 : 0,
        do_trade: targetDoTrade,
      });
      const diff = res && typeof res === "object" && "diff" in res ? res.diff : undefined;
      const diffText = typeof diff === "number" ? `；需调仓 ${diff}` : "";
      setTargetBanner({
        tone: targetDoTrade ? "success" : "info",
        text: targetDoTrade ? `已提交目标持仓调仓${diffText}` : `测算完成（未下单）${diffText}`,
      });
    } catch (e) {
      setTargetBanner({ tone: "danger", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setTargetBusy(false);
    }
  };

  return (
    <div className={s.wrap}>
      <div className={s.left}>
        <Panel title="下单">
          <OrderForm onDone={refresh} />
        </Panel>
      </div>

      <div className={s.right}>
        <Panel
          flush
          title="账户持仓与委托"
          extra={
            <Button size="sm" variant="ghost" onClick={() => void refresh()}>
              刷新
            </Button>
          }
        >
          {banner && (
            <div className={s.banner} data-tone={banner.tone} role="status" aria-live="polite">
              {banner.text}
            </div>
          )}
          <Tabs
            items={[
              { key: "positions", label: `持仓 ${positions.length}` },
              { key: "orders", label: `委托 ${orders.length}` },
              { key: "deals", label: `成交 ${deals.length}` },
            ]}
            value={tab}
            onChange={setTab}
          />
          <div className={s.tableArea}>
            {tab === "positions" &&
              (positions.length ? (
                <DataTable
                  columns={positionCols}
                  rows={positions}
                  rowKey={(r) => r.code}
                  rowTone={(r) =>
                    r.profit_pct && r.profit_pct > 0 ? 1 : r.profit_pct && r.profit_pct < 0 ? -1 : 0
                  }
                />
              ) : (
                <EmptyState text="暂无持仓数据" />
              ))}
            {tab === "orders" &&
              (orders.length ? (
                <DataTable columns={orderCols} rows={orders} rowKey={(r) => r.order_id} />
              ) : (
                <EmptyState text="暂无委托" />
              ))}
            {tab === "deals" &&
              (deals.length ? (
                <DataTable columns={dealCols} rows={deals} rowKey={(r) => r.deal_id} />
              ) : (
                <EmptyState text="暂无成交" />
              ))}
          </div>
        </Panel>
      </div>

      <Panel title="目标持仓调仓">
        <div className={s.form}>
          <div className={s.warnBar}>
            把某标的调整到目标仓位比例。关闭「真实下单」时仅测算差额，不动账。
          </div>
          <FormRow label="标的">
            <Input
              value={targetCode}
              onChange={(e) => setTargetCode(e.target.value)}
              mono
              placeholder="600519 或 600519.SH"
            />
          </FormRow>
          <FormRow label="目标比例">
            <Input
              value={targetPct}
              onChange={(e) => setTargetPct(e.target.value)}
              mono
              placeholder="0–1，如 0.1 = 10%"
            />
          </FormRow>
          <FormRow label="价格">
            <Input
              value={targetPrice}
              onChange={(e) => setTargetPrice(e.target.value)}
              mono
              placeholder="0 = 市价"
            />
          </FormRow>
          <label className={s.checkRow}>
            <input
              type="checkbox"
              checked={targetDoTrade}
              onChange={(e) => setTargetDoTrade(e.target.checked)}
            />
            真实下单（否则仅测算）
          </label>
          <div className={s.actions}>
            <Button variant="primary" onClick={() => void onTarget()} disabled={targetBusy} block>
              {targetBusy ? "提交中…" : "执行目标持仓调仓"}
            </Button>
          </div>
          {targetBanner && (
            <div className={s.banner} data-tone={targetBanner.tone} role="status" aria-live="polite">
              {targetBanner.text}
            </div>
          )}
        </div>
      </Panel>

      <ConfirmModal
        open={cancelTarget !== null}
        danger
        title="撤单确认"
        warn="撤单不可撤销，已成交部分不会回滚"
        confirmText="确认撤单"
        loading={cancelBusy}
        message={
          cancelTarget ? (
            <>
              即将撤销委托 <b>{cancelTarget.order_id}</b>
              {cancelTarget.name
                ? `（${cancelTarget.name} ${cancelTarget.code}）`
                : `（${cancelTarget.code}）`}
              ：{cancelTarget.side === "buy" ? "买入" : "卖出"} {cancelTarget.volume} 股 @{" "}
              {fmtPrice(cancelTarget.price)}。
              <br />
              已成交的 <b>{cancelTarget.filled ?? 0}</b> 股<b>不会回滚</b>。
            </>
          ) : null
        }
        onCancel={() => setCancelTarget(null)}
        onConfirm={() => {
          const id = cancelTarget?.order_id;
          setCancelTarget(null);
          if (!id) return;
          setCancelBusy(true);
          setBanner(null);
          void tradeApi
            .cancel(id, activeConn)
            .then(async () => {
              setBanner({ tone: "success", text: `撤单已提交：${id}` });
              await refresh();
            })
            .catch((e: unknown) =>
              setBanner({
                tone: "danger",
                text: `撤单失败：${e instanceof Error ? e.message : String(e)}`,
              }),
            )
            .finally(() => setCancelBusy(false));
        }}
      />
    </div>
  );
}

export default Trade;
