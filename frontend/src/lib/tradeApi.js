// 交易 API 统一封装：字段名/幂等键/预检/错误归一化。
// 后端约定：POST /trade/order { code, direction, volume, price, price_type, ... }
// 前端统一在 send() 内部补齐 direction / 校验 / 幂等键。

import { api } from "../api.js";

/* ============== 字段规整 ============== */
// 兼容旧字段 side → direction，volume 强制整数（手），price 保留 2 位小数
export function normalizeOrderPayload(p) {
  if (!p || typeof p !== "object") throw new Error("委托参数为空");
  const code = String(p.code || "").trim().toUpperCase();
  if (!code) throw new Error("代码不能为空");
  const direction = (p.direction || p.side || "buy").toString().toLowerCase();
  if (direction !== "buy" && direction !== "sell") throw new Error("方向须为 buy / sell");
  const volume = Math.trunc(Number(p.volume) || 0);
  if (!Number.isFinite(volume) || volume <= 0) throw new Error("数量必须为正整数（手）");
  const px = Number(p.price);
  const price_type = p.price_type || "limit";
  if (!["limit", "market"].includes(price_type)) throw new Error("price_type 仅支持 limit / market");
  // 市价单放行 price=0（后端风控用最新价估算，gateway/risk.py）；仅限价单强制 >0。
  // 此前一刀切导致 Trade 页选「市价」必然被前端拒绝（比后端更严的误伤）。
  if (price_type === "limit" && (!Number.isFinite(px) || px <= 0)) throw new Error("限价单价格必须大于 0");
    // 价格精度按标的适配：ETF/债券（51/56/58/15/16 段）tick 为 0.001，股票 0.01
  const tickDecimals = ["51", "56", "58", "15", "16"].includes(code.slice(0, 2)) ? 3 : 2;
  const price = Number.isFinite(px) && px > 0 ? Number(px.toFixed(tickDecimals)) : 0;
  // A 股整手校验（前端先给出友好文案，避免等后端 400）
  if (volume % 100 !== 0) throw new Error("数量须为 100 的整数倍（A股一手=100股）");
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
  if (/未连接|券商客户端|handshake|not connected|disconnect/i.test(s)) return "未连接券商：请到「券商连接」添加并连接券商";
  if (/风控拒绝/.test(s)) return s.replace(/^.*?风控拒绝：/, "风控拦截：");
  if (/资金|余额|可用|insufficient/i.test(s)) return "可用资金不足：" + s;
  if (/持仓|可卖|数量|position/i.test(s)) return "可卖持仓不足：" + s;
  if (/权限|拒绝|illegal|permission/i.test(s)) return "券商拒绝（账号/客户端权限）：" + s;
  if (/限价|涨停|跌停|price|limit/i.test(s)) return "价格越界（可能触发涨跌停）：" + s;
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
  // 幂等键策略：显式传入的 idempotency_key 原样透传；未传时不自生成 ——
  // 此前按「内容 + 5s 时间桶」生成，桶边界（4.9s/5.1s）两侧会得到不同键，
  // 反而覆盖掉后端 single_flight 的滚动窗口内容哈希去重（app/routes/trade.py，
  // 同参数 5s 内只下一单，无桶边界问题）。交给后端兜底更稳。
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
