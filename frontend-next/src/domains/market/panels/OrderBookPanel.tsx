import { useMemo } from "react";
import { EmptyState } from "@/design/primitives";
import { useQuotesStore } from "@/stores/quotes";
import { useWorkspaceStore } from "@/stores/workspace";
import { fmtAmount, fmtPrice, fmtVolume } from "@/shared/format";
import s from "./panels.module.css";

interface BookRow {
  label: string;
  price?: number;
  vol?: number;
  side: "ask" | "bid" | "mid";
}

/**
 * 五档盘口面板（含量能条与关键数据）。
 *
 * 从 `OrderBook.tsx` 抽出，同时把 `MarketData.tsx` 里那份「卖5→买1 / 买1→买5」
 * 的简版盘口**统一到这里** —— 原先两个页面各画一套盘口，档位顺序与缺失值处理
 * 已经出现细微差异（一处显示 `--`、一处显示 `—`），合并展示要消灭的就是这种漂移。
 *
 * ★ 契约要点：五档来自 /market/quote 的 **`bids` / `asks`**（元素为
 *   `{price, volume}` 的定长 5 数组，后端已归一）。
 *
 * ⚠️ 不要改回读 `bid` / `ask` / `bid_vol` / `ask_vol`：那四个是**买一/卖一标量**
 * （`xtquant_client/xtp/quotes.py` 的 `_lst(..., 0)`），不是数组。此前按数组索引
 * 读（`quote?.bid ?? []` 然后 `asks[i]`）得到的是 undefined —— 五档整列显示「—」，
 * 既不报错也不兜底，用户只会以为「这只票没盘口」。
 *   快照由 WS 订阅驱动（调用方负责 useQuoteSubscription），本面板不再订阅。
 */
export function OrderBookPanel({ code }: { code: string }) {
  const quote = useQuotesStore((st) => st.quotes[code]);
  // ★ 行情通道的**真实状态**（`stores/quotes.ts` 维护，`services/ws.ts` 的
  //   `SocketState`）—— 空态必须靠它分叉成因，而不是把两个成因用「或」糊在一起。
  const socketState = useQuotesStore((st) => st.socketState);
  const open = useWorkspaceStore((st) => st.open);

  // 五档数组优先；后端未给 bids/asks（旧版/其它源）时用买一卖一标量补首档，
  // 保证「至少买一卖一可见」，其余档位留空而非用 0 填充。
  const bids = quote?.bids ?? (quote?.bid !== undefined
    ? [{ price: quote.bid, volume: quote.bid_vol ?? 0 }] : []);
  const asks = quote?.asks ?? (quote?.ask !== undefined
    ? [{ price: quote.ask, volume: quote.ask_vol ?? 0 }] : []);
  const bidVols = bids.map((l) => l.volume);
  const askVols = asks.map((l) => l.volume);

  /** 盘口按「卖5→卖1 / 最新 / 买1→买5」纵向排列（终端惯例） */
  const rows = useMemo<BookRow[]>(() => {
    const out: BookRow[] = [];
    for (let i = 4; i >= 0; i--) {
      out.push({ label: `卖${i + 1}`, price: asks[i]?.price, vol: askVols[i], side: "ask" });
    }
    out.push({ label: "最新", price: quote?.price, vol: undefined, side: "mid" });
    for (let i = 0; i < 5; i++) {
      out.push({ label: `买${i + 1}`, price: bids[i]?.price, vol: bidVols[i], side: "bid" });
    }
    return out;
  }, [asks, askVols, bids, bidVols, quote?.price]);

  const maxVol = Math.max(
    1,
    ...bidVols.filter((v) => typeof v === "number"),
    ...askVols.filter((v) => typeof v === "number"),
  );

  // ★ 空态必须按**已核实的成因**分叉（与 `Backtest.tsx` / `Dashboard.tsx` 同口径）。
  //
  //   此前是一句「未订阅到行情（WS 未连接或该标的不在推送范围）」—— 一个括号把两个
  //   完全不同的成因糊在一起，用户没法判断该去修哪一边，也没有任何出路。
  //
  //   而 `socketState` 是**可判的**（`stores/quotes.ts` 由 `services/ws.ts` 的状态
  //   回调维护），所以两个成因必须分开说：
  //     - 通道没开（idle/connecting/closed）⇒ 五档**不可能**有，出路是去连券商；
  //     - 通道开着还没推送 ⇒ 是「这只没推」，不是「通道坏了」⇒ **不给「去连接」假出口**
  //       （通道明明是好的，把用户送去连接管理只会白跑一趟）。
  if (!quote) {
    if (socketState !== "open") {
      const label =
        socketState === "connecting" ? "正在连接"
          : socketState === "closed" ? "已断开"
            : "尚未连接";
      return (
        <EmptyState
          text={`行情通道${label} —— 五档盘口来自实时推送，通道未连接时无法显示`}
          actionText="去连接"
          onAction={() => open("brokers", {}, { title: "连接管理" })}
        />
      );
    }
    return (
      <EmptyState text="行情通道已连接，但尚未收到该标的的推送 —— 该标的不在行情源的推送范围内，或行情源未覆盖它" />
    );
  }

  const stats: Array<[string, string]> = [
    ["今开", fmtPrice(quote.open)],
    ["昨收", fmtPrice(quote.pre_close)],
    ["最高", fmtPrice(quote.high)],
    ["最低", fmtPrice(quote.low)],
    ["总量", fmtVolume(quote.volume)],
    ["成交额", fmtAmount(quote.amount)],
    ["更新时间", quote.time ?? "—"],
  ];

  return (
    <div className={s.fill}>
      <div className={s.book}>
        {rows.map((r) => (
          <div
            key={r.label}
            className={s.bookRow}
            style={{
              background:
                r.side === "mid"
                  ? "var(--bg-3)"
                  : r.side === "ask"
                    ? "var(--down-dim)"
                    : "var(--up-dim)",
            }}
          >
            {r.vol !== undefined && (
              <div className={s.bookBar} style={{ width: `${(r.vol / maxVol) * 100}%` }} />
            )}
            <span className={s.bookLabel}>{r.label}</span>
            <span
              className={s.bookPrice}
              style={{
                color:
                  r.side === "ask"
                    ? "var(--down)"
                    : r.side === "bid"
                      ? "var(--up)"
                      : "var(--text)",
                fontWeight: r.side === "mid" ? 600 : 400,
              }}
            >
              {fmtPrice(r.price)}
            </span>
            <span className={s.bookVol}>{r.vol === undefined ? "—" : fmtVolume(r.vol)}</span>
          </div>
        ))}
      </div>

      <div className={s.kvGrid} style={{ marginTop: 10 }}>
        {stats.map(([k, v]) => (
          <div key={k} className={s.kvCell}>
            <span className={s.kvCellKey}>{k}</span>
            <span className={s.kvCellVal}>{v}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default OrderBookPanel;
