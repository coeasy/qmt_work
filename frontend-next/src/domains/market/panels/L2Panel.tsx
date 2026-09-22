import { EmptyState, Spinner } from "@/design/primitives";
import { marketApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtPrice, fmtVolume } from "@/shared/format";
import d from "../../domain.module.css";
import s from "./panels.module.css";

/**
 * 逐笔成交面板（券商 L2）。
 *
 * ★ 契约要点：逐笔走 `/market/l2`（券商 `get_l2_transactions`），**券商未连接时
 *   返回 503**；TDX 公共行情不提供逐笔。因此本面板在无券商时**明确提示**而不是
 *   留白 —— 留白会让人以为是「没成交」，实际是「没能力」。
 *
 * 零 mock：任一字段缺失显示「—」，不用 0 填充。
 */
export function L2Panel({
  code,
  count = 50,
  reloadToken = 0,
}: {
  code: string;
  count?: number;
  /** 外部「刷新逐笔」按钮用：自增即重新取数 */
  reloadToken?: number;
}) {
  const l2 = useAsync(() => marketApi.l2(code, count), [code, count, reloadToken]);
  const ticks = l2.data ?? [];

  if (l2.loading && !l2.data) return <Spinner label="加载逐笔…" />;

  if (l2.error) {
    return (
      <div className={`${d.note} ${d.noteWarn}`} style={{ margin: 8 }}>
        {l2.error}
        <div style={{ marginTop: 4 }}>
          逐笔成交（L2）由券商网关提供，TDX 公共行情不包含逐笔明细。
          未连接券商时该端点返回 503，这是零 mock 契约的预期行为。
        </div>
      </div>
    );
  }

  // ★ 走到这里说明 `/market/l2` **没有报错** —— 看 `app/routes/market.py::market_l2`：
  //   `b = _need()`，未连券商时 `no_broker()` 返 503，而那条路径**已被上面的 error 分支
  //   接管**（它会明说「未连接券商时该端点返回 503」）。⇒ 能走到空态，**券商必然是连着的**。
  //
  //   所以空态**不能**再写「未连接券商时为空是预期行为」：那句话在本分支下必然是错的，
  //   只会把用户送去「连接管理」白跑一趟（与 `Backtest.tsx` / `Dashboard.tsx` 同一类缺陷）。
  //   空态只说**能核实的事实**：券商已连接、端点正常返回、本标的没有逐笔推送。
  if (ticks.length === 0)
    return (
      <EmptyState text="暂无逐笔数据 —— 券商已连接，但本标的当前没有逐笔推送（TDX 公共行情不含逐笔明细；非交易时段或该标的当日无成交时为空）" />
    );

  return (
    <div className={d.scroll}>
      <table className={s.tickTable}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>时间</th>
            <th style={{ textAlign: "right" }}>价格</th>
            <th style={{ textAlign: "right" }}>数量</th>
            <th style={{ textAlign: "center" }}>方向</th>
          </tr>
        </thead>
        <tbody>
          {ticks.map((t, i) => (
            <tr key={i}>
              <td className={s.tickTime}>{String(t.time ?? "—")}</td>
              <td className={s.tickNum}>{fmtPrice(t.price)}</td>
              <td className={s.tickNum}>{fmtVolume(t.volume)}</td>
              <td className={s.tickDir}>
                {t.side === undefined ? (
                  "—"
                ) : (
                  <span style={{ color: t.side === "buy" ? "var(--up)" : "var(--down)" }}>
                    {t.side === "buy" ? "买" : t.side === "sell" ? "卖" : t.side}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default L2Panel;
