// 交易 API 统一封装：字段名/幂等键/预检/错误归一化。
// 后端约定：POST /trade/order { code, direction, volume, price, price_type, ... }
// 前端统一在 send() 内部补齐 direction / 校验 / 幂等键。

import { api } from "../api.js";

/* ============== 字段规整 ============== */
// 兼容旧字段 side → direction，volume 强制整数（手），price 保留 2 位小数
function normalizeOrderPayload(p) {
  if (!p || typeof p !== "object") throw new Error("委托参数为空");
  const code = String(p.code || "").trim().toUpperCase();
  if (!code) throw new Error("代码不能为空");
  const direction = (p.direction || p.side || "buy").toString().toLowerCase();
  if (direction !== "buy" && direction !== "sell") throw new Error("方向须为 buy / sell");
  const volume = Math.trunc(Number(p.volume) || 0);
  if (!Number.isFinite(volume) || volume <= 0) throw new Error("数量必须为正整数（手）");
  const px = Number(p.price);
  if (!Number.isFinite(px) || px <= 0) throw new Error("价格必须大于 0");
  const price = Math.round(px * 100) / 100;
  const price_type = p.price_type || "limit";
  if (!["limit", "market"].includes(price_type)) throw new Error("price_type 仅支持 limit / market");
  return {
    code,
    direction,
    volume,
    price,
    price_type,
    strategy_name: p.strategy_name || "manual",
    remark: p.remark || "",
    conn_id: p.conn_id || "",
    // 幂等：同参数 5s 内同 key 不重复下单
    idempotency_key: p.idempotency_key || "",
  };
}

/* ============== 错误归一化 ============== */
// 后端错误：{ code: !=0, message: "..." } 顶层，_req 抛 Error(message)。
// 这里做业务化映射：风控 / 余额 / 权限 / 重复单 / 网络。
export function explainTradeError(msg) {
  const s = String(msg || "");
  if (/未连接|券商客户端/.test(s)) return "未连接券商：请到「券商连接」添加并连接券商";
  if (/风控拒绝/.test(s)) return s.replace(/^.*?风控拒绝：/, "风控拦截：");
  if (/资金|余额|可用/.test(s)) return "可用资金不足：" + s;
  if (/持仓|可卖|数量/.test(s)) return "可卖持仓不足：" + s;
  if (/权限|拒绝|illegal/.test(s)) return "券商拒绝（账号/客户端权限）：" + s;
  if (/限价|涨停|跌停|price/.test(s)) return "价格越界（可能触发涨跌停）：" + s;
  return s || "委托失败";
}

/* ============== 公开 API ============== */
// 预检：先问后端风控（不真下单），用于在下单按钮前给 TDX 同款「预演」提示
// 返回 { ok, allowed, reason, risk }：
//   ok=true  且 allowed=true  → 通过
//   ok=true  且 allowed=false → 业务拒绝（携带 reason）
//   ok=false                  → 网络/字段/未连接 等系统错误
export async function precheckOrder(payload) {
  const body = normalizeOrderPayload(payload);
  try {
    const r = await api.post("/trade/precheck", body);
    // api._req 已自动 unwrap data；r 形如 { allowed, reason, risk, ... }
    if (r && r.allowed === false) {
      return { ok: true, allowed: false, reason: explainTradeError(r.reason || "风控未通过") };
    }
    return { ok: true, allowed: true, reason: "", risk: r };
  } catch (e) {
    return { ok: false, allowed: false, reason: explainTradeError(e?.message || String(e)) };
  }
}

// 下单：自动归一化字段 + 幂等键
export async function sendOrder(payload) {
  const body = normalizeOrderPayload(payload);
  // 没传幂等键时按内容生成一个，5s 内同 key 不重复
  if (!body.idempotency_key) {
    body.idempotency_key = `qt:${body.code}:${body.direction}:${body.volume}:${body.price}:${body.price_type}:${Math.floor(Date.now() / 1000 / 5)}`;
  }
  try {
    const r = await api.tradeOrder(body);
    return r;
  } catch (e) {
    const reason = explainTradeError(e?.message || String(e));
    const err = new Error(reason); err.cause = e; throw err;
  }
}

// 撤单
export async function cancelOrder(orderId) {
  if (!orderId) throw new Error("order_id 必填");
  try {
    return await api.tradeCancel(orderId);
  } catch (e) {
    const err = new Error(explainTradeError(e?.message || String(e))); err.cause = e; throw err;
  }
}
