// 页面注册表：所有页面的「树形分组 + 扁平映射」单一真相来源。
// 顶菜单（MenuBar）、左侧功能树（FunctionTree）、中央工作区（Workbench）共用。
// 通达信式重构：每个叶子即一个独立窗口（Workbench 标签），分组仅用于树形归类。
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
        // index.html 已配置 no-cache，整页刷新必拉取最新入口与新分片
        location.reload();
        // 保持 promise pending，避免刷新前 React 抛错 / ErrorBoundary 闪现
        return new Promise(() => {});
      }
      throw err;
    }
  });
}

// 保留原始 import(...) 字面量供 Vite 静态分析做代码分割，
// 同时用工厂源码提取模块路径作为刷新守卫的 key。
const D = (imp) => {
  const src = imp.toString();
  const m = src.match(/import\(\s*["']([^"']+)["']\s*\)/);
  const key = m ? m[1] : "unknown";
  return lazyWithRetry(imp, key);
};

// ================= 旧 key → 中心页 key 归一（阶段一入口合并） =================
// 被并入 Hub 子页签的旧页面 key，在导航/持久化/迁移三处统一归一。
// 归并后的旧 key 仍可通过 nav / 菜单 / 命令面板打开，表现为「中心页 + 对应子页签」。
export const KEY_ALIAS = {
  boards: "mktstructure", etfs: "mktstructure", index_overview: "mktstructure", rotation: "mktstructure",
  strategies: "strategy_hub", strmarket: "strategy_hub",
  factors: "factor_hub", research: "factor_hub",
  signal: "automation", alerts: "automation", notifications: "automation", webhooks: "automation",
  markettools: "datacenter", reference: "datacenter",
  audit: "audit_recon", reconcile: "audit_recon",
};

// 把任意 key 解析为 PAGES 中实际存在的 key（旧 key → 新中心 key；非法 → 默认页）。
export function resolveKey(key) {
  const k = KEY_ALIAS[key] || key;
  return PAGES[k] ? k : DEFAULT_PAGE;
}

// 若 key 是被归并的旧 key，返回它在目标中心页里应携带的 tab 参数，否则 undefined。
export function aliasTab(key) {
  return KEY_ALIAS[key] ? key : undefined;
}

export const PAGES = {
  dashboard: { label: "仪表盘", comp: D(() => import("./components/Dashboard.jsx")) },
  quote: { label: "行情分析", comp: D(() => import("./components/MarketData.jsx")) },
  quoteboard: { label: "报价牌", comp: D(() => import("./components/QuoteBoard.jsx")) },
  mktstructure: { label: "市场结构", comp: D(() => import("./hubs/MarketStructureHub.jsx")) },
  screen: { label: "条件选股", comp: D(() => import("./components/Screen.jsx")) },

  trade: { label: "手动交易", comp: D(() => import("./components/Trade.jsx")) },
  limitup: { label: "涨停监控", comp: D(() => import("./components/LimitUp.jsx")) },
  algo: { label: "算法交易", comp: D(() => import("./components/Algo.jsx")) },
  paper: { label: "模拟盘", comp: D(() => import("./components/Paper.jsx")) },

  strategy_hub: { label: "策略工场", comp: D(() => import("./hubs/StrategyHub.jsx")) },
  target: { label: "目标持仓", comp: D(() => import("./components/TargetPortfolio.jsx")) },
  rebalance: { label: "即时再平衡", comp: D(() => import("./components/Rebalance.jsx")) },

  backtest: { label: "回测对比", comp: D(() => import("./components/Backtest.jsx")) },
  factor_hub: { label: "因子研究", comp: D(() => import("./hubs/FactorHub.jsx")) },

  automation: { label: "信号与自动化", comp: D(() => import("./hubs/AutomationHub.jsx")) },

  brokers: { label: "连接管理", comp: D(() => import("./components/Brokers.jsx")) },
  accounts: { label: "多账户网格", comp: D(() => import("./components/AccountsGrid.jsx")) },

  datacenter: { label: "数据中心", comp: D(() => import("./hubs/DataCenterHub.jsx")) },
  audit_recon: { label: "审计对账", comp: D(() => import("./hubs/AuditReconHub.jsx")) },
  sysstatus: { label: "系统状态", comp: D(() => import("./components/SystemStatus.jsx")) },

  settings: { label: "设置", comp: D(() => import("./components/Settings.jsx")) },
};

// 功能树（通达信式左树）：分组 -> 叶子（阶段一：入口合并为 7 中心组 21 顶层页）
export const PAGE_TREE = [
  { group: "总览", items: [{ key: "dashboard", label: "仪表盘" }] },
  { group: "行情", items: [
    { key: "quoteboard", label: "报价牌" },
    { key: "quote", label: "行情分析" },
    { key: "mktstructure", label: "市场结构" },
  ] },
  { group: "交易", items: [
    { key: "trade", label: "手动交易" },
    { key: "limitup", label: "涨停监控" },
    { key: "algo", label: "算法交易" },
    { key: "paper", label: "模拟盘" },
  ] },
  { group: "组合与策略", items: [
    { key: "strategy_hub", label: "策略工场" },
    { key: "target", label: "目标持仓" },
    { key: "rebalance", label: "即时再平衡" },
  ] },
  { group: "研究", items: [
    { key: "backtest", label: "回测对比" },
    { key: "factor_hub", label: "因子研究" },
  ] },
  { group: "选股与信号", items: [
    { key: "screen", label: "条件选股" },
    { key: "automation", label: "信号与自动化" },
  ] },
  { group: "系统运维", items: [
    { key: "brokers", label: "连接管理" },
    { key: "accounts", label: "多账户网格" },
    { key: "datacenter", label: "数据中心" },
    { key: "audit_recon", label: "审计对账" },
    { key: "sysstatus", label: "系统状态" },
    { key: "settings", label: "设置" },
  ] },
];

export const DEFAULT_PAGE = "dashboard";
