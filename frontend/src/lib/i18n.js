// T25 i18n 基础框架（轻量，出海前可用）。
//
// 现状：项目纯中文硬编码（~200+ 字符串散落组件）。本模块提供最小可用骨架：
//   - t(key, params?)：查字典，缺省回退 key 本身；
//   - setLocale(locale)：切换语言（"zh" | "en"）；
//   - 字典按需注册（registerDict），避免一次性搬运全部文案（增量迁移策略：
//     新组件直接走 t()，存量组件按页逐步迁移）。
//
// 注意：不做运行时 bundle 重载/复数/ICU，仅覆盖基础 key-value + 参数插值，
// 满足「能切语言、不会漏翻」的最低门槛；完整方案见 docs/多语言接入指南.md。
const _dicts = {
  zh: {
    "common.loading": "加载中…",
    "common.refresh": "刷新",
    "common.close": "关闭",
    "common.confirm": "确认",
    "common.cancel": "取消",
    "common.delete": "删除",
    "common.empty": "暂无数据",
    "common.retry": "重试",
    "nav.back": "返回",
    // 页面标签（与 pagesRegistry 对齐，key = page key）
    "page.dashboard": "仪表盘",
    "page.quote": "行情分析",
    "page.quoteboard": "报价牌",
    "page.boards": "板块行情",
    "page.etfs": "ETF 基金",
    "page.index_overview": "指数分析",
    "page.rotation": "板块轮动",
    "page.markettools": "行情工具",
    "page.screen": "条件选股",
    "page.trade": "手动交易",
    "page.limitup": "涨停监控",
    "page.algo": "算法交易",
    "page.paper": "模拟盘",
    "page.strategies": "模板生成",
    "page.strmarket": "策略市场",
    "page.target": "目标持仓",
    "page.rebalance": "即时再平衡",
    "page.backtest": "回测对比",
    "page.factors": "因子/指标",
    "page.research": "研究深度",
    "page.reference": "参考数据",
    "page.signal": "信号路由",
    "page.alerts": "告警规则",
    "page.notifications": "通知渠道",
    "page.webhooks": "出站 Webhook",
    "page.brokers": "连接管理",
    "page.accounts": "多账户网格",
    "page.sysstatus": "系统状态",
    "page.audit": "审计日志",
    "page.reconcile": "对账核销",
    "page.settings": "设置",
    // 分组（菜单树）
    "group.dashboard": "总览",
    "group.market": "行情",
    "group.trade": "交易",
    "group.strategy": "策略",
    "group.analysis": "分析",
    "group.system": "系统",
    "group.other": "其他",
    // 顶菜单（MenuBar）
    "menu.系统": "系统",
    "menu.行情": "行情",
    "menu.分析": "分析",
    "menu.交易": "交易",
    "menu.策略": "策略",
    "menu.研究": "研究",
    "menu.信号": "信号",
    "menu.账户": "账户",
    "menu.运维": "运维",
    // 页面标题（page.<key>.title，用于 h2.page-title）
    "page.accounts.title": "多账户网格",
    "page.audit.title": "审计日志",
    "page.brokers.title": "券商连接管理",
    "page.dashboard.title": "仪表盘",
    "page.limitup.title": "涨停板 · 打板助手",
    "page.quote.title": "行情分析",
    "page.markettools.title": "行情工具",
    "page.notifications.title": "通知渠道配置",
    "page.rebalance.title": "分仓再平衡",
    "page.reference.title": "参考数据",
    "page.settings.title": "设置",
    "page.strategies.title": "策略模板库",
    "page.trade.title": "手动交易",
    // 功能树分组（PAGE_TREE）
    "tree.总览": "总览",
    "tree.行情": "行情",
    "tree.交易": "交易",
    "tree.策略与组合": "策略与组合",
    "tree.研究": "研究",
    "tree.信号": "信号",
    "tree.系统与运维": "系统与运维",
    "tree.账户": "账户",
  },
  en: {
    "common.loading": "Loading…",
    "common.refresh": "Refresh",
    "common.close": "Close",
    "common.confirm": "Confirm",
    "common.cancel": "Cancel",
    "common.delete": "Delete",
    "common.empty": "No data",
    "common.retry": "Retry",
    "nav.back": "Back",
    "page.dashboard": "Dashboard",
    "page.quote": "Quotes",
    "page.quoteboard": "Quote Board",
    "page.boards": "Boards",
    "page.etfs": "ETFs",
    "page.index_overview": "Indices",
    "page.rotation": "Rotation",
    "page.markettools": "Market Tools",
    "page.screen": "Screener",
    "page.trade": "Trade",
    "page.limitup": "Limit Up",
    "page.algo": "Algo",
    "page.paper": "Paper",
    "page.strategies": "Strategies",
    "page.strmarket": "Strategy Market",
    "page.target": "Target Portfolio",
    "page.rebalance": "Rebalance",
    "page.backtest": "Backtest",
    "page.factors": "Factors",
    "page.research": "Research",
    "page.reference": "Reference",
    "page.signal": "Signals",
    "page.alerts": "Alerts",
    "page.notifications": "Notifications",
    "page.webhooks": "Webhooks",
    "page.brokers": "Brokers",
    "page.accounts": "Accounts",
    "page.sysstatus": "System",
    "page.audit": "Audit",
    "page.reconcile": "Reconcile",
    "page.settings": "Settings",
    "group.dashboard": "Overview",
    "group.market": "Market",
    "group.trade": "Trading",
    "group.strategy": "Strategy",
    "group.analysis": "Analysis",
    "group.system": "System",
    "group.other": "Other",
    "menu.系统": "System",
    "menu.行情": "Market",
    "menu.分析": "Analysis",
    "menu.交易": "Trading",
    "menu.策略": "Strategy",
    "menu.研究": "Research",
    "menu.信号": "Signals",
    "menu.账户": "Accounts",
    "menu.运维": "Ops",
    "page.accounts.title": "Accounts Grid",
    "page.audit.title": "Audit Log",
    "page.brokers.title": "Broker Connections",
    "page.dashboard.title": "Dashboard",
    "page.limitup.title": "Limit Up Monitor",
    "page.quote.title": "Market Analysis",
    "page.markettools.title": "Market Tools",
    "page.notifications.title": "Notification Channels",
    "page.rebalance.title": "Rebalance",
    "page.reference.title": "Reference Data",
    "page.settings.title": "Settings",
    "page.strategies.title": "Strategy Templates",
    "page.trade.title": "Manual Trading",
    "tree.总览": "Overview",
    "tree.行情": "Market",
    "tree.交易": "Trading",
    "tree.策略与组合": "Strategy",
    "tree.研究": "Research",
    "tree.信号": "Signals",
    "tree.系统与运维": "System",
    "tree.账户": "Accounts",
  },
};

// 惰性读取初始 locale（node 测试环境无 localStorage）
let _locale = "zh";
try {
  _locale = (typeof localStorage !== "undefined" && localStorage.getItem("qmt_work.locale")) || "zh";
} catch { /* noop */ }

export function setLocale(locale) {
  _locale = locale && _dicts[locale] ? locale : "zh";
  try { if (typeof localStorage !== "undefined") localStorage.setItem("qmt_work.locale", _locale); } catch { /* noop */ }
}

export function getLocale() {
  return _locale;
}

export function registerDict(locale, entries) {
  _dicts[locale] = { ...(_dicts[locale] || {}), ...entries };
}

export function t(key, params) {
  const dict = _dicts[_locale] || _dicts.zh || {};
  let s = dict[key] != null ? dict[key] : key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = String(s).replace(new RegExp(`\\{${k}\\}`, "g"), String(v));
    }
  }
  return s;
}

// React 组件便捷别名：import { _t } 与 t() 同义（避免与组件内局部 t 变量冲突）
export const _t = t;
