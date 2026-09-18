import { useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  type Column,
} from "@/design/primitives";
import { portfolioApi } from "@/services/api";
import { ConnSelect } from "@/domains/research/ConnSelect";
import { fmtMoney, fmtPrice, normalizeCode } from "@/shared/format";
import s from "../domain.module.css";

/**
 * 分仓再平衡（POST /rebalance）。
 *
 * 后端能力早已就绪（targets 等权篮子 → 调仓单，含阈值过滤、拆单、涨跌停跳过），
 * 但前端长期是占位页 —— 于是这项能力对使用者等于不存在。这里补上界面。
 *
 * ★ 后端契约（app/routes/rebalance.py）：
 *   - body: {targets:[{code, target_ratio}], delta_min, delta_max, do_trade, conn_id}
 *   - 返回 {orders:[...], generated:N}；order 行可能是
 *     {code, direction, volume, price}（调仓单）或 {code, skipped:"limit", diff}
 *     （涨跌停跳过），**跳过行没有 direction**，渲染时必须区分。
 *   - do_trade=true 会**真实下单**（经 SignalRouter 统一链路，过风控）。
 */
export interface OrderRow {
  code: string;
  direction?: string;
  volume?: number;
  price?: number;
  skipped?: string;
  diff?: number;
  order?: unknown;
}

interface TargetRow {
  code: string;
  ratio: string;
}

/**
 * 调仓计划表列定义（导出以便单测）。
 *
 * ★ 为什么导出：``DataTable`` 是虚拟滚动表格，jsdom 下容器高度为 0 ⇒ **行根本不渲染**，
 * 页面级断言拿不到行内容。因此把列定义导出，直接对 ``render`` 单测（与
 * ``domains/research/screen/ScreenPanels::resultCols`` 同一套路）。
 *
 * 这里最该锁的是**跳过行**：涨跌停跳过的行只有 ``{code, skipped:"limit", diff}``，
 * **没有 direction** —— 若按「买入/卖出」渲染就会把「没调」显示成「调了」，
 * 属于会误导决策的静默错误。
 */
export const orderCols: Column<OrderRow>[] = [
  { key: "code", header: "代码", width: 110, mono: true, render: (r) => r.code },
  {
    key: "dir",
    header: "方向",
    width: 68,
    render: (r) =>
      r.skipped ? (
        <Badge tone="neutral">跳过</Badge>
      ) : (
        <span style={{ color: r.direction === "buy" ? "var(--up)" : "var(--down)" }}>
          {r.direction === "buy" ? "买入" : "卖出"}
        </span>
      ),
  },
  {
    key: "vol",
    header: "数量",
    width: 82,
    align: "right",
    mono: true,
    render: (r) => (r.volume === undefined ? "--" : String(r.volume)),
  },
  {
    key: "price",
    header: "价格",
    width: 82,
    align: "right",
    mono: true,
    render: (r) => fmtPrice(r.price),
  },
  {
    key: "diff",
    header: "差额",
    width: 104,
    align: "right",
    mono: true,
    render: (r) => (r.diff === undefined ? "--" : fmtMoney(r.diff)),
  },
  {
    key: "note",
    header: "说明",
    render: (r) =>
      r.skipped === "limit" ? (
        <span style={{ color: "var(--warning)" }}>触及涨跌停，本次不调</span>
      ) : r.order ? (
        <span className={s.muted}>已提交下单，可在委托页查看结果</span>
      ) : (
        <span className={s.muted}>计划（未下单）</span>
      ),
  },
];

export function Rebalance() {
  const [targets, setTargets] = useState<TargetRow[]>([
    { code: "600519.SH", ratio: "0.5" },
    { code: "000001.SZ", ratio: "0.5" },
  ]);
  const [connId, setConnId] = useState("");
  const [deltaMin, setDeltaMin] = useState("3000");
  const [deltaMax, setDeltaMax] = useState("30000");
  const [doTrade, setDoTrade] = useState("0");

  const [orders, setOrders] = useState<OrderRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [confirm, setConfirm] = useState(false);

  const parsed = targets
    .map((t) => ({ code: normalizeCode(t.code), ratio: Number(t.ratio) }))
    .filter((t) => t.code && Number.isFinite(t.ratio) && t.ratio >= 0);

  const run = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await portfolioApi.rebalance({
        targets: parsed.map((t) => ({ code: t.code, target_ratio: t.ratio })),
        delta_min: Number(deltaMin) || 3000,
        delta_max: Number(deltaMax) || 30000,
        do_trade: doTrade === "1",
        conn_id: connId,
      });
      const rows = (res as { orders?: OrderRow[] }).orders ?? [];
      setOrders(rows);
      const skipped = rows.filter((r) => r.skipped).length;
      setBanner({
        tone: "ok",
        text: `生成 ${(res as { generated?: number }).generated ?? rows.length} 条调仓单`
          + (skipped ? `，${skipped} 条因涨跌停跳过` : ""),
      });
    } catch (e) {
      setOrders(null);
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={s.page}>
      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>
          {banner.text}
        </div>
      )}

      <Panel title="目标权重">
        <div className={s.form}>
          {targets.map((t, i) => (
            <div key={i} style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Input
                value={t.code}
                mono
                style={{ width: 130 }}
                placeholder="代码"
                onChange={(e) =>
                  setTargets((prev) =>
                    prev.map((x, j) => (j === i ? { ...x, code: e.target.value } : x)),
                  )
                }
              />
              <Input
                value={t.ratio}
                mono
                style={{ width: 100 }}
                placeholder="目标比例"
                onChange={(e) =>
                  setTargets((prev) =>
                    prev.map((x, j) => (j === i ? { ...x, ratio: e.target.value } : x)),
                  )
                }
              />
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setTargets((prev) => prev.filter((_, j) => j !== i))}
              >
                删除
              </Button>
            </div>
          ))}
          <div style={{ display: "flex", gap: 8 }}>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setTargets((prev) => [...prev, { code: "", ratio: "0" }])}
            >
              添加标的
            </Button>
          </div>

          <div className={s.cols4}>
            <FormRow label="账户连接">
              <ConnSelect value={connId} onChange={setConnId} />
            </FormRow>
            <FormRow label="最小调仓额">
              <Input value={deltaMin} mono onChange={(e) => setDeltaMin(e.target.value)} />
            </FormRow>
            <FormRow label="单笔上限">
              <Input value={deltaMax} mono onChange={(e) => setDeltaMax(e.target.value)} />
            </FormRow>
            <FormRow label="是否下单">
              <Select
                value={doTrade}
                onChange={(e) => setDoTrade(e.target.value)}
                options={[
                  { value: "0", label: "仅生成计划" },
                  { value: "1", label: "真实下单" },
                ]}
              />
            </FormRow>
          </div>

          <Button
            block
            disabled={busy || parsed.length === 0}
            onClick={() => {
              if (doTrade === "1") {
                setConfirm(true);
                return;
              }
              void run();
            }}
          >
            {busy ? "计算中…" : "生成调仓计划"}
          </Button>

          <div className={s.note}>
            按「目标市值 = 总资产 × 目标比例」计算差额，差额绝对值小于最小调仓额的标的会被跳过；
            单笔超过上限会自动拆成多笔。触及涨跌停的标的本次不调（标记为「跳过」）。
            <b>真实下单</b>会经统一信号链路（风控 + 幂等），不可撤销。
          </div>
        </div>
      </Panel>

      <Panel flush className={s.grow} title={`调仓计划（${orders?.length ?? 0}）`}>
        <div className={s.tableArea}>
          {!orders ? (
            <EmptyState text="尚未生成调仓计划" />
          ) : orders.length === 0 ? (
            <EmptyState text="无需调仓（所有标的差额均低于阈值）" />
          ) : (
            <DataTable columns={orderCols} rows={orders} rowKey={(r, i) => `${r.code}-${i}`} rowHeight={24} />
          )}
        </div>
      </Panel>

      <ConfirmModal
        open={confirm}
        title="确认真实下单"
        danger
        confirmText="确认下单"
        message={
          <>
            将按当前计划<b>用真实资金下单</b>，共 {parsed.length} 只标的。
            <br />
            若要先看看计划，请改回「仅生成计划」。
          </>
        }
        warn="下单后不可撤销"
        onConfirm={() => {
          setConfirm(false);
          void run();
        }}
        onCancel={() => setConfirm(false)}
      />
    </div>
  );
}

export default Rebalance;
