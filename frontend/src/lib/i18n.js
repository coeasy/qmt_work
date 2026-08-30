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
