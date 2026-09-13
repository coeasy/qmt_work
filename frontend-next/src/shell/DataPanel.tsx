import { useMemo } from "react";
import { useUiStore, type DataPanelTab } from "@/stores/ui";
import { useWatchlistStore } from "@/stores/watchlist";
import { useQuotesStore } from "@/stores/quotes";
import { useWorkspaceStore } from "@/stores/workspace";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPct, fmtPrice, toneColor } from "@/shared/format";
import s from "./shell.module.css";
import d from "./datapanel.module.css";

const TABS: Array<{ key: DataPanelTab; label: string }> = [
  { key: "watchlist", label: "自选股" },
  { key: "orderbook", label: "盘口" },
  { key: "alerts", label: "预警" },
];

/**
 * 左侧数据面板。
 *
 * ★ 布局决策（方案 §4.4）：左栏不再承载功能树，改为真实数据面板。
 * 导航已收进顶部菜单，左栏的价值在于「常驻可见的交易数据」。
 */
export function DataPanel() {
  const open = useUiStore((st) => st.dataPanelOpen);
  const tab = useUiStore((st) => st.dataPanelTab);
  const setTab = useUiStore((st) => st.setDataPanelTab);

  return (
    <div
      className={[s.datapanel, open ? "" : s.datapanelCollapsed].filter(Boolean).join(" ")}
      aria-hidden={!open}
    >
      <div className={s.datapanelHead}>
        <div className={s.datapanelTabs}>
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={[s.datapanelTab, t.key === tab ? s.datapanelTabActive : ""]
                .filter(Boolean)
                .join(" ")}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className={s.datapanelBody}>
        {tab === "watchlist" && <WatchlistPanel />}
        {tab === "orderbook" && (
          <div className={d.empty}>在「K 线分析」页查看选中标的的五档盘口</div>
        )}
        {tab === "alerts" && (
          <div className={d.empty}>告警规则页面待实现（端点已就绪）</div>
        )}
      </div>
    </div>
  );
}

function WatchlistPanel() {
  const codes = useWatchlistStore((st) => st.codes);
  const remove = useWatchlistStore((st) => st.remove);
  const quotes = useQuotesStore((st) => st.quotes);
  const open = useWorkspaceStore((st) => st.open);

  // 订阅聚合：整个面板只占 codes.length 个服务端订阅，与打开多少 Tab 无关
  const stable = useMemo(() => codes, [codes]);
  useQuoteSubscription(stable);

  if (codes.length === 0) {
    return <div className={d.empty}>自选股为空，在 K 线页点击「加自选」</div>;
  }

  return (
    <div className={d.list}>
      {codes.map((code) => {
        const q = quotes[code];
        const pct = q?.change_pct;
        return (
          <div
            key={code}
            className={d.row}
            onClick={() =>
              open("quote", { code, name: q?.name ?? "" }, { title: q?.name ?? code })
            }
          >
            <div style={{ minWidth: 0 }}>
              <div className={d.name}>{q?.name ?? code}</div>
              <div className={d.code}>{code}</div>
            </div>
            <div className={d.price} style={{ color: toneColor(pct) }}>
              {fmtPrice(q?.price)}
            </div>
            <div className={d.pct} style={{ color: toneColor(pct) }}>
              {fmtPct(pct)}
            </div>
            <button
              type="button"
              className={d.remove}
              aria-label={`移除 ${code}`}
              onClick={(e) => {
                e.stopPropagation();
                remove(code);
              }}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
