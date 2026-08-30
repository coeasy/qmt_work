// 前端共用纯函数（无副作用，便于 vitest 单测，C3/P2-3）：
// 金额/时长格式化 + 状态→文案与样式映射。组件内不再各自实现，避免口径漂移。

// 金额缩写（**全站唯一实现**）：>=1亿 → x.xx亿；>=1万 → x.xx万；否则取整。
//
// 收敛说明（终极整合方案 G0 批次）：历史上存在 3 份实现且口径互相冲突——
//   ① hooks/useMarket.js formatAmount：万档 toFixed(2)
//   ② lib/format.js     fmtAmount   ：万档 toFixed(1) ← 同一金额跨页精度不一致
//   ③ QuoteBoard.jsx    fmtAmount   ：本地副本，<1万档 String(Math.round(n))
// 三处现已全部指向本函数。
//
// 口径要点（勿回退）：
//   - 档位用**绝对值**判定：否则负数金额（如资金净流出 -1.5 亿）会漏判档位，
//     直接输出 "-150000000" 这种原始数字（② 的历史 Bug）。
//   - 亿/万档统一 2 位小数，<1万取整，与金融终端惯例一致。
export function fmtAmount(v) {
  if (v == null || isNaN(v)) return "—";
  const n = Number(v);
  const abs = Math.abs(n);
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (abs >= 1e4) return (n / 1e4).toFixed(2) + "万";
  return n.toFixed(0);
}

// 时长缩写：分钟+秒，如 "3分20秒" / "20秒"；无值返回 "—"。
export function fmtLimitDur(sec) {
  if (!sec) return "—";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
}

// 订单/算法单状态 → 展示文案 + 样式类（对齐各页面 tag 约定）。
// 支持形态：pending/running/paused/done/canceled/failed（算法单），
// 以及券商侧 submitted/filled/part_filled 等（条件单/委托）。
const STATUS_MAP = {
  pending: { label: "排队中", className: "warn" },
  running: { label: "执行中", className: "run" },
  paused: { label: "已暂停", className: "warn" },
  done: { label: "已完成", className: "ok" },
  filled: { label: "已成", className: "ok" },
  canceled: { label: "已取消", className: "fail" },
  cancelled: { label: "已取消", className: "fail" },
  failed: { label: "失败", className: "fail" },
  rejected: { label: "被拒", className: "fail" },
  submitted: { label: "已受理", className: "run" },
  active: { label: "进行中", className: "run" },
  part_filled: { label: "部成", className: "warn" },
  expired: { label: "过期", className: "fail" },
};

export function orderStatus(status) {
  return STATUS_MAP[status] || { label: String(status ?? "—"), className: "" };
}
