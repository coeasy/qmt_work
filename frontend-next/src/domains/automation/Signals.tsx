import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
} from "@/design/primitives";
import { signalApi, type OrderResult, type SignalMode } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPrice, normalizeCode } from "@/shared/format";
import type { PriceType, Side } from "@/shared/types";
import s from "../domain.module.css";

const MODE_LABEL: Record<SignalMode, string> = {
  live: "live 实盘（真实下单）",
  paper: "paper 模拟盘",
  dry_run: "dry_run 空跑（只走风控与审计，不触达柜台）",
};

/**
 * 外部信号与信号模式。
 *
 * ★ 契约要点（signal.py + gateway/signal_router.py）：
 *   - 模式只有三种：live / paper / dry_run（**没有 paused**），非法值后端返回 400
 *   - /signal/submit 的 body 用 side（不是 direction）；成功后返回订单结果
 *   - 大额单会返回 pending_confirmation + confirm_token，需调 /signal/confirm 二次确认
 *   - 提交失败统一返回 503 并携带 reason（含风控拦截原因），本页原样展示不粉饰
 *   - 非 live 模式不会触达柜台，这是验证策略的安全档位
 */
export function Signals() {
  const mode = useAsync(() => signalApi.getMode(), []);

  const [code, setCode] = useState("000001.SZ");
  const [side, setSide] = useState<Side>("buy");
  const [volume, setVolume] = useState("100");
  const [price, setPrice] = useState("");
  const [priceType, setPriceType] = useState<PriceType>("limit");
  const [source, setSource] = useState("manual");

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);
  const [pending, setPending] = useState<OrderResult | null>(null);
  const [totp, setTotp] = useState("");

  const normCode = useMemo(() => normalizeCode(code), [code]);
  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  const setMode = async (m: SignalMode) => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await signalApi.setMode(m);
      setBanner({ tone: "ok", text: `信号模式已切换为 ${res.mode}` });
      await mode.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await signalApi.submit({
        source,
        code: normCode,
        side,
        volume: Number(volume) || 0,
        price: priceType === "market" ? 0 : Number(price) || 0,
        price_type: priceType,
      });

      if (res.pending_confirmation && res.confirm_token) {
        setPending(res);
        setBanner({
          tone: "warn",
          text: `信号已挂起待二次确认${res.amount ? `（金额 ${res.amount.toFixed(2)}）` : ""}`,
        });
        return;
      }

      if (!res.ok) {
        setBanner({ tone: "error", text: `信号被拒：${res.reason ?? "未知原因"}` });
        return;
      }
      setBanner({ tone: "ok", text: `信号已路由，委托号 ${res.order_id ?? "—"}` });
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!pending?.confirm_token) return;
    setBusy(true);
    try {
      const res = await signalApi.confirm(pending.confirm_token, totp);
      if (res.ok) {
        setBanner({ tone: "ok", text: `确认成功，委托号 ${res.order_id ?? "—"}` });
        setPending(null);
        setTotp("");
      } else {
        setBanner({ tone: "error", text: `确认失败：${res.reason ?? "未知原因"}` });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const curMode = mode.data?.mode;

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <span className={s.muted}>当前模式</span>
        {mode.loading && !mode.data ? (
          <Spinner />
        ) : (
          <Badge tone={curMode === "live" ? "danger" : curMode === "paper" ? "warning" : "info"}>
            {curMode ?? "未知"}
          </Badge>
        )}
        <span className={s.spacer} />
        {(["dry_run", "paper", "live"] as SignalMode[]).map((m) => (
          <Button
            key={m}
            size="sm"
            variant={curMode === m ? "primary" : "ghost"}
            disabled={busy || curMode === m}
            title={MODE_LABEL[m]}
            onClick={() => void setMode(m)}
          >
            {m}
          </Button>
        ))}
      </div>

      {curMode === "live" && (
        <div className={`${s.note} ${s.noteWarn}`}>
          当前为 <b>live 实盘</b>模式：信号会经统一链路真实触达券商柜台。切换到 dry_run 可先验证规则与风控。
        </div>
      )}

      {banner && (
        <div
          className={`${s.note} ${
            banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
          }`}
        >
          {banner.text}
        </div>
      )}

      <div className={s.split}>
        <Panel title="提交信号">
          <div className={s.form}>
            <FormRow label="来源">
              <Input value={source} onChange={(e) => setSource(e.target.value)} mono placeholder="manual / webhook / ..." />
            </FormRow>
            <FormRow label="代码">
              <Input value={code} onChange={(e) => setCode(e.target.value)} mono />
            </FormRow>
            {quote && (
              <div className={s.note}>
                {quote.name || normCode} · 最新 <span className={s.mono}>{fmtPrice(quote.price)}</span>
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
            <FormRow label="数量">
              <Input value={volume} onChange={(e) => setVolume(e.target.value)} mono />
            </FormRow>
            <FormRow label="委托类型">
              <Select
                value={priceType}
                onChange={(e) => setPriceType(e.target.value as PriceType)}
                options={[
                  { value: "limit", label: "限价" },
                  { value: "market", label: "市价" },
                ]}
              />
            </FormRow>
            {priceType === "limit" && (
              <FormRow label="价格">
                <Input value={price} onChange={(e) => setPrice(e.target.value)} mono />
              </FormRow>
            )}
            <Button block disabled={busy} onClick={() => void submit()}>
              {busy ? "提交中…" : "提交信号"}
            </Button>
          </div>
        </Panel>

        <Panel title="链路说明">
          <div className={s.kv}>
            <span className={s.kvKey}>统一入口</span>
            <span className={s.kvVal}>SignalRouter（风控 + 幂等 + WAL + 审计）</span>
            <span className={s.kvKey}>执行模式</span>
            <span className={s.kvVal}>live / paper / dry_run</span>
            <span className={s.kvKey}>幂等</span>
            <span className={s.kvVal}>同 idempotency_key 在窗口内只执行一次</span>
            <span className={s.kvKey}>大额确认</span>
            <span className={s.kvVal}>返回 pending_confirmation + confirm_token</span>
            <span className={s.kvKey}>Webhook 入站</span>
            <span className={s.kvVal}>POST /signal/webhook（HMAC 签名校验）</span>
          </div>
          <div className={`${s.note} ${s.noteInfo}`} style={{ marginTop: 8 }}>
            dry_run 模式仍会完整走风控与审计，只是不触达柜台 —— 这是上线新策略前推荐的验证档位。
            出站 Webhook 的配置在「出站 Webhook」页。
          </div>
        </Panel>
      </div>

      <ConfirmModal
        open={pending !== null}
        danger
        title="大额信号二次确认"
        warn="确认后将按当前信号模式执行"
        confirmText="确认执行"
        loading={busy}
        onCancel={() => {
          setPending(null);
          setTotp("");
        }}
        onConfirm={() => void confirm()}
        message={
          <div className={s.kv}>
            <span className={s.kvKey}>标的</span>
            <span className={s.kvVal}>{normCode}</span>
            <span className={s.kvKey}>方向</span>
            <span className={s.kvVal}>{side === "buy" ? "买入" : "卖出"}</span>
            <span className={s.kvKey}>数量</span>
            <span className={s.kvVal}>{volume}</span>
            {pending?.amount !== undefined && (
              <>
                <span className={s.kvKey}>金额</span>
                <span className={s.kvVal}>{pending.amount.toFixed(2)}</span>
              </>
            )}
            {pending?.requires_totp && (
              <>
                <span className={s.kvKey}>动态码</span>
                <span className={s.kvVal}>
                  <Input value={totp} onChange={(e) => setTotp(e.target.value)} mono placeholder="6 位 TOTP" />
                </span>
              </>
            )}
          </div>
        }
      />
    </div>
  );
}

export default Signals;
