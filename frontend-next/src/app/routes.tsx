import { lazy, type ComponentType, type LazyExoticComponent } from "react";

/**
 * 页面注册表 · 顶部菜单 / 命令面板 / 工作区的单一真源。
 *
 * 设计要点（对标通达信）：
 * - MENU 即顶部菜单栏的一级项（6 大业务域），二级为域内页面
 * - 点击二级项 → 在工作区新开/聚焦 Tab，而不是在左栏展开
 * - 所有后端可达能力都必须在此有入口（修复旧前端「12 个页面不可达」问题）
 * - status: done=已实现 / planned=占位（占位页会显式展示其后端契约）
 */

export interface PageProps {
  params: Record<string, unknown>;
  tabId: string;
  leafId: string;
}

export type PageComponent = LazyExoticComponent<ComponentType<PageProps>>;

export interface PageDef {
  key: string;
  label: string;
  comp: PageComponent;
  /** 终端型页面：满幅渲染不加留白 */
  fullBleed?: boolean;
  /** 该页面已实现 / 仍为占位（占位页会显式展示其后端契约） */
  status?: "done" | "partial" | "planned";
}

const P = (loader: () => Promise<{ default: ComponentType<PageProps> }>): PageComponent =>
  lazy(loader);

/** 占位页工厂：把后端契约显式写在页面上，避免「看起来能用其实是空壳」 */
function placeholder(
  title: string,
  endpoints: string[],
  note: string,
): PageComponent {
  return lazy(() =>
    import("@/domains/_shared/PagePlaceholder").then((m) => ({
      default: m.makePlaceholder(title, endpoints, note),
    })),
  );
}

export const PAGES: Record<string, PageDef> = {
  /* ---------- 行情 ---------- */
  dashboard: { key: "dashboard", label: "仪表盘", comp: P(() => import("@/domains/Dashboard")), status: "done" },
  quoteboard: {
    key: "quoteboard",
    label: "报价牌",
    comp: P(() => import("@/domains/market/QuoteBoard")),
    fullBleed: true,
    status: "done",
  },
  quote: {
    key: "quote",
    label: "K 线分析",
    comp: P(() => import("@/domains/market/MarketData")),
    fullBleed: true,
    status: "done",
  },
  minutes: {
    key: "minutes",
    label: "分时图",
    comp: P(() => import("@/domains/market/Minutes")),
    fullBleed: true,
    status: "done",
  },
  orderbook: {
    key: "orderbook",
    label: "盘口逐笔",
    comp: P(() => import("@/domains/market/OrderBook")),
    fullBleed: true,
    status: "done",
  },
  deal_feed: {
    key: "deal_feed",
    label: "成交明细",
    comp: P(() => import("@/domains/market/DealFeed")),
    fullBleed: true,
    status: "done",
  },
  sector_radar: {
    key: "sector_radar",
    label: "板块雷达",
    comp: P(() => import("@/domains/market/SectorRadar")),
    fullBleed: true,
    status: "done",
  },
  moneyflow: {
    key: "moneyflow",
    label: "资金流",
    comp: P(() => import("@/domains/market/MoneyFlow")),
    fullBleed: true,
    status: "done",
  },
  etfs: {
    key: "etfs",
    label: "ETF",
    comp: P(() => import("@/domains/market/Etfs")),
    fullBleed: true,
    status: "done",
  },
  mktstructure: {
    key: "mktstructure",
    label: "市场结构",
    comp: P(() => import("@/domains/market/MarketStructure")),
    fullBleed: true,
    status: "done",
  },
  watchlist: {
    key: "watchlist",
    label: "自选股",
    comp: P(() => import("@/domains/market/Watchlist")),
    fullBleed: true,
    status: "done",
  },

  /* ---------- 研究 ---------- */
  screen: {
    key: "screen",
    label: "条件选股",
    comp: P(() => import("@/domains/research/Screen")),
    fullBleed: true,
    status: "done",
  },
  formula: {
    key: "formula",
    label: "公式选股",
    comp: P(() => import("@/domains/research/Formula")),
    fullBleed: true,
    status: "done",
  },
  factor_hub: {
    key: "factor_hub",
    label: "因子研究",
    comp: P(() => import("@/domains/research/FactorHub")),
    status: "done",
  },
  search: {
    key: "search",
    label: "标的检索",
    comp: P(() => import("@/domains/research/Search")),
    status: "done",
  },

  /* ---------- 交易 ---------- */
  trade: { key: "trade", label: "手动交易", comp: P(() => import("@/domains/trading/Trade")), status: "done" },
  algo: {
    key: "algo",
    label: "算法交易",
    comp: P(() => import("@/domains/trading/Algo")),
    status: "done",
  },
  conditions: {
    key: "conditions",
    label: "条件单",
    comp: P(() => import("@/domains/trading/Conditions")),
    status: "done",
  },
  limitup: {
    key: "limitup",
    label: "涨停监控",
    comp: P(() => import("@/domains/trading/LimitUp")),
    status: "done",
  },
  target_portfolio: {
    key: "target_portfolio",
    label: "目标持仓",
    comp: P(() => import("@/domains/trading/TargetPortfolio")),
    status: "done",
  },
  rebalance: {
    key: "rebalance",
    label: "分仓再平衡",
    comp: placeholder("分仓再平衡", ["POST /rebalance"], "再平衡端点已就绪，页面待实现。"),
    status: "planned",
  },

  /* ---------- 账户 ---------- */
  accounts: {
    key: "accounts",
    label: "多账户网格",
    comp: P(() => import("@/domains/account/Accounts")),
    status: "done",
  },
  positions: {
    key: "positions",
    label: "委托 / 持仓 / 成交",
    comp: P(() => import("@/domains/account/Positions")),
    fullBleed: true,
    status: "done",
  },
  reconcile: {
    key: "reconcile",
    label: "对账核销",
    comp: P(() => import("@/domains/account/Reconcile")),
    status: "done",
  },
  paper: {
    key: "paper",
    label: "模拟盘",
    comp: P(() => import("@/domains/account/Paper")),
    status: "done",
  },

  /* ---------- 自动化 ---------- */
  alerts: {
    key: "alerts",
    label: "告警规则",
    comp: P(() => import("@/domains/automation/Alerts")),
    status: "done",
  },
  webhooks: {
    key: "webhooks",
    label: "出站 Webhook",
    comp: P(() => import("@/domains/automation/Webhooks")),
    status: "done",
  },
  signals: {
    key: "signals",
    label: "外部信号",
    comp: P(() => import("@/domains/automation/Signals")),
    status: "done",
  },
  runtime_jobs: {
    key: "runtime_jobs",
    label: "定时任务",
    comp: P(() => import("@/domains/automation/RuntimeJobs")),
    status: "done",
  },

  /* ---------- 系统 ---------- */
  brokers: { key: "brokers", label: "连接管理", comp: P(() => import("@/domains/system/Brokers")), status: "done" },
  sysstatus: {
    key: "sysstatus",
    label: "系统状态",
    comp: P(() => import("@/domains/system/SystemStatus")),
    status: "done",
  },
  audit: {
    key: "audit",
    label: "审计日志",
    comp: P(() => import("@/domains/system/Audit")),
    status: "done",
  },
  apikeys: {
    key: "apikeys",
    label: "API Key",
    comp: P(() => import("@/domains/system/ApiKeys")),
    status: "done",
  },
  settings: {
    key: "settings",
    label: "设置",
    comp: P(() => import("@/domains/system/Settings")),
    status: "done",
  },
  system_log: {
    key: "system_log",
    label: "系统日志",
    comp: P(() => import("@/domains/system/SystemLog")),
    fullBleed: true,
    status: "done",
  },
};

export interface MenuGroup {
  key: string;
  label: string;
  items: string[];
}

/** 顶部菜单栏：一级 = 业务域，二级 = 域内页面 */
export const MENU: MenuGroup[] = [
  {
    key: "market",
    label: "行情",
    items: [
      "quoteboard",
      "quote",
      "minutes",
      "orderbook",
      "deal_feed",
      "sector_radar",
      "moneyflow",
      "etfs",
      "mktstructure",
      "watchlist",
    ],
  },
  { key: "research", label: "研究", items: ["screen", "formula", "factor_hub", "search"] },
  {
    key: "trading",
    label: "交易",
    items: ["trade", "algo", "conditions", "limitup", "target_portfolio", "rebalance"],
  },
  { key: "account", label: "账户", items: ["accounts", "positions", "reconcile", "paper"] },
  { key: "automation", label: "自动化", items: ["alerts", "webhooks", "signals", "runtime_jobs"] },
  {
    key: "system",
    label: "系统",
    items: ["dashboard", "brokers", "sysstatus", "audit", "apikeys", "settings", "system_log"],
  },
];

export const DEFAULT_PAGE = "dashboard";

export function pageLabel(key: string): string {
  return PAGES[key]?.label ?? key;
}

/** 命令面板用：全部页面的扁平列表 */
export function allPages(): PageDef[] {
  return Object.values(PAGES);
}
