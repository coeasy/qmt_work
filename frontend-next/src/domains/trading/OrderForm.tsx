import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, ConfirmModal, FormRow, Input, Select } from "@/design/primitives";
import { accountApi, signalApi, tradeApi, type OrderResult } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { useAsync } from "@/hooks/useAsync";
import { fmtMoney, fmtPct, fmtPrice, normalizeCode, toneColor } from "@/shared/format";
import { isLivePrice } from "@/shared/freshness";
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
  /**
   * 快捷下单模式：工作台右栏底部「闪电下单」。
   *
   * ★ 只换**布局**，不换逻辑：预检 / 结果分流（503 / 被拒 / 成功）/ 大额二次确认
   *   全部走同一份实现 —— 这三件事一旦分叉，最容易出现「没下成功却显示已报」。
   *   快捷模式额外提供的是**取价与配仓快捷键**（买一 / 卖一 / 全仓 / 1/2…），
   *   它们只写表单字段，不碰提交链路。
   */
  quick?: boolean;
  /** 下单成功后回调（父级刷新持仓/委托列表） */
  onDone?: () => void;
}

type ResultTone = "success" | "danger" | "warning" | "info";

/**
 * 「这一单到底会不会真的报给券商」—— 下单前必须说清的**唯一结论**。
 *
 * ★ 2026-09-20 修复。此前这段逻辑只在**未连接券商**时才提示，且提示词硬写
 *   「下单将返回 503」。两个错误：
 *
 * ① **已连券商 + 默认 paper 模式**这个最常见的组合下，一个字都不提示。
 *    后端信号模式的默认值是 ``paper``（``SignalRouter._load_persisted_mode("paper")``），
 *    而新装的用户往往先连上券商就直接下单 —— 于是：本地撮合成交、界面上「成交」，
 *    但**一根委托都没有报给券商**。更糟的是 Trade 页的持仓/委托/成交读的是**券商**
 *    接口，列表不会变，用户从界面上完全看不出这一单是模拟的。
 *    对交易软件来说，这是最不能含糊的一句话。
 *
 * ② 「未连接券商 ⇒ 下不了单」是**错误归因**：后端已按模式分流（`routes/trade.py`），
 *    paper 由 PaperEngine 本地撮合、dry_run 只返回计划，**都不需要券商**。
 *    硬写 503 会把用户骗去连券商，而其实当场就能成交。
 *
 * 现在：只要 ``mode !== "live"`` 就提示，**与是否连券商无关**；模式取不到时不断言后果。
 */
export function orderExecutionNote(
  mode: string | undefined,
  connected: boolean,
): string | null {
  switch (mode) {
    case "paper":
      return connected
        ? "当前信号模式为「模拟盘」：下单由本地模拟撮合，「不会向券商报单」—— "
          + "即使券商已连接，这笔委托也不会进入柜台。成交只体现在「账户 · 模拟盘」。"
        : "当前无已连接券商；信号模式为「模拟盘」，下单由本地模拟撮合，不依赖券商。";
    case "dry_run":
      return "当前信号模式为「预演」：下单只返回计划，既不成交也不报单。";
    case "live":
      return connected
        ? null
        : "未连接券商，下单将返回 503 —— 请先到「连接管理」连接。";
    default:
      return connected
        ? null
        : "当前无已连接券商。下单后果取决于信号模式（实盘需连接券商；模拟盘 / 预演不依赖券商），"
          + "可到「信号」页确认当前模式。";
  }
}

export function OrderForm({
  code: codeProp,
  onCodeChange,
  compact = false,
  quick = false,
  onDone,
}: OrderFormProps) {
  const connections = useBrokerStore((st) => st.connections);
  const activeConn = connections.find((c) => c.active)?.conn_id ?? "";
  const connected = connections.some((c) => c.connected);

  /**
   * 账户资金与持仓（**仅快捷模式需要**）：「全仓 / 1/2 / 1/3」必须知道可用资金
   * 与该标的持仓量，否则快捷键只能靠猜。连不上券商时 grid 取不到 ⇒ 快捷键如实
   * 提示「未取到可用资金」，不按 0 算（0 会被读成「一分钱都买不了」）。
   */
  const gridRes = useAsync(() => (quick ? accountApi.grid() : Promise.resolve(null)), [quick]);
  const grid = gridRes.data;

  /**
   * 信号模式：它决定「这一单会不会真的报给券商」。
   *
   * 结论由 :func:`orderExecutionNote` 统一给出（含 ★ 2026-09-20 修复的两处错误归因：
   * 只在「未连接券商」时提示、以及把「没连券商」一律说成 503）。
   * 这里只负责取模式 —— 取不到时该函数返回 null（不断言后果，宁可不说也不说错）。
   */
  const sigModeRes = useAsync(() => signalApi.getMode(), []);
  const sigMode = sigModeRes.data?.mode;
  const execNote = orderExecutionNote(sigMode, connected);

  const [innerCode, setInnerCode] = useState("000001.SZ");
  const code = codeProp ?? innerCode;
  const normCode = useMemo(() => normalizeCode(code), [code]);

  const [side, setSide] = useState<Side>("buy");
  const [priceType, setPriceType] = useState<PriceType>("limit");
  const [volume, setVolume] = useState("100");
  const [price, setPrice] = useState("");
  /**
   * 用户是否**手动动过**价格。
   *
   * ★ 2026-09-20 实测缺陷：限价单价格默认**空**（`price=""`），而「最新价」只是个
   * 手动按钮 —— 用户不点它就直接下单，`Number("") || 0` ⇒ **以 price=0 提交限价单**
   * （柜台要么拒单，要么当市价单成交，用户毫不知情）。
   * 现在行情到手即自动回填最新价；一旦用户自己改过（或点了买一/卖一/最新价），
   * 就不再覆盖他的输入 —— 自动回填绝不能盖掉人已经填好的价格。
   */
  const [priceTouched, setPriceTouched] = useState(false);

  const [banner, setBanner] = useState<{ tone: ResultTone; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<OrderResult | null>(null);
  const [totp, setTotp] = useState("");

  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  // 换标的时清掉上一次的结果提示，避免「上一只票的成功文案」挂在新标的上
  useEffect(() => {
    setBanner(null);
    setPriceTouched(false);   // 换了标的 ⇒ 允许重新自动回填最新价
  }, [normCode]);

  /**
   * 限价单：行情到手后自动回填最新价（用户没动过价格时才回填）。
   *
   * 取不到行情就**留空**（placeholder 0.00），绝不回填 0 —— 见下方 `priceMissing`，
   * 那种情况下提交按钮是禁用的，并给出明确提示。
   */
  useEffect(() => {
    if (priceType !== "limit" || priceTouched) return;
    // 「价格可用」的判据唯一实现在 shared/freshness（0 与缺失都算不可用），
    // 不要在页面里另写一遍 `typeof p === "number" && p > 0` —— 门禁会挡，
    // 而且停牌推 0 时会被当成真价格填进下单框。
    if (isLivePrice(quote?.price)) setPrice(String(quote.price));
  }, [priceType, priceTouched, quote?.price]);

  /** 限价单却填不出正数价格 ⇒ 不许提交（避免以 price=0 报出去）。 */
  const priceMissing = priceType === "limit" && !(Number(price) > 0);

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
    if (quote?.price) {
      setPrice(String(quote.price));
      setPriceTouched(true);
    }
  }, [quote?.price]);

  /** 快捷取价：买一 / 卖一。取不到就明说，绝不回填 0 */
  const fillSidePrice = useCallback(
    (which: "bid" | "ask") => {
      const p = which === "bid" ? (quote?.bid ?? quote?.bids?.[0]?.price) : (quote?.ask ?? quote?.asks?.[0]?.price);
      if (typeof p === "number" && p > 0) {
        setPriceType("limit");
        setPrice(String(p));
        setPriceTouched(true);
      } else {
        setBanner({ tone: "warning", text: `未取到${which === "bid" ? "买一" : "卖一"}价（行情未订阅到）` });
      }
    },
    [quote?.bid, quote?.ask, quote?.bids, quote?.asks],
  );

  /**
   * 快捷配仓：ratio = 1 / 0.5 / 1/3 / 0.25。
   *
   * ★ 买入按**可用资金**算并留 2% 余量（佣金 + 过户费会让「刚好用完」被柜台拒单），
   *   且必须取整到 100 股（A 股买入最小单位一手）。
   * ★ 卖出按**该标的实际持仓**算；零股可以一次卖完，所以不足一手时按实际股数。
   */
  const fillQty = useCallback(
    (ratio: number) => {
      const p = priceType === "market" ? quote?.price : Number(price) || 0;
      if (side === "buy") {
        if (!(typeof p === "number" && p > 0)) {
          setBanner({ tone: "warning", text: "请先填入价格（或点「最新」「买一」取价）" });
          return;
        }
        const cash = grid?.total_cash ?? 0;
        if (cash <= 0) {
          setBanner({ tone: "warning", text: "未取到可用资金，请确认券商已连接" });
          return;
        }
        const v = Math.floor((cash * 0.98 * ratio) / p / 100) * 100;
        setVolume(String(v));
        if (v <= 0) {
          setBanner({ tone: "warning", text: "可用资金不足一手（100 股）" });
        }
      } else {
        const held = grid?.positions.find((x) => x.code === normCode)?.total_volume ?? 0;
        if (held <= 0) {
          setBanner({ tone: "warning", text: `当前账户无 ${normCode} 持仓，无法按仓位卖出` });
          return;
        }
        const v = Math.floor((held * ratio) / 100) * 100;
        setVolume(String(v > 0 ? v : held));
      }
    },
    [grid, normCode, price, priceType, quote?.price, side],
  );

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

  /**
   * 提交委托。经 SignalRouter 统一链路，可能返回「挂起待确认」。
   *
   * ★ `dir` 是快捷下单用的：面板上「买入」「卖出」是两个按钮，直接带方向进来，
   *   不必先 setState 再等下一次渲染 —— 否则连点两次会出现「按了卖出、按卖出的
   *   状态还没生效就已经提交成买入」。
   */
  const onSubmit = async (dir?: Side) => {
    const d: Side = dir ?? side;
    if (dir) setSide(dir);
    setBusy(true);
    setBanner(null);
    try {
      const res = await tradeApi.order({
        conn_id: activeConn,
        code: normCode,
        direction: d,
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

  /** 该标的当前持仓（快捷模式「卖出全仓」要用） */
  const held = grid?.positions.find((x) => x.code === normCode)?.total_volume ?? 0;

  /**
   * 大额二次确认弹层 —— 两种布局**共用同一个实例**：
   * 若各写一份，TOTP / confirm_token 的处理一旦分叉就是「确认了却没下单」。
   */
  const confirmModal = (
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
  );

  // ---------------------------------------------------------------- 快捷下单
  if (quick) {
    return (
      <>
        <div className={s.quick}>
          <div className={s.quickHead}>
            <span className={s.quickName}>{quote?.name || normCode}</span>
            <span className={s.quickCode}>{normCode}</span>
            <span className={s.mono} style={{ color: toneColor(quote?.change_pct) }}>
              {fmtPrice(quote?.price)} {fmtPct(quote?.change_pct)}
            </span>
          </div>

          <div className={s.quickRow}>
            <span className={s.quickLabel}>价格</span>
            <Input
              value={price}
              onChange={(e) => { setPriceTouched(true); setPrice(e.target.value); }}
              mono
              disabled={priceType === "market"}
              placeholder={priceType === "market" ? "市价" : "0.00"}
              style={{ width: 74 }}
            />
            <div className={s.quickChips}>
              <button
                type="button"
                className={s.quickChip}
                onClick={() => fillSidePrice("bid")}
                title="按买一价挂限价单"
              >
                买一
              </button>
              <button
                type="button"
                className={s.quickChip}
                onClick={() => fillSidePrice("ask")}
                title="按卖一价挂限价单"
              >
                卖一
              </button>
              <button
                type="button"
                className={s.quickChip}
                onClick={fillMarketPrice}
                disabled={!quote?.price}
              >
                最新
              </button>
              <button
                type="button"
                className={
                  priceType === "market" ? `${s.quickChip} ${s.quickChipOn}` : s.quickChip
                }
                onClick={() => setPriceType(priceType === "market" ? "limit" : "market")}
                title="市价单不填价格"
              >
                市价
              </button>
            </div>
          </div>

          <div className={s.quickRow}>
            <span className={s.quickLabel}>数量</span>
            <Input
              value={volume}
              onChange={(e) => setVolume(e.target.value)}
              mono
              placeholder="股数"
              style={{ width: 74 }}
            />
            <div className={s.quickChips}>
              <button type="button" className={s.quickChip} onClick={() => fillQty(1)}>
                全仓
              </button>
              <button type="button" className={s.quickChip} onClick={() => fillQty(0.5)}>
                1/2
              </button>
              <button type="button" className={s.quickChip} onClick={() => fillQty(1 / 3)}>
                1/3
              </button>
              <button type="button" className={s.quickChip} onClick={() => fillQty(0.25)}>
                1/4
              </button>
            </div>
          </div>

          <div className={s.quickMeta}>
            <span>
              可用 <b className={s.mono}>{grid ? fmtMoney(grid.total_cash) : "—"}</b>
            </span>
            <span>
              持仓 <b className={s.mono}>{held > 0 ? String(held) : "—"}</b>
            </span>
            <span>
              预估 <b className={s.mono}>{estText}</b>
            </span>
          </div>

          <div className={s.quickActions}>
            <Button variant="buy" block onClick={() => void onSubmit("buy")} disabled={busy || priceMissing}>
              {busy ? "提交中…" : "买入"}
            </Button>
            <Button variant="sell" block onClick={() => void onSubmit("sell")} disabled={busy || priceMissing}>
              {busy ? "提交中…" : "卖出"}
            </Button>
          </div>

          {priceMissing && (
            <div className={s.warnBar} role="status">
              限价单需要价格：行情未取到时请手动填写，或点「买一 / 卖一 / 最新价」取价 ——
              价格为 0 的限价单会被柜台拒单。
            </div>
          )}
          {execNote && (
            <div className={s.warnBar}>{execNote}</div>
          )}
          {banner && (
            <div className={s.banner} data-tone={banner.tone} role="status" aria-live="polite">
              {banner.text}
            </div>
          )}
        </div>
        {confirmModal}
      </>
    );
  }

  // ---------------------------------------------------------------- 标准表单
  return (
    <>
      <div className={compact ? s.formCompact : s.form}>
        {execNote && (
          <div className={s.warnBar}>{execNote}</div>
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
            <span>{quote.name || normCode}</span>
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
              onChange={(e) => { setPriceTouched(true); setPrice(e.target.value); }}
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
            onClick={() => void onSubmit()}
            disabled={busy || priceMissing}
            block
            title={priceMissing ? "限价单需要价格（避免以 price=0 报单）" : undefined}
          >
            {busy ? "提交中…" : side === "buy" ? "买入下单" : "卖出下单"}
          </Button>
        </div>

        {priceMissing && (
          <div className={s.warnBar} role="status">
            限价单需要价格：行情未取到时请手动填写，或点「买一 / 卖一 / 最新价」取价 ——
            价格为 0 的限价单会被柜台拒单。
          </div>
        )}

        {banner && (
          <div className={s.banner} data-tone={banner.tone} role="status" aria-live="polite">
            {banner.text}
          </div>
        )}
      </div>

      {confirmModal}
    </>
  );
}

export default OrderForm;
