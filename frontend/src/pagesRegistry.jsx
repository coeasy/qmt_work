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

export const PAGES = {
  dashboard: { label: "仪表盘", comp: D(() => import("./components/Dashboard.jsx")) },
  quote: { label: "行情分析", comp: D(() => import("./components/MarketData.jsx")) },
  quoteboard: { label: "报价牌", comp: D(() => import("./components/QuoteBoard.jsx")) },
  boards: { label: "板块行情", comp: D(() => import("./components/Boards.jsx")) },
  etfs: { label: "ETF 基金", comp: D(() => import("./components/Etfs.jsx")) },
  index_overview: { label: "指数分析", comp: D(() => import("./components/IndexOverview.jsx")) },
  rotation: { label: "板块轮动", comp: D(() => import("./components/Rotation.jsx")) },
  markettools: { label: "行情工具", comp: D(() => import("./components/MarketTools.jsx")) },

  trade: { label: "手动交易", comp: D(() => import("./components/Trade.jsx")) },
  limitup: { label: "涨停监控", comp: D(() => import("./components/LimitUp.jsx")) },
  algo: { label: "算法交易", comp: D(() => import("./components/Algo.jsx")) },
  paper: { label: "模拟盘", comp: D(() => import("./components/Paper.jsx")) },

  strategies: { label: "模板生成", comp: D(() => import("./components/Strategies.jsx")) },
  strmarket: { label: "策略市场", comp: D(() => import("./components/StrategyMarket.jsx")) },
  target: { label: "目标持仓", comp: D(() => import("./components/TargetPortfolio.jsx")) },
  rebalance: { label: "即时再平衡", comp: D(() => import("./components/Rebalance.jsx")) },

  backtest: { label: "回测对比", comp: D(() => import("./components/Backtest.jsx")) },
  factors: { label: "因子/指标", comp: D(() => import("./components/Factors.jsx")) },
  research: { label: "研究深度", comp: D(() => import("./components/Research.jsx")) },
  reference: { label: "参考数据", comp: D(() => import("./components/Reference.jsx")) },

  signal: { label: "信号路由", comp: D(() => import("./components/Signal.jsx")) },
  alerts: { label: "告警规则", comp: D(() => import("./components/Alerts.jsx")) },
  notifications: { label: "通知渠道", comp: D(() => import("./components/Notifications.jsx")) },
  webhooks: { label: "出站 Webhook", comp: D(() => import("./components/Webhooks.jsx")) },

  brokers: { label: "连接管理", comp: D(() => import("./components/Brokers.jsx")) },
  accounts: { label: "多账户网格", comp: D(() => import("./components/AccountsGrid.jsx")) },

  sysstatus: { label: "系统状态", comp: D(() => import("./components/SystemStatus.jsx")) },
  audit: { label: "审计日志", comp: D(() => import("./components/Audit.jsx")) },
  reconcile: { label: "对账核销", comp: D(() => import("./components/Reconcile.jsx")) },

  settings: { label: "设置", comp: D(() => import("./components/Settings.jsx")) },
};

// 功能树（通达信式左树）：分组 -> 叶子
export const PAGE_TREE = [
  { group: "总览", items: [{ key: "dashboard", label: "仪表盘" }] },
  { group: "行情", items: [
    { key: "quoteboard", label: "报价牌" },
    { key: "boards", label: "板块行情" },
    { key: "etfs", label: "ETF 基金" },
    { key: "index_overview", label: "指数分析" },
    { key: "rotation", label: "板块轮动" },
    { key: "quote", label: "行情分析" },
    { key: "markettools", label: "行情工具" },
  ] },
  { group: "交易", items: [
    { key: "trade", label: "手动交易" },
    { key: "limitup", label: "涨停监控" },
    { key: "algo", label: "算法交易" },
    { key: "paper", label: "模拟盘" },
  ] },
  { group: "策略与组合", items: [
    { key: "strategies", label: "模板生成" },
    { key: "strmarket", label: "策略市场" },
    { key: "target", label: "目标持仓" },
    { key: "rebalance", label: "即时再平衡" },
  ] },
  { group: "研究", items: [
    { key: "backtest", label: "回测对比" },
    { key: "factors", label: "因子/指标" },
    { key: "research", label: "研究深度" },
    { key: "reference", label: "参考数据" },
  ] },
  { group: "信号与告警", items: [
    { key: "signal", label: "信号路由" },
    { key: "alerts", label: "告警规则" },
    { key: "notifications", label: "通知渠道" },
    { key: "webhooks", label: "出站 Webhook" },
  ] },
  { group: "账户", items: [
    { key: "brokers", label: "连接管理" },
    { key: "accounts", label: "多账户网格" },
  ] },
  { group: "运维", items: [
    { key: "sysstatus", label: "系统状态" },
    { key: "audit", label: "审计日志" },
    { key: "reconcile", label: "对账核销" },
  ] },
  { group: "系统", items: [
    { key: "settings", label: "设置" },
  ] },
];

export const DEFAULT_PAGE = "dashboard";
