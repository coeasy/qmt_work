import { useEffect, useMemo, useRef, useState } from "react";
import { EmptyState, Spinner } from "@/design/primitives";
import { marketApi } from "@/services/api";
import { useQuotesStore } from "@/stores/quotes";
import { useWorkspaceStore } from "@/stores/workspace";
import type { Quote } from "@/shared/types";
import { fmtAmount, fmtPrice, fmtVolume } from "@/shared/format";
import s from "./panels.module.css";

interface BookRow {
  label: string;
  price?: number;
  vol?: number;
  side: "ask" | "bid" | "mid";
}

/** 五档缺失时的 REST 轮询间隔（ms）。盘中 5s 足够跟上盘口变化，又不至于打爆行情源。 */
const POLL_MS = 5_000;

function hasDepth(q: Quote | undefined): boolean {
  return (q?.bids?.length ?? 0) > 0 || (q?.asks?.length ?? 0) > 0;
}

/**
 * 五档盘口面板（含量能条与关键数据）。
 *
 * 从 `OrderBook.tsx` 抽出，同时把 `MarketData.tsx` 里那份「卖5→买1 / 买1→买5」
 * 的简版盘口**统一到这里** —— 原先两个页面各画一套盘口，档位顺序与缺失值处理
 * 已经出现细微差异（一处显示 `--`、一处显示 `—`），合并展示要消灭的就是这种漂移。
 *
 * ## 契约要点
 * 五档来自 `/market/quote` 的 **`bids` / `asks`**（元素为 `{price, volume}` 的
 * 定长 5 数组，后端已归一）。
 *
 * ⚠️ 不要改回读 `bid` / `ask` / `bid_vol` / `ask_vol`：那四个是**买一/卖一标量**
 * （`xtquant_client/xtp/quotes.py` 的 `_lst(..., 0)`），不是数组。此前按数组索引
 * 读（`quote?.bid ?? []` 然后 `asks[i]`）得到的是 undefined —— 五档整列显示「—」，
 * 既不报错也不兜底，用户只会以为「这只票没盘口」。
 *
 * ## ★ 双源取值（2026-10-03 修断链）
 * 本面板原先**只**读 WS 推送的快照。而 WS 行情推送的前提是**有活跃券商桥**
 * （`sync/__init__.py::_subscribe_to_qmt` 无桥直接 return），于是：
 *   - 未连券商 / 券商被授权串封死 ⇒ 通道一条都不推 ⇒ 五档**恒空**；
 *   - 同一个界面里，头部用 `useDisplayQuotes`（含 REST 兜底）**有价**，
 *     下方五档却写着「行情通道尚未连接」—— 自相矛盾。
 * 而五档本身**不需要券商**：本地 TDX 行情源（eltdx）在 `/market/quote` 就返回
 * `bids` / `asks`（盘中 5 档，休市退化为买一卖一）。
 *
 * 因此取值规则改为：
 *   ① **WS 优先**（开市时唯一可信来源，零额外请求）；
 *   ② WS 没有深度（未推送 / 该源不给数组）⇒ **REST 兜底** `GET /market/quote`；
 *   ③ 两边都没有 ⇒ 按真实成因分叉空态，**绝不**用 0 填档位。
 *
 * 合并时保留 WS 的价/量字段（更实时），只从 REST 借 `bids` / `asks`。
 *
 * 零 mock：无数据显示成因，绝不用假档位凑满 5 行。
 */
export function OrderBookPanel({ code }: { code: string }) {
  const live = useQuotesStore((st) => st.quotes[code]);
  const socketState = useQuotesStore((st) => st.socketState);
  const open = useWorkspaceStore((st) => st.open);

  const [rest, setRest] = useState<Quote | undefined>(undefined);
  const [err, setErr] = useState<string>("");
  const [loaded, setLoaded] = useState(false);

  /**
   * ★ 只在「WS 没给深度」时才真的打接口。
   *
   * 常驻 5s 轮询的前提是「无法预先知道 WS 会不会推深度」，但**已经推了**之后就
   * 不必再打：WS 深度是实时的，REST 那份无论如何都更旧。开市连着券商时这条能省掉
   * 每 5 秒一次的 `/market/quote`，也避免 `setRest` 每 5 秒触发一次无谓重渲染。
   *
   * ⚠️ 用 effect 同步 ref（**不在渲染期写 ref**）：并发渲染下被丢弃的渲染也会执行
   *    渲染期赋值，ref 会留下一份永远不会被提交的值。
   */
  const liveRef = useRef(live);
  useEffect(() => {
    liveRef.current = live;
  }, [live]);

  /**
   * REST 兜底：换标的立即清空；已拿到过数据后失败不清空（`got`）——
   * 避免「有 → 空」的闪烁 —— 行情源偶发超时不该把盘口整个抹掉。
   */
  useEffect(() => {
    setRest(undefined);
    setErr("");
    setLoaded(false);
    if (!code) return;
    let cancelled = false;
    let got = false;

    const tick = () => {
      if (cancelled) return;
      // WS 已经在推五档了 ⇒ 不必再打（见上方 liveRef 的说明）
      if (got && hasDepth(liveRef.current)) return;
      marketApi
        .quote(code)
        .then((q) => {
          if (cancelled || !q || typeof q !== "object") return;
          setRest(q);
          setErr("");
          setLoaded(true);
          got = true;
        })
        .catch((e: unknown) => {
          if (cancelled || got) return;
          setErr(e instanceof Error ? e.message : String(e));
          setLoaded(true);
        });
    };

    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [code]);

  /** WS 优先 + REST 借深度（见类注释「双源取值」） */
  const quote = useMemo<Quote | undefined>(() => {
    if (live && rest) {
      return {
        ...rest,
        ...live,
        bids: live.bids?.length ? live.bids : rest.bids,
        asks: live.asks?.length ? live.asks : rest.asks,
      };
    }
    return live ?? rest;
  }, [live, rest]);

  const fromWs = hasDepth(live);
  const depthSource = fromWs ? "实时推送" : rest ? "行情接口" : "";

  /**
   * 五档数组优先；两边都没给数组时用买一卖一标量补首档，
   * 保证「至少买一卖一可见」，其余档位留空而非用 0 填充。
   *
   * ★ 为什么要 `useMemo`：下面的 `rows` 依赖 `bids` / `asks` / `bidVols` / `askVols`。
   *   若这四样是每次渲染新建的普通变量（`.map()` 产物引用必变），`rows` 的 useMemo
   *   **永不命中** —— 看着写了记忆化，实际每次渲染都重算 11 行盘口（典型「假记忆化」）。
   *   这里把整段派生收成一个以 `quote` 为唯一依赖的 memo，下游引用才稳定。
   */
  const levels = useMemo(() => {
    const bs = quote?.bids ?? (quote?.bid !== undefined
      ? [{ price: quote.bid, volume: quote.bid_vol ?? 0 }] : []);
    const as = quote?.asks ?? (quote?.ask !== undefined
      ? [{ price: quote.ask, volume: quote.ask_vol ?? 0 }] : []);
    return { bids: bs, asks: as, bidVols: bs.map((l) => l.volume), askVols: as.map((l) => l.volume) };
  }, [quote]);
  const { bids, asks, bidVols, askVols } = levels;

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
    // ★ 依赖只放 `levels` 与 price：`levels` 已是以 quote 为唯一依赖的 memo，
    //   引用稳定 ⇒ 本 useMemo 才真的会命中。
  }, [levels, quote?.price]);

  const maxVol = Math.max(
    1,
    ...bidVols.filter((v) => typeof v === "number"),
    ...askVols.filter((v) => typeof v === "number"),
  );

  // ★ 空态必须按**已核实的成因**分叉（与 `MarketTicksPanel` / `L2Panel` 同口径）。
  //
  //   WS 与 REST 都拿不到 ⇒ 是「行情源本身不可用」，出路是连券商（券商快照也带五档）
  //   或检查本地行情源是否就绪；**不得**再说「通道未连接」——REST 兜底已经覆盖了
  //   「没连券商」这一半成因，那是本轮修复要消灭的假出口。
  if (!quote) {
    if (!loaded) return <Spinner label="加载盘口…" />;
    return (
      <EmptyState
        text={err
          ? `行情不可用：${err}`
          : `${code} 暂无行情快照 —— 本地行情源未覆盖该标的，或行情服务未就绪`}
        actionText="去连接"
        onAction={() => open("brokers", {}, { title: "连接管理" })}
      />
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
      <div className={s.miniBar}>
        <span style={{ color: "var(--text-faint)" }}>
          五档盘口{depthSource ? ` · ${depthSource}` : ""}
        </span>
        <span className={s.mono} style={{ color: "var(--text-faint)" }}>
          {quote.source ?? ""}
        </span>
        <span style={{ flex: 1 }} />
        {/* 非实时推送时说明「数据来自接口轮询、可能滞后」，别让用户当成逐笔实时盘口。
            ⚠️ 不要给 `--warning` 写臆造兜底色（此前写 `#c8901e`，与真实令牌
            `#f59e0b`/`#d97706` 不符）—— 令牌一定存在，写错兜底只会误导读者。 */}
        {!fromWs && socketState !== "open" && (
          <span style={{ color: "var(--warning)" }}>非实时推送</span>
        )}
      </div>

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

      {/* 源确实没给五档（缺数组，或空数组——easy_tdx 公共源无档位数据）⇒ 明说，
          而不是让 11 行空白去暗示「这只票没盘口」。 */}
      {(quote.bids === undefined && quote.asks === undefined) && (
        <div className={s.panelNote} style={{ marginTop: 6 }}>
          当前行情源未提供五档数组，以上仅由买一 / 卖一标量补齐 —— 连接券商后可取得完整五档。
        </div>
      )}
      {quote.bids !== undefined && quote.asks !== undefined &&
        (quote.bids?.length ?? 0) === 0 && (quote.asks?.length ?? 0) === 0 && (
        <div className={s.panelNote} style={{ marginTop: 6 }}>
          当前行情源（{quote.source ?? "TDX 公共源"}）不提供五档档位 —— 快照/成交额仍实时；
          连接券商后可取得完整五档盘口。
        </div>
      )}

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
