import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, ConfirmModal, FormRow, Input, Select } from "@/design/primitives";
import { signalApi, tradeApi, type OrderResult } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, normalizeCode, toneColor } from "@/shared/format";
import type { PriceType, Side } from "@/shared/types";
import s from "./trade.module.css";

/**
 * 手动下单表单 —— **独立「手动交易」页与「行情工作台」共用同一份实现**。
 *
 * ## 为什么必须抽出来
 *
 * 工作台合并了「看盘 + 下单」后，下单逻辑有两处消费者。若各写一份，最容易分叉的
 * 恰恰是最危险的三件事：
 * ① **结果分流**（券商不可用 503 / 风控拒绝 400 / 成功 → 三种文案）；
 * ② **大额二次确认**（含 TOTP 动态码）；
 * ③ **市价单未取到行情时不能按 0 算金额**。
 * 任何一条走偏都会把「没下成功」显示成「已报」，所以只允许一份实现。
 *
 * ## 受控标的
 *
 * 传 `code` 则用父级的标的（工作台里就是当前正在看的那只，改标的不用重填）；
 * 不传则表单自带代码输入框（独立交易页的形态）。
 */
export interface OrderFormProps {
  /** 受控标的；不传则表单自带代码输入框 */
  code?: string;
  /** 父级标的被改（表单内输入框提交时） */
  onCodeChange?: (code: string) => void;
  /** 紧凑模式：工作台底部坞里用，表单项收窄 */
  compact?: boolean;
  /** 下单成功后回调（父级刷新持仓/委托列表） */
  onDone?: () => void;
}

type ResultTone = "success" | "danger" | "warning" | "info";

export function OrderForm({ code: codeProp, onCodeChange, compact = false, onDone }: OrderFormProps) {
  const connections = useBrokerStore((st) => st.connections);
  const activeConn = connections.find((c) => c.active)?.conn_id ?? "";
  const connected = connections.some((c) => c.connected);

  const [innerCode, setInnerCode] = useState("000001.SZ");
  const code = codeProp ?? innerCode;
  const normCode = useMemo(() => normalizeCode(code), [code]);

  const [side, setSide] = useState<Side>("buy");
  const [priceType, setPriceType] = useState<PriceType>("limit");
  const [volume, setVolume] = useState("100");
  const [price, setPrice] = useState("");

  const [banner, setBanner] = useState<{ tone: ResultTone; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<OrderResult | null>(null);
  const [totp, setTotp] = useState("");

  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  // 换标的时清掉上一次的结果提示，避免「上一只票的成功文案」挂在新标的上
  useEffect(() => {
    setBanner(null);
  }, [normCode]);

  /**
   * 预估金额。
   *
   * ⚠️ 拿不到行情时**绝不能**按 `quote?.price ?? 0` 计算 —— 界面显示
   * 「预估金额 0.00」会被读成「这笔不花钱」，比显示 `--` 危险得多。
   */
  const estText = useMemo(() => {
    const vol = Number(volume) || 0;
    if (vol <= 0) return "--";
    const p = priceType === "market" ? quote?.price : Number(price) || 0;
    if (p === undefined || p <= 0) return priceType === "market" ? "未取到市价" : "--";
    return (p * vol).toFixed(2);
  }, [priceType, price, volume, quote?.price]);

  const fillMarketPrice = useCallback(() => {
    if (quote?.price) setPrice(String(quote.price));
  }, [quote?.price]);

  const submitCode = useCallback(
    (raw: string) => {
      const c = normalizeCode(raw.trim());
      if (!c) return;
      if (onCodeChange) onCodeChange(c);
      else setInnerCode(c);
    },
    [onCodeChange],
  );

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
      onDone?.();
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
        onDone?.();
      } else {
        setBanner({ tone: "danger", text: `确认失败：${res.reason ?? "未知原因"}` });
      }
    } catch (e) {
      setBanner({ tone: "danger", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const rowStyle = compact ? { display: "flex", gap: "var(--sp-2)", alignItems: "center" } : undefined;

  return (
    <>
      <div className={compact ? s.formCompact : s.form}>
        {!connected && (
          <div className={s.warnBar}>
            当前无已连接券商，下单将返回 503 引导。请先到「连接管理」连接。
          </div>
        )}

        {codeProp === undefined && (
          <FormRow label="代码">
            <Input
              value={code}
              onChange={(e) => setInnerCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitCode(code);
              }}
              mono
              placeholder="600519 或 600519.SH"
            />
          </FormRow>
        )}

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
            <Button size="sm" variant={side === "buy" ? "buy" : "default"} onClick={() => setSide("buy")}>
              买入
            </Button>
            <Button size="sm" variant={side === "sell" ? "sell" : "default"} onClick={() => setSide("sell")}>
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

        <div className={s.est} style={rowStyle}>
          预估金额 <span className={s.mono}>{estText}</span>
        </div>

        <div className={s.actions}>
          <Button onClick={onPrecheck} disabled={busy}>
            预检
          </Button>
          <Button
            variant={side === "buy" ? "buy" : "sell"}
            onClick={onSubmit}
            disabled={busy}
            block
          >
            {busy ? "提交中…" : side === "buy" ? "买入下单" : "卖出下单"}
          </Button>
        </div>

        {banner && (
          <div className={s.banner} data-tone={banner.tone} role="status" aria-live="polite">
            {banner.text}
          </div>
        )}
      </div>

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
    </>
  );
}

export default OrderForm;
