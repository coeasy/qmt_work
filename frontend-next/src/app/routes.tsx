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
  /**
   * 是否出现在顶部主菜单（默认 `true`）。
   *
   * 置 `false` 表示「**已被合并进更上层的页面**，不再作为菜单入口」，但仍注册在
   * `PAGES` 里 —— 也就是说它必须另有入口（例如行情工作台头部的「独立打开」按钮组），
   * 否则就退化成旧前端那种「页面不可达却无人发现」的问题。
   *
   * `MENU` 数组仍然完整列出它们（命令面板分组、快捷键序号都依赖它），
   * 只是 `MenuBar` 渲染时跳过 —— 这样「隐藏」不会打乱任何基于 MENU 的索引。
   */
  menu?: boolean;
  /**
   * `menu: false` 时**必须**声明它现在从哪个页面进入（该页面的 key）。
   *
   * 这是把「页面去哪了」变成**可断言的事实**：旧前端就栽在「页面注册了但没人能到达」
   * 上（12 个页面不可达却无人发现）。有了这个字段，门禁就能在 CI 里检查
   * 「每个隐藏页面都另有入口」，而不是靠人记得去读代码。
   */
  entryFrom?: string;
}

const P = (loader: () => Promise<{ default: ComponentType<PageProps> }>): PageComponent =>
  lazy(loader);

export const PAGES: Record<string, PageDef> = {
  /* ---------- 行情 ---------- */
  dashboard: { key: "dashboard", label: "仪表盘", comp: P(() => import("@/domains/Dashboard")), status: "done" },
  workbench: {
    key: "workbench",
    label: "行情工作台",
    comp: P(() => import("@/domains/market/MarketWorkbench")),
    fullBleed: true,
    status: "done",
  },
  quoteboard: {
    key: "quoteboard",
    label: "报价牌",
    comp: P(() => import("@/domains/market/QuoteBoard")),
    fullBleed: true,
    status: "done",
    // 已合并进「行情工作台」；入口在工作台头部「独立打开」按钮组
    menu: false,
    entryFrom: "workbench",
  },
  quote: {
    key: "quote",
    label: "K 线分析",
    comp: P(() => import("@/domains/market/MarketData")),
    fullBleed: true,
    status: "done",
    menu: false,
    // 入口：行情工作台头部的「独立打开」按钮组
    entryFrom: "workbench",
  },
  minutes: {
    key: "minutes",
    label: "分时图",
    comp: P(() => import("@/domains/market/Minutes")),
    fullBleed: true,
    status: "done",
    menu: false,
    // 入口：行情工作台头部的「独立打开」按钮组
    entryFrom: "workbench",
  },
  orderbook: {
    key: "orderbook",
    label: "盘口逐笔",
    comp: P(() => import("@/domains/market/OrderBook")),
    fullBleed: true,
    status: "done",
    menu: false,
    // 入口：行情工作台头部的「独立打开」按钮组
    entryFrom: "workbench",
  },
  deal_feed: {
    key: "deal_feed",
    label: "成交明细",
    comp: P(() => import("@/domains/market/DealFeed")),
    fullBleed: true,
    status: "done",
    menu: false,
    // 入口：行情工作台头部的「独立打开」按钮组
    entryFrom: "workbench",
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
  screen_workbench: {
    key: "screen_workbench",
    label: "选股工作台",
    comp: P(() => import("@/domains/research/ScreenWorkbench")),
    status: "done",
  },
  auto_picks: {
    key: "auto_picks",
    label: "自动选股",
    comp: P(() => import("@/domains/research/screen/AutoPicks")),
    fullBleed: true,
    status: "done",
  },
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
    comp: P(() => import("@/domains/trading/Rebalance")),
    status: "done",
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
  offline: {
    key: "offline",
    label: "离线数据",
    comp: P(() => import("@/domains/system/OfflineData")),
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
  mcp: {
    key: "mcp",
    label: "MCP 工具",
    comp: P(() => import("@/domains/system/McpTools")),
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
      // 工作台 = 报价牌 + K 线 + 分时 + 盘口 + 成交流 + 基本信息 + 基本面 + 交易
      // 合并展示。后面 5 项（quoteboard/quote/minutes/orderbook/deal_feed）标了
      // menu:false ⇒ 不再出现在下拉里（去掉重复入口），但页面仍注册、仍可用，
      // 入口在工作台头部的「独立打开」按钮组（多显示器 / 分栏对照时仍需要）。
      "workbench",
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
  {
    key: "research",
    label: "研究",
    items: ["screen_workbench", "auto_picks", "screen", "formula", "factor_hub", "search"],
  },
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
    items: [
      "dashboard",
      "brokers",
      "sysstatus",
      "offline",
      "audit",
      "apikeys",
      "mcp",
      "settings",
      "system_log",
    ],
  },
];

export const DEFAULT_PAGE = "dashboard";

export function pageLabel(key: string): string {
  return PAGES[key]?.label ?? key;
}

/**
 * 某个菜单分组里**应当显示**的条目。
 *
 * `menu: false` 的页面已被合并进更上层的页面（如行情工作台），不再重复列出。
 * 注意只过滤**显示**，不过滤 `MENU` 本身 —— 命令面板分组与快捷键序号都基于
 * `MENU` 的全量列表，改动那份列表会连带打乱索引。
 */
export function visibleMenuItems(group: MenuGroup): string[] {
  return group.items.filter((k) => {
    const def = PAGES[k];
    return def !== undefined && def.menu !== false;
  });
}

/** 已合并进上层页面、不在主菜单显示的页面（供门禁断言「它们仍另有入口」） */
export function hiddenMenuPages(): string[] {
  return Object.values(PAGES)
    .filter((d) => d.menu === false)
    .map((d) => d.key);
}

/** 命令面板用：全部页面的扁平列表 */
export function allPages(): PageDef[] {
  return Object.values(PAGES);
}
