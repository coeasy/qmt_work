import { useCallback, useEffect, useMemo, useState } from "react";
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
  Tabs,
  type Column,
} from "@/design/primitives";
import { signalApi, tradeApi, type OrderResult } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import {
  fmtPct,
  fmtPrice,
  normalizeCode,
  orderStatusLabel,
  orderStatusTone,
  toneColor,
} from "@/shared/format";
import type { Deal, Order, Position, PriceType, Side } from "@/shared/types";
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
 */
export function Trade() {
  const connections = useBrokerStore((st) => st.connections);
  const activeConn = connections.find((c) => c.active)?.conn_id ?? "";
  const connected = connections.some((c) => c.connected);

  const [code, setCode] = useState("000001.SZ");
  const [side, setSide] = useState<Side>("buy");
  const [priceType, setPriceType] = useState<PriceType>("limit");
  const [volume, setVolume] = useState("100");
  const [price, setPrice] = useState("");

  const [banner, setBanner] = useState<Banner | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<OrderResult | null>(null);
  const [totp, setTotp] = useState("");
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

  const normCode = useMemo(() => normalizeCode(code), [code]);
  const sub = useMemo(() => (normCode ? [normCode] : []), [normCode]);
  useQuoteSubscription(sub);
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  /**
   * 预估金额。
   *
   * ⚠️ 修复前市价单在**没拿到行情**时按 `quote?.price ?? 0` 计算，界面显示
   * 「预估金额 0.00」—— 这会被读成「这笔不花钱」，比显示 `--` 危险得多。
   * 拿不到价格就如实说拿不到，不编数字。
   */
  const estText = useMemo(() => {
    const vol = Number(volume) || 0;
    if (vol <= 0) return "--";
    const p = priceType === "market" ? quote?.price : Number(price) || 0;
    if (p === undefined || p <= 0) return priceType === "market" ? "未取到市价" : "--";
    return (p * vol).toFixed(2);
  }, [priceType, price, volume, quote?.price]);

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

  const fillMarketPrice = useCallback(() => {
    if (quote?.price) setPrice(String(quote.price));
  }, [quote?.price]);

  /** 预检：只读校验，不产生委托 */
  const onPrecheck = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await tradeApi.precheck({
        conn_id: activeConn,
        code: normCode,
        direction: side,
        volume: Number(volume) || 0,
        price: priceType === "market" ? 0 : Number(price) || 0,
        price_type: priceType,
      });
      // 契约：后端返回 {allowed, reason}，字段名不是 ok，也没有 est_amount
      setBanner(
        res.allowed
          ? { tone: "info", text: "预检通过：当前风控参数下该委托可放行（预检不计入日级用量）" }
          : { tone: "warning", text: `预检未通过：${res.reason || "未知原因"}` },
      );
    } catch (e) {
      setBanner({ tone: "danger", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  /** 提交委托。经 SignalRouter 统一链路，可能返回「挂起待确认」 */
  const onSubmit = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await tradeApi.order({
        conn_id: activeConn,
        code: normCode,
        direction: side,
        volume: Number(volume) || 0,
        price: priceType === "market" ? 0 : Number(price) || 0,
        price_type: priceType,
      });

      if (res.pending_confirmation && res.confirm_token) {
        setPending(res);
        setBanner({
          tone: "warning",
          text: `大额委托待二次确认${res.amount ? `（金额 ${res.amount.toFixed(2)}）` : ""}`,
        });
        return;
      }

      if (!res.ok) {
        setBanner({ tone: "danger", text: `下单被拒：${res.reason ?? "未知原因"}` });
        return;
      }

      setBanner({ tone: "success", text: `委托已提交，委托号 ${res.order_id ?? "—"}` });
      await refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // 503 = 券商不可用，语义与「被拒」不同，需分别引导
      const unavailable = msg.includes("券商") || msg.includes("503");
      setBanner({
        tone: unavailable ? "warning" : "danger",
        text: unavailable ? `${msg} —— 请到「连接管理」添加并连接券商` : msg,
      });
    } finally {
      setBusy(false);
    }
  };

  /** 二次确认：回传完整参数（含 price_type），避免市价单被当限价单复检 */
  const onConfirm = async () => {
    if (!pending?.confirm_token) return;
    setBusy(true);
    try {
      const res = await signalApi.confirm(pending.confirm_token, totp);
      if (res.ok) {
        setBanner({ tone: "success", text: `委托已提交，委托号 ${res.order_id ?? "—"}` });
        setPending(null);
        setTotp("");
        await refresh();
      } else {
        setBanner({ tone: "danger", text: `确认失败：${res.reason ?? "未知原因"}` });
      }
    } catch (e) {
      setBanner({ tone: "danger", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

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
      const diff = (res && typeof res === "object" && "diff" in res) ? res.diff : undefined;
      const diffText = typeof diff === "number" ? `；需调仓 ${diff}` : "";
      setTargetBanner({
        tone: targetDoTrade ? "success" : "info",
        text: targetDoTrade
          ? `已提交目标持仓调仓${diffText}`
          : `测算完成（未下单）${diffText}`,
      });
    } catch (e) {
      setTargetBanner({ tone: "danger", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setTargetBusy(false);
    }
  };

  const dealCols: Column<Deal>[] = [
    { key: "did", header: "成交号", width: 100, mono: true, render: (r) => r.deal_id },
    { key: "code", header: "代码", width: 92, mono: true, render: (r) => r.code },
    { key: "side", header: "方向", width: 52, render: (r) => (r.side === "buy" ? "买入" : "卖出") },
    { key: "price", header: "价格", width: 72, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "vol", header: "数量", width: 68, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "amt", header: "金额", width: 96, align: "right", mono: true, render: (r) => fmtPrice(r.amount) },
    { key: "time", header: "时间", width: 82, mono: true, render: (r) => r.time ?? "--" },
  ];

  return (
    <div className={s.wrap}>
      <div className={s.left}>
        <Panel title="下单">
          <div className={s.form}>
            {!connected && (
              <div className={s.warnBar}>当前无已连接券商，下单将返回 503 引导。请先到「连接管理」连接。</div>
            )}

            <FormRow label="代码">
              <Input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                mono
                placeholder="600519 或 600519.SH"
              />
            </FormRow>

            {quote && (
              <div className={s.quoteLine}>
                <span>{quote.name ?? normCode}</span>
                <span className={s.mono} style={{ color: toneColor(quote.change_pct) }}>
                  {fmtPrice(quote.price)} {fmtPct(quote.change_pct)}
                </span>
              </div>
            )}

            <FormRow label="方向">
              <div className={s.sideGroup}>
                <Button
                  size="sm"
                  variant={side === "buy" ? "buy" : "default"}
                  onClick={() => setSide("buy")}
                >
                  买入
                </Button>
                <Button
                  size="sm"
                  variant={side === "sell" ? "sell" : "default"}
                  onClick={() => setSide("sell")}
                >
                  卖出
                </Button>
              </div>
            </FormRow>

            <FormRow label="类型">
              <Select
                value={priceType}
                onChange={(e) => setPriceType(e.target.value as PriceType)}
                options={[
                  { value: "limit", label: "限价" },
                  { value: "market", label: "市价" },
                ]}
              />
            </FormRow>

            <FormRow label="价格">
              <div className={s.inline}>
                <Input
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  mono
                  disabled={priceType === "market"}
                  placeholder={priceType === "market" ? "市价" : "0.00"}
                />
                <Button size="sm" variant="ghost" onClick={fillMarketPrice} disabled={!quote?.price}>
                  最新价
                </Button>
              </div>
            </FormRow>

            <FormRow label="数量">
              <Input
                value={volume}
                onChange={(e) => setVolume(e.target.value)}
                mono
                placeholder="100 的整数倍"
              />
            </FormRow>

            <div className={s.est}>
              预估金额{" "}
              <span className={s.mono}>{estText}</span>
            </div>

            <div className={s.actions}>
              <Button onClick={onPrecheck} disabled={busy}>
                预检
              </Button>
              <Button variant={side === "buy" ? "buy" : "sell"} onClick={onSubmit} disabled={busy} block>
                {busy ? "提交中…" : side === "buy" ? "买入下单" : "卖出下单"}
              </Button>
            </div>

            {banner && (
              <div
                className={s.banner}
                data-tone={banner.tone}
                role="status"
                aria-live="polite"
              >
                {banner.text}
              </div>
            )}
          </div>
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
                <DataTable columns={positionCols} rows={positions} rowKey={(r) => r.code} rowTone={(r) => (r.profit_pct && r.profit_pct > 0 ? 1 : r.profit_pct && r.profit_pct < 0 ? -1 : 0)} />
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
            <Button
              variant="primary"
              onClick={() => void onTarget()}
              disabled={targetBusy}
              block
            >
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
        loading={busy}
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
        onCancel={() => setCancelTarget(null)}
        onConfirm={() => {
          const id = cancelTarget?.order_id;
          setCancelTarget(null);
          if (!id) return;
          void tradeApi
            .cancel(id, activeConn)
            .then(refresh)
            .catch((e: unknown) =>
              setBanner({ tone: "danger", text: e instanceof Error ? e.message : String(e) }),
            );
        }}
      />

      <ConfirmModal
        open={pending !== null}
        danger
        title="大额委托二次确认"
        warn="实盘委托：确认后将真实发送至券商柜台"
        confirmText="确认下单"
        loading={busy}
        onCancel={() => {
          setPending(null);
          setTotp("");
        }}
        onConfirm={() => void onConfirm()}
        message={
          <div className={s.confirmBody}>
            <div className={s.confirmRow}>
              <span>标的</span>
              <span className={s.mono}>{normCode}</span>
            </div>
            <div className={s.confirmRow}>
              <span>方向</span>
              <span style={{ color: side === "buy" ? "var(--up)" : "var(--down)" }}>
                {side === "buy" ? "买入" : "卖出"}
              </span>
            </div>
            <div className={s.confirmRow}>
              <span>数量</span>
              <span className={s.mono}>{volume}</span>
            </div>
            <div className={s.confirmRow}>
              <span>类型</span>
              <span>{priceType === "market" ? "市价" : "限价"}</span>
            </div>
            {pending?.amount !== undefined && (
              <div className={s.confirmRow}>
                <span>金额</span>
                <span className={s.mono}>{pending.amount.toFixed(2)}</span>
              </div>
            )}
            {pending?.requires_totp && (
              <div className={s.confirmRow}>
                <span>动态码</span>
                <Input
                  value={totp}
                  onChange={(e) => setTotp(e.target.value)}
                  mono
                  placeholder="6 位 TOTP"
                  style={{ width: 120 }}
                />
              </div>
            )}
          </div>
        }
      />
    </div>
  );
}

export default Trade;
