import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
  type Column,
} from "@/design/primitives";
import { tradeApi, type ConditionOrderPayload } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPrice, normalizeCode } from "@/shared/format";
import type { ConditionOrder, ConditionStatusPayload, PriceType, Side } from "@/shared/types";
import s from "../domain.module.css";

const STATUS_TONE: Record<string, "success" | "danger" | "neutral" | "info" | "warning"> = {
  pending: "info",
  triggered: "success",
  canceled: "neutral",
  expired: "warning",
};

const STATUS_LABEL: Record<string, string> = {
  pending: "监控中",
  triggered: "已触发",
  canceled: "已撤销",
  expired: "已过期",
};

/**
 * 条件单 / 止损单。
 *
 * ★ 契约要点（trade.py:trade_condition_* → engines/condition_order.py）：
 *   - 触发价字段是 trigger_price（**不是** trigger_value）
 *   - trigger_type 仅支持 gte（价格≥触发价，突破买入/止损卖出）/ lte（价格≤触发价）
 *   - volume 须为 100 的整数倍；valid_days=0 仅当日有效，N>0 为 N 个自然日
 *   - GET 返回 {running, interval, total, pending, orders}，orders 主键字段是 id
 *   - 跨日续作：pending 条件单不会因换日丢失；拒单会进重试队列（当日 5 次 + 次日 3 次）
 */
export function Conditions() {
  const [code, setCode] = useState("000001.SZ");
  const [side, setSide] = useState<Side>("buy");
  const [triggerType, setTriggerType] = useState<"gte" | "lte">("gte");
  const [triggerPrice, setTriggerPrice] = useState("");
  const [volume, setVolume] = useState("100");
  const [priceType, setPriceType] = useState<PriceType>("market");
  const [price, setPrice] = useState("");
  const [validDays, setValidDays] = useState("0");
  const [remark, setRemark] = useState("");

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const normCode = useMemo(() => normalizeCode(code), [code]);
  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  const st = useAsync<ConditionStatusPayload>(() => tradeApi.conditions(), []);
  const orders = st.data?.orders ?? [];

  const submit = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const payload: ConditionOrderPayload = {
        code: normCode,
        side,
        trigger_type: triggerType,
        trigger_price: Number(triggerPrice) || 0,
        volume: Number(volume) || 0,
        price_type: priceType,
        price: Number(price) || 0,
        remark,
        valid_days: Number(validDays) || 0,
      };
      const res = await tradeApi.createCondition(payload);
      setBanner({
        tone: "ok",
        text: `条件单已建立：${res.id}（有效期至 ${res.expire_at}）`,
      });
      await st.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (cid: string) => {
    setBusy(true);
    setBanner(null);
    try {
      await tradeApi.cancelCondition(cid);
      setBanner({ tone: "ok", text: `已撤销条件单 ${cid}` });
      await st.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cols: Column<ConditionOrder>[] = [
    { key: "id", header: "条件单号", width: 104, mono: true, render: (r) => r.id },
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
    {
      key: "trig",
      header: "触发条件",
      width: 132,
      mono: true,
      render: (r) => `${r.trigger_type === "gte" ? "≥" : "≤"} ${fmtPrice(r.trigger_price)}`,
    },
    { key: "vol", header: "数量", width: 72, align: "right", mono: true, render: (r) => String(r.volume) },
    {
      key: "pt",
      header: "委托",
      width: 96,
      render: (r) => (r.price_type === "market" ? "市价" : `限价 ${fmtPrice(r.price)}`),
    },
    {
      key: "status",
      header: "状态",
      width: 78,
      render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "neutral"}>{STATUS_LABEL[r.status] ?? r.status}</Badge>,
    },
    { key: "created", header: "创建", width: 148, mono: true, render: (r) => r.created_at },
    { key: "expire", header: "到期", width: 148, mono: true, render: (r) => r.expire_at || "--" },
    {
      key: "retry",
      header: "重试",
      width: 72,
      align: "right",
      mono: true,
      render: (r) => `${r.retry_count ?? 0}/${r.intraday_retry ?? 0}`,
    },
    {
      key: "oid",
      header: "触发委托号",
      width: 110,
      mono: true,
      render: (r) => r.order_id || "--",
    },
    {
      key: "act",
      header: "操作",
      width: 64,
      render: (r) =>
        r.status === "pending" || r.status === "triggered" ? (
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void cancel(r.id)}>
            撤销
          </Button>
        ) : null,
    },
  ];

  return (
    <div className={s.page}>
      <div className={s.split}>
        <Panel title="新建条件单">
          <div className={s.form}>
            <FormRow label="代码">
              <Input value={code} onChange={(e) => setCode(e.target.value)} mono placeholder="600519 或 600519.SH" />
            </FormRow>

            {quote && (
              <div className={s.note}>
                {quote.name ?? normCode} · 最新 <span className={s.mono}>{fmtPrice(quote.price)}</span>
              </div>
            )}

            <FormRow label="方向">
              <div className={s.actions}>
                <Button size="sm" variant={side === "buy" ? "buy" : "default"} onClick={() => setSide("buy")}>
                  买入
                </Button>
                <Button size="sm" variant={side === "sell" ? "sell" : "default"} onClick={() => setSide("sell")}>
                  卖出
                </Button>
              </div>
            </FormRow>

            <FormRow label="触发方式">
              <Select
                value={triggerType}
                onChange={(e) => setTriggerType(e.target.value as "gte" | "lte")}
                options={[
                  { value: "gte", label: "价格 ≥ 触发价（突破买入 / 止损卖出）" },
                  { value: "lte", label: "价格 ≤ 触发价（回落买入 / 止盈卖出）" },
                ]}
              />
            </FormRow>

            <FormRow label="触发价">
              <div className={s.inline}>
                <Input value={triggerPrice} onChange={(e) => setTriggerPrice(e.target.value)} mono placeholder="0.00" />
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!quote?.price}
                  onClick={() => quote?.price && setTriggerPrice(String(quote.price))}
                >
                  最新价
                </Button>
              </div>
            </FormRow>

            <FormRow label="数量">
              <Input value={volume} onChange={(e) => setVolume(e.target.value)} mono placeholder="100 的整数倍" />
            </FormRow>

            <FormRow label="委托类型">
              <Select
                value={priceType}
                onChange={(e) => setPriceType(e.target.value as PriceType)}
                options={[
                  { value: "market", label: "市价" },
                  { value: "limit", label: "限价" },
                ]}
              />
            </FormRow>

            {priceType === "limit" && (
              <FormRow label="限价">
                <Input value={price} onChange={(e) => setPrice(e.target.value)} mono />
              </FormRow>
            )}

            <FormRow label="有效期(天)">
              <Input value={validDays} onChange={(e) => setValidDays(e.target.value)} mono placeholder="0=仅当日" />
            </FormRow>

            <FormRow label="备注">
              <Input value={remark} onChange={(e) => setRemark(e.target.value)} />
            </FormRow>

            <Button block disabled={busy} onClick={() => void submit()}>
              {busy ? "提交中…" : "建立条件单"}
            </Button>

            {banner && (
              <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
            )}

            <div className={s.note}>
              引擎按 2s 轮询真实行情触发；触发后经统一风控下单。拒单会自动重试
              （当日最多 5 次、次日最多 3 次），跨自然日 pending 条件单继续监控。
            </div>
          </div>
        </Panel>

        <Panel
          flush
          title={`条件单监控（监控中 ${st.data?.pending ?? 0} / 共 ${st.data?.total ?? 0}）`}
          extra={
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <Badge tone={st.data?.running ? "success" : "neutral"}>
                {st.data?.running ? `运行中 · ${st.data?.interval ?? 2}s` : "未运行"}
              </Badge>
              <Button size="sm" variant="ghost" onClick={() => void st.reload()}>
                刷新
              </Button>
            </div>
          }
        >
          <div className={s.tableArea}>
            {st.loading && !st.data ? (
              <Spinner label="加载中…" />
            ) : st.error ? (
              <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                {st.error}
              </div>
            ) : orders.length === 0 ? (
              <EmptyState text="暂无条件单" />
            ) : (
              <DataTable columns={cols} rows={orders} rowKey={(r) => r.id} rowHeight={24} />
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

export default Conditions;
