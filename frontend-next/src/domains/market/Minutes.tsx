import { useMemo, useState } from "react";
import { Button, Input } from "@/design/primitives";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, namePair, normalizeCode, toneColor } from "@/shared/format";
import type { PageProps } from "@/app/routes";
import { MinutesChart } from "./panels/MinutesChart";
import s from "../domain.module.css";
import p from "./panels/panels.module.css";

/**
 * 分时图（独立页）。
 *
 * 图表本体在 `panels/MinutesChart`（行情工作台共用同一份实现）——
 * 本页只负责「输入代码 / 日期 + 刷新」这层外壳。
 *
 * ★ 契约要点（market.py:market_minutes → hub.get_minutes）：
 *   - 返回 {code, trading_date, pre_close, open_price, points:[{t,price,avg,volume}]}
 *   - 数据源只有 TDX 公共行情（券商 SDK 无分时接口）；全源无数据返回 503
 *   - 分时不是 K 线周期：走 /market/minutes，**不能**用 /market/kline 的 tick 周期
 *     （后端会显式 400 并指向本端点，避免静默返回 0 根导致图表空白无报错）
 *
 * 支持 `params.code` 预填（工作台「独立分时」按钮带入当前标的）。
 */
export function Minutes({ params }: PageProps) {
  const [code, setCode] = useState((params.code as string) || "000001.SZ");
  const [date, setDate] = useState("");
  /** 刷新令牌：面板自己不放刷新按钮，避免同一动作两处各有一个 */
  const [token, setToken] = useState(0);

  const normCode = useMemo(() => normalizeCode(code), [code]);
  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);
  const [title, sub] = namePair(quote?.name, normCode);

  return (
    <div className={`${s.page} ${s.pageFlush}`}>
      <div className={s.toolbar} style={{ padding: "6px 8px 0" }}>
        <Input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          mono
          style={{ width: 150 }}
          placeholder="代码"
        />
        <Input
          value={date}
          onChange={(e) => setDate(e.target.value)}
          mono
          style={{ width: 130 }}
          placeholder="YYYY-MM-DD（空=最新）"
        />
        <Button size="sm" variant="ghost" onClick={() => setToken((t) => t + 1)}>
          刷新
        </Button>
        <span className={s.spacer} />
        <span>{title}</span>
        {sub && <span className={s.muted}>{sub}</span>}
        {quote && (
          <span className={s.mono} style={{ color: toneColor(quote.change_pct) }}>
            {fmtPrice(quote.price)} {fmtPct(quote.change_pct)}
          </span>
        )}
      </div>

      {/* .chart 只给高度，面板内部靠 flex 填充 ⇒ 容器也要是 flex column（bodyCol） */}
      <div className={`${s.chart} ${p.bodyCol}`} style={{ margin: 8 }}>
        <MinutesChart code={normCode} date={date} reloadToken={token} />
      </div>
    </div>
  );
}

export default Minutes;
