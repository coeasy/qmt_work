// G4 统一数据面：topic 总线（subscribe/peek + 策略表驱动的缓存/节流/合并）。
//
// 单一真源：策略表由后端 /datahub/policies 下发（杜绝前后端漂移）。
// 语义：
//   subscribe(topic, fetcher, cb)  —— 订阅数据流：
//     1) 快照新鲜（ttl 内）→ 立即回调 {data, stale:false}，不发网络请求；
//     2) 最小请求间隔内 → 回调陈旧快照 {data, stale:true}（stale_ok 时）；
//     3) 否则合并：同一 topic 在 coalesce 窗口内的多个订阅共享**一次**请求。
//   peek(topic) —— 同步读最后一次快照（无则 null）。
//   invalidate(topic) —— 强制下次刷新（用户手动刷新）。
import { api } from "../api.js";

const _policies = {
  default: { ttl_ms: 30000, min_interval_ms: 10000, coalesce_within_ms: 2000,
             priority: "medium", stale_ok: true },
  topics: {},
};
const _snapshots = new Map();   // topic -> {data, ts}
const _lastReq = new Map();     // topic -> ts
const _coalesce = new Map();    // topic -> {cbs, timer}
let _policiesLoaded = null;

export async function loadPolicies() {
  if (_policiesLoaded) return _policiesLoaded;
  _policiesLoaded = api.dataHubPolicies()
    .then((d) => { Object.assign(_policies, d || {}); return _policies; })
    .catch(() => _policies);
  return _policiesLoaded;
}

export function policyOf(topic) {
  const topics = _policies.topics || {};
  let best = null;
  for (const pre of Object.keys(topics)) {
    if (topic.startsWith(pre) && (!best || pre.length > best.length)) best = pre;
  }
  return best ? topics[best] : _policies.default;
}

export function peek(topic) {
  const s = _snapshots.get(topic);
  return s ? { data: s.data, as_of: s.ts } : null;
}

export function invalidate(topic) {
  _snapshots.delete(topic);
}

export function subscribe(topic, fetcher, cb) {
  const pol = policyOf(topic);
  const now = Date.now();
  const snap = _snapshots.get(topic);
  const last = _lastReq.get(topic) || 0;
  // 1) 快照新鲜 → 立即回调
  if (snap && now - snap.ts < pol.ttl_ms) {
    cb({ data: snap.data, stale: false });
    return () => {};
  }
  // 2) 最小间隔内 → 陈旧快照（stale 明示，不触发新请求）
  if (now - last < pol.min_interval_ms) {
    if (snap && pol.stale_ok) cb({ data: snap.data, stale: true });
    return () => {};
  }
  // 3) 合并窗口：共享一次请求
  let entry = _coalesce.get(topic);
  if (!entry) {
    entry = { cbs: [], timer: null };
    entry.timer = setTimeout(() => _run(topic, fetcher, entry.cbs),
                              pol.coalesce_within_ms);
    _coalesce.set(topic, entry);
  }
  entry.cbs.push(cb);
  return () => { entry.cbs = entry.cbs.filter((c) => c !== cb); };
}

async function _run(topic, fetcher, cbs) {
  _coalesce.delete(topic);
  if (!cbs || !cbs.length) return;   // 窗口内订阅者已全部退订：不再打源
  _lastReq.set(topic, Date.now());
  let data = null;
  let error = null;
  try {
    data = await fetcher();
  } catch (e) {
    error = e;
  }
  if (!error) _snapshots.set(topic, { data, ts: Date.now() });
  cbs.forEach((c) => c(error ? { data: null, stale: true, error } : { data, stale: false }));
}

export function __resetForTests() {
  _snapshots.clear(); _lastReq.clear(); _coalesce.clear();
  _policiesLoaded = null;
}
