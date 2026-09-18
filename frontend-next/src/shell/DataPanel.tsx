import { useMemo } from "react";
import { useUiStore, type DataPanelTab } from "@/stores/ui";
import { useWatchlistStore } from "@/stores/watchlist";
import { useWorkspaceStore, collectLeaves, type PaneNode } from "@/stores/workspace";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { ConfirmButton } from "@/design/primitives";
import { OrderBookPanel } from "@/domains/market/panels/OrderBookPanel";
import { fmtPct, fmtPrice, namePair, toneColor } from "@/shared/format";
import s from "./shell.module.css";
import d from "./datapanel.module.css";
import p from "@/domains/market/panels/panels.module.css";

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
        {tab === "orderbook" && <OrderBookTab />}
        {tab === "alerts" && <AlertsShortcut />}
      </div>
    </div>
  );
}

type LeafNode = Extract<PaneNode, { kind: "leaf" }>;
const isLeaf = (n: PaneNode): n is LeafNode => n.kind === "leaf";

/**
 * 左栏「当前标的」的推导顺序（没有全局 currentSymbol，就近推导比新造全局态便宜）：
 *   1. 活动 Tab 的**活动叶子**的 `params.code`（工作台/K线页都会带）
 *   2. 该 Tab 的第一个叶子的 `params.code`
 *   3. 自选股第一只
 * 三步都拿不到才返回空串，由调用方给出「先选一只」的出路。
 */
function useCurrentSymbol(): string {
  const codes = useWatchlistStore((st) => st.codes);
  const tabs = useWorkspaceStore((st) => st.tabs);
  const activeId = useWorkspaceStore((st) => st.activeId);
  return useMemo(() => {
    const tab = tabs.find((t) => t.id === activeId);
    if (tab) {
      const leaves = collectLeaves(tab.tree).filter(isLeaf);
      const leaf = leaves.find((l) => l.id === tab.activeLeafId) ?? leaves[0];
      const c = leaf?.params?.code;
      if (typeof c === "string" && c) return c;
    }
    return codes[0] ?? "";
  }, [tabs, activeId, codes]);
}

/**
 * 左栏「盘口」Tab。
 *
 * 修复前这里只有一句「在 K 线分析页查看五档盘口」——即**永远没有数据**。
 * 现在直接复用工作台抽出的 `OrderBookPanel`（同一实现、同一份样式），
 * 左栏常驻即可看到五档，不必来回切页。
 *
 * ⚠️ 必须**自己订阅**：`useQuoteSubscription` 是按组件挂载/卸载增减引用计数的，
 * 切到本 Tab 时 `WatchlistPanel` 已卸载 ⇒ 不订阅就永远拿不到 quote，
 * 表现是「盘口全空但也不报错」。
 */
function OrderBookTab() {
  const code = useCurrentSymbol();
  const one = useMemo(() => (code ? [code] : []), [code]);
  useQuoteSubscription(one);
  if (!code) {
    return <div className={d.empty}>先添加自选股，或在工作台选中一只标的</div>;
  }
  return (
    <div className={p.panelHost}>
      <OrderBookPanel code={code} />
    </div>
  );
}

/**
 * 左栏「预警」Tab。
 *
 * 修复前写着「告警规则页面待实现（端点已就绪）」——这句是**过期且错误**的：
 * `domains/automation/Alerts.tsx` 早已实现（消费 alertApi 的 5 个方法）。
 * 与其在这里重复一遍规则表（会与告警页两处漂移），不如如实说明入口在哪并给出路。
 */
function AlertsShortcut() {
  const open = useWorkspaceStore((st) => st.open);
  return (
    <div className={d.section}>
      <div>预警规则在「自动化 → 告警」页维护。</div>
      <button type="button" className={d.link} onClick={() => open("alerts")}>
        打开告警页 →
      </button>
    </div>
  );
}

function WatchlistPanel() {
  const codes = useWatchlistStore((st) => st.codes);
  const remove = useWatchlistStore((st) => st.remove);
  const open = useWorkspaceStore((st) => st.open);
  // 订阅聚合：整个面板只占 codes.length 个服务端订阅，与打开多少 Tab 无关。
  // 用 useLiveQuotes 而不是「订阅 + 取 store」两行 —— 漏掉订阅不会报错，
  // 只会让价格永远停在「--」，是「界面没有真实数据」里最难发现的一类。
  const quotes = useLiveQuotes(codes);

  if (codes.length === 0) {
    return <div className={d.empty}>自选股为空，在 K 线页点击「加自选」</div>;
  }

  return (
    <div className={d.list}>
      {codes.map((code) => {
        const q = quotes[code];
        const pct = q?.change_pct;
        // 名称未知时只显示一次代码，避免「名称槽 + 代码槽」渲染两遍造成重影
        const [title, sub] = namePair(q?.name, code);
        return (
          <div
            key={code}
            className={d.row}
            onClick={() =>
              open("quote", { code, name: q?.name ?? "" }, { title: q?.name ?? code })
            }
          >
            <div style={{ minWidth: 0 }}>
              <div className={d.name}>{title}</div>
              {sub && <div className={d.code}>{sub}</div>}
            </div>
            <div className={d.price} style={{ color: toneColor(pct) }}>
              {fmtPrice(q?.price)}
            </div>
            <div className={d.pct} style={{ color: toneColor(pct) }}>
              {fmtPct(pct)}
            </div>
            {/* 移除按钮：常驻可见 + 两段式确认（见 ConfirmButton 的注释）。
                修复前它是「opacity:0 + hover 才浮现」，且因 grid 少写一列被挤到
                隐式第二行、溢出压在下一行上 —— 点第 N+1 行会删掉第 N 行。 */}
            <ConfirmButton
              className={d.remove}
              title={`移除自选 ${q?.name ?? code}`}
              confirmText="确认"
              onConfirm={() => remove(code)}
            >
              ×
            </ConfirmButton>
          </div>
        );
      })}
    </div>
  );
}
