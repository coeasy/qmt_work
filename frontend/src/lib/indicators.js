// 统一指标引擎前端消费层（G2-2/G2-3）。
//
// 单一真源：指标计算只存在于后端（app/indicators，契约与 MarketData.jsx 逐位一致），
// 前端通过 /market/indicators/calc 消费，**不再自带指标实现**（R2 断崖收敛）。
// 带内存缓存：同 (name,code,period,count,adj,params) 组合只请求一次，切页/切指标
// 秒回；后端不可用（503）返回 null，调用方降级为「不渲染该指标叠加层」（绝不本地
// 偷偷重算——那样会重新制造双份实现）。
import { api } from "../api.js";

const _cache = new Map();

function _key(name, params) {
  return `${name}:${JSON.stringify(params)}`;
}

/**
 * 取后端指标计算结果（输出列与 K 线对齐，窗口不足处为 null）。
 * @param {string} name 指标名：ma/ema/macd/kdj/rsi/boll/wr
 * @param {{code:string, period?:string, count?:number, adj?:string, [k:string]:any}} opts
 *        code 必填；win/n/m 等指标参数透传
 * @returns {Promise<object|null>} outputs 字典（如 {k,d,j} / {dif,dea,bar}），失败返回 null
 */
export async function fetchIndicator(name, opts = {}) {
  const { code, period = "1d", count = 250, adj = "", source = "auto", ...params } = opts;
  const key = _key(name, { code, period, count, adj, ...params });
  if (_cache.has(key)) return _cache.get(key);
  let out = null;
  try {
    const data = await api.marketIndicatorsCalc({ name, code, period, count, adj, source, ...params });
    if (data && data.outputs) out = data.outputs;
  } catch (e) {
    out = null; // 后端不可用 → 不渲染该叠加层，不本地重算
  }
  _cache.set(key, out);
  return out;
}

/**
 * 批量取多个指标（并行 + 结果按名索引）。
 * @returns {Promise<Record<string, object|null>>} key 与 fetchIndicator 缓存键一致
 */
export async function fetchIndicators(list) {
  const rows = await Promise.all(list.map(([name, params]) =>
    fetchIndicator(name, params).then((o) => [name, params, o])));
  const map = {};
  rows.forEach(([name, params, o]) => { map[indicatorKey(name, params)] = o; });
  return map;
}

// T9 修复：indicatorKey 与 fetchIndicator 内部缓存键归一化（默认 period/count/adj），
// 否则 fetchIndicators 的 map 键与 fetchIndicator 缓存键不一致 → 同数据两套键。
export function indicatorKey(name, opts = {}) {
  const { code, period = "1d", count = 250, adj = "", ...params } = opts;
  return _key(name, { code, period, count, adj, ...params });
}

// 仅供测试：清空模块级缓存（vitest 隔离）
export function _resetIndicatorCache() {
  _cache.clear();
}
