import { useEffect, useMemo, useRef, useState } from "react";
import { EmptyState, Spinner } from "@/design/primitives";
import { marketApi, type MarketTick } from "@/services/api";
import { fmtAmount, fmtPrice, fmtVolume } from "@/shared/format";
import d from "../../domain.module.css";
import s from "./panels.module.css";

/**
 * 成交流面板 —— **真实市场逐笔成交**（`/market/ticks`，本地 TDX，无需券商）。
 *
 * ## 为什么整块换掉（2026-10-03）
 * 本面板原来是 `DealFeedPanel`，消费 WS 的 `deal` 事件。那条链路的真实来源是
 * `sync/__init__.py::_push_order_deal_events`（`adapter.get_deals`）与
 * `_on_realtime_deal`（`on_trade`）—— 即**本账户的成交回报**，不是市场成交流：
 *   - 默认标的（上证指数）永远不会有行；看别人的票同样永远为空；
 *   - 即使连了券商，显示的也只是「我自己的成交」，对判断盘口活跃度**没有意义**，
 *     且标题写着「实时成交流」会被直接误读成市场成交 —— 是**语义错误**，不只是没数据。
 * 同时旁边那条「逐笔（券商 L2）」要走券商网关，未连接恒 503。
 * 于是「行情工作台的成交流」在未连券商时**结构性空白**，而真实市场逐笔本来就能
 * 从公开行情源取到。零 mock 铁律下，正确做法是接上真源，而不是拿账户回报充数。
 *
 * ## 契约（改一侧必须同步另一侧）
 * - 后端 `GET /market/ticks` → `{code, items:[{time,price,volume,amount,side,order_count}],
 *   count, trading_date, source}`
 * - `volume` 单位**手**；`amount` 单位**元**（后端已算好，此处**不再乘 100**）
 * - `side` ∈ `buy` / `sell` / `neutral`（主动买 / 主动卖 / 中性）
 * - 后端已按**最新在前**排序，面板不再二次排序
 *
 * ## 空态必须按成因分叉（与 `OrderBookPanel` / `L2Panel` 同口径）
 * - 请求**失败**（503：本地 TDX 未就绪）⇒ 说明原因 + 出路；
 * - 请求**成功但 `items` 为空** ⇒ 源是好的，只是当前没有成交（盘前 / 该标的无成交），
 *   **不得**再说「源不可用」—— 那是把两个不同成因糊在一起的老毛病。
 *
 * 零 mock：字段缺失显示 `--`，绝不用 0 填充。
 */

/** 大单阈值（元）：单笔成交额 ≥ 100 万 —— 与「大单」在盘口语境下的直观量级一致。 */
const BIG_DEAL_YUAN = 1_000_000;

/** 轮询间隔（ms）。市场逐笔没有 WS 推送通道，只能轮询 REST。 */
const POLL_MS = 3_000;

function isBig(t: MarketTick): boolean {
  return Number(t.amount ?? 0) >= BIG_DEAL_YUAN;
}

/**
 * 两批逐笔是否**内容相同**。
 *
 * ★ 为什么需要：轮询每 3s 回一次，盘后/停牌时返回的逐笔**逐字节相同**，
 *   但 `setItems(新数组)` 每次都是新引用 ⇒ 组件每 3 秒白渲染一次，还连带重算
 *   `bigCount` 与整张表格的 diff。内容没变就返回上一次的引用，让 React 直接跳过。
 */
function sameTicks(a: MarketTick[], b: MarketTick[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const x = a[i] ?? {};
    const y = b[i] ?? {};
    if (x.time !== y.time || x.price !== y.price || x.volume !== y.volume
        || x.amount !== y.amount || x.side !== y.side) return false;
  }
  return true;
}

/** 方向 → 颜色令牌（红涨蓝跌/红涨绿跌等由 `data-updown` 令牌统一驱动） */
function sideColor(side: string | undefined): string {
  if (side === "buy") return "var(--up)";
  if (side === "sell") return "var(--down)";
  return "var(--text-dim)";
}

function sideLabel(side: string | undefined): string {
  if (side === "buy") return "买";
  if (side === "sell") return "卖";
  if (side === "neutral") return "中";
  return "—";
}

/** `YYYYMMDD` / `YYYY-MM-DD` → `YYYY-MM-DD`；识别不出就原样返回（不臆造） */
function fmtTradingDate(v: string | null | undefined): string {
  const raw = String(v ?? "").trim();
  if (!raw) return "";
  return /^\d{8}$/.test(raw)
    ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`
    : raw;
}

export function MarketTicksPanel({
  code = "",
  count = 60,
}: {
  /** 标的（必填；工作台 = 当前标的，独立页 = 用户输入） */
  code?: string;
  /** 单次拉取条数（后端上限 500） */
  count?: number;
}) {
  const [items, setItems] = useState<MarketTick[]>([]);
  const [tradingDate, setTradingDate] = useState<string>("");
  const [source, setSource] = useState<string>("");
  const [err, setErr] = useState<string>("");
  const [loaded, setLoaded] = useState(false);
  const [paused, setPaused] = useState(false);
  /** 已拿到过一次数据 → 后续轮询失败不再清空列表（避免「有 → 空」的闪烁） */
  const gotRef = useRef(false);
  const pausedRef = useRef(paused);
  // ★ 用 effect 同步 ref，**不在渲染期写**：并发渲染下被丢弃的渲染也会执行渲染期
  //   赋值，ref 会留下一份永远不会被提交的值（React 明确禁止）。
  //   用 ref 的目的仍是不把 `paused` 放进下面轮询 effect 的依赖 —— 否则每次
  //   暂停/恢复都会 clearInterval + 重建 + 立刻重拉一次。
  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  useEffect(() => {
    // 换标的即清空：否则会短暂显示上一只票的成交，比空白更危险
    setItems([]);
    setTradingDate("");
    setErr("");
    setLoaded(false);
    gotRef.current = false;
    if (!code) return;

    let cancelled = false;
    const tick = () => {
      if (pausedRef.current) return;
      marketApi
        .ticks(code, count)
        .then((res) => {
          if (cancelled) return;
          setItems((prev) => (sameTicks(prev, res?.items ?? []) ? prev : (res?.items ?? [])));
          setTradingDate(fmtTradingDate(res?.trading_date));
          setSource(String(res?.source ?? ""));
          setErr("");
          setLoaded(true);
          gotRef.current = true;
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setLoaded(true);
          // 已经有过数据就别用错误顶掉列表（下一轮可能恢复）
          if (gotRef.current) return;
          const msg = e instanceof Error ? e.message : String(e);
          setErr(msg);
        });
    };

    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [code, count]);

  const bigCount = useMemo(() => items.filter(isBig).length, [items]);

  if (!code) {
    return <EmptyState text="未指定标的 —— 请选择或输入要查看的标的代码" />;
  }

  if (!loaded) return <Spinner label="加载逐笔成交…" />;

  // ★ 失败分支：说明「为什么没有」以及「怎么补救」，不是留白
  if (err && items.length === 0) {
    return (
      <div className={`${d.note} ${d.noteWarn}`} style={{ margin: 8 }}>
        逐笔成交暂不可用：{err}
        <div style={{ marginTop: 4 }}>
          本面板走**公开行情源**（本地 TDX），与券商无关；不可用通常是
          TDX 行情源（easy_tdx）未安装或行情服务器不可达。也可连接券商后用「逐笔(L2)」查看券商侧逐笔。
        </div>
      </div>
    );
  }

  // ★ 空态（请求成功、只是没有成交）—— 不得说成「源不可用」
  if (items.length === 0) {
    return (
      <div className={s.fill}>
        <div className={s.miniBar}>
          <span className={s.panelNote}>
            行情源{tradingDate ? `（数据日 ${tradingDate}）` : ""}可用，本标的当前没有逐笔成交
            —— 盘前、集合竞价前或该标的当日无成交时为空。
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className={s.fill}>
      <div className={s.miniBar}>
        <span style={{ color: "var(--text-faint)" }}>
          逐笔成交{source ? ` · ${source}` : ""}
        </span>
        {tradingDate && (
          <span className={s.mono} style={{ color: "var(--text-dim)" }}>
            数据日 {tradingDate}
          </span>
        )}
        <span style={{ color: "var(--text-faint)" }}>
          共 {items.length} 笔{bigCount > 0 ? ` · 大单 ${bigCount}` : ""}
        </span>
        <span style={{ flex: 1 }} />
        <button type="button" className={s.periodBtn} onClick={() => setPaused((v) => !v)}>
          {paused ? "继续" : "暂停"}
        </button>
      </div>

      <div className={d.scroll} style={{ flex: "1 1 auto", minHeight: 0 }}>
        <table className={s.tickTable}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>时间</th>
              <th style={{ textAlign: "right" }}>价格</th>
              <th style={{ textAlign: "right" }}>量(手)</th>
              <th style={{ textAlign: "right" }}>金额</th>
              <th style={{ textAlign: "center" }}>方向</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t, i) => (
              <tr key={`${t.time ?? ""}-${i}`}>
                <td className={s.tickTime}>{String(t.time ?? "—")}</td>
                <td className={s.tickNum} style={{ color: sideColor(t.side) }}>
                  {fmtPrice(t.price)}
                </td>
                <td className={s.tickNum}>{fmtVolume(t.volume)}</td>
                <td className={s.tickNum}>
                  {fmtAmount(t.amount)}
                  {isBig(t) && <span className={s.bigDeal}> 大单</span>}
                </td>
                <td className={s.tickDir} style={{ color: sideColor(t.side) }}>
                  {sideLabel(t.side)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default MarketTicksPanel;
