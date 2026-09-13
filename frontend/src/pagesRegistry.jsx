// 页面注册表：所有页面的「树形分组 + 扁平映射」单一真相来源。
// 顶菜单（MenuBar）、命令面板（CommandPalette）、中央工作区（Workbench）共用。
// v2 精简：21→17 页，BottomDock 5 Tab 提升为独立页面，纯顶部菜单。
import { lazy } from "react";

// 深度优化：SPA 重部署后浏览器可能持有旧的 entry chunk，
// 其引用的旧 hash 分片已被构建清理 → 动态 import 返回 404
// （"Failed to fetch dynamically imported module"），导致整页/多页打不开。
// 这里在分片加载失败时，自动整页刷新一次以拉取最新构建；
// 用 sessionStorage 守卫避免无限刷新循环。
const CHUNK_RELOAD_FLAG = "qmt_chunk_reload_v1";

function lazyWithRetry(imp, key) {
  return lazy(async () => {
    try {
      return await imp();
    } catch (err) {
      const msg = String((err && err.message) || err || "");
      const isChunkError =
        /Failed to fetch dynamically imported module/i.test(msg) ||
        /Importing a module script failed/i.test(msg) ||
        /error loading dynamically imported module/i.test(msg);
      if (isChunkError && !sessionStorage.getItem(CHUNK_RELOAD_FLAG + ":" + key)) {
        sessionStorage.setItem(CHUNK_RELOAD_FLAG + ":" + key, "1");
        location.reload();
        return new Promise(() => {});
      }
      throw err;
    }
  });
}

const D = (imp) => {
  const src = imp.toString();
  const m = src.match(/import\(\s*["']([^"']+)["']\s*\)/);
  const key = m ? m[1] : "unknown";
  return lazyWithRetry(imp, key);
};

// ================= 旧 key → 保留页 key 归一 =================
// 被移除页面（回测/策略/模拟盘/信号/数据中心/审计/目标持仓等）的旧 key
// 统一回退到仪表盘，避免旧收藏/持久化数据导致空白页。
export const KEY_ALIAS = {
  // 市场结构 Hub 子页（保留）
  boards: "mktstructure", etfs: "mktstructure", index_overview: "mktstructure", rotation: "mktstructure",
  // 已移除页面 → dashboard
  strategies: "dashboard", strmarket: "dashboard",
  backtest: "dashboard", research: "dashboard",
  factors: "factor_hub",
  signal: "dashboard", alerts: "dashboard", notifications: "dashboard", webhooks: "dashboard",
  markettools: "dashboard", reference: "dashboard",
  audit: "sysstatus", reconcile: "sysstatus",
  datacenter: "sysstatus",
  target: "dashboard", rebalance: "dashboard",
  paper: "dashboard", limitup: "dashboard",
  automation: "dashboard",
  // 新增独立页（原 BottomDock Tab）
  watchlist: "watchlist", sector_radar: "sector_radar",
  moneyflow: "moneyflow", deal_feed: "deal_feed", system_log: "system_log",
};

export function resolveKey(key) {
  const k = KEY_ALIAS[key] || key;
  return PAGES[k] ? k : DEFAULT_PAGE;
}

export function aliasTab(key) {
  return KEY_ALIAS[key] ? key : undefined;
}

export const PAGES = {
  // fullBleed: 终端型页面（行情/K线/报价牌/选股/市场结构），满幅渲染不加留白；
  // 其余文档型页面由 Pane 统一包裹 .pane-leaf-body.padded 提供四周留白。
  dashboard: { label: "仪表盘", comp: D(() => import("./components/Dashboard.jsx")) },
  quote: { label: "行情分析", comp: D(() => import("./features/market/MarketData.jsx")), fullBleed: true },
  quoteboard: { label: "报价牌", comp: D(() => import("./features/market/QuoteBoard.jsx")), fullBleed: true },
  mktstructure: { label: "市场结构", comp: D(() => import("./hubs/MarketStructureHub.jsx")), fullBleed: true },
  sector_radar: { label: "板块雷达", comp: D(() => import("./features/market/SectorRadar.jsx")), fullBleed: true },
  moneyflow: { label: "资金流", comp: D(() => import("./features/market/Moneyflow.jsx")), fullBleed: true },
  deal_feed: { label: "成交明细", comp: D(() => import("./features/market/DealFeed.jsx")), fullBleed: true },
  screen: { label: "条件选股", comp: D(() => import("./features/research/Screen.jsx")), fullBleed: true },
  factor_hub: { label: "因子研究", comp: D(() => import("./hubs/FactorHub.jsx")) },
  trade: { label: "手动交易", comp: D(() => import("./features/trading/Trade.jsx")) },
  algo: { label: "算法交易", comp: D(() => import("./features/trading/Algo.jsx")) },
  watchlist: { label: "自选股", comp: D(() => import("./features/market/Watchlist.jsx")), fullBleed: true },
  brokers: { label: "连接管理", comp: D(() => import("./features/system/Brokers.jsx")) },
  accounts: { label: "多账户网格", comp: D(() => import("./features/accounts/AccountsGrid.jsx")) },
  sysstatus: { label: "系统状态", comp: D(() => import("./features/system/SystemStatus.jsx")) },
  system_log: { label: "系统日志", comp: D(() => import("./features/system/SystemLog.jsx")), fullBleed: true },
  settings: { label: "设置", comp: D(() => import("./features/system/Settings.jsx")) },
};

// 页面分组树（命令面板 / 顶部下拉菜单共用）：4 组 17 页
export const PAGE_TREE = [
  { group: "行情", items: [
    { key: "quoteboard", label: "报价牌" },
    { key: "quote", label: "行情分析" },
    { key: "mktstructure", label: "市场结构" },
    { key: "sector_radar", label: "板块雷达" },
    { key: "moneyflow", label: "资金流" },
    { key: "deal_feed", label: "成交明细" },
  ]},
  { group: "研究", items: [
    { key: "screen", label: "条件选股" },
    { key: "factor_hub", label: "因子研究" },
  ]},
  { group: "交易", items: [
    { key: "trade", label: "手动交易" },
    { key: "algo", label: "算法交易" },
    { key: "watchlist", label: "自选股" },
  ]},
  { group: "系统", items: [
    { key: "dashboard", label: "仪表盘" },
    { key: "brokers", label: "连接管理" },
    { key: "accounts", label: "多账户网格" },
    { key: "sysstatus", label: "系统状态" },
    { key: "system_log", label: "系统日志" },
    { key: "settings", label: "设置" },
  ]},
];

export const DEFAULT_PAGE = "dashboard";
