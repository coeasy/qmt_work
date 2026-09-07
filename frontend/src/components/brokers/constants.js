// 券商连接页共享常量与纯工具函数（自 Brokers.jsx 原样搬移，行为零变更）。

export const ACCOUNT_TYPES = ["STOCK", "CREDIT", "OPTION", "FUTURES"];

// 把后端/SDK 的底层报错翻译成用户可操作的指引（解决「连接异常：无法连接行情服务！」
// 「桥接子进程握手失败：None」这类信息失明问题）。
export function friendlyErr(detail) {
  if (!detail) return "连接失败，请检查券商客户端状态后重试。";
  const d = String(detail);
  // 后端已给出分步排查指引（行情/交易连接失败均如此）时直接透出，
  // 不再叠加前端提示——否则同一条建议出现两遍，反而降低可读性。
  if (d.includes("请按顺序排查") || d.includes("请按以下顺序排查") || d.includes("官方四步排查")) return d;
  if (d.includes("未登录") || d.includes("行情服务") || d.includes("无法连接行情") || d.includes("未启动"))
    return d + "\n\n→ 请先登录 QMT 客户端（极速/普通模式均可），保持客户端运行，再重试连接。";
  if (d.includes("握手失败"))
    return d + "\n\n→ 桥接子进程未在 90s 内就绪，最常见原因是 QMT 客户端未登录导致 SDK 阻塞。请登录客户端后重试；仍失败则重启客户端与桌面端。";
  if (d.includes("未配置") || d.includes("account_id"))
    return d + "\n\n→ 请在「资金账号」填写券商资金账号后再连接（行情模式可不填，但交易/持仓/下单需账号）。";
  if (d.includes("xtquant") || d.includes("SDK"))
    return d + "\n\n→ 未找到/无法加载 xtquant。请确认客户端路径指向 userdata_mini 目录且对应券商客户端已安装。";
  return d;
}

// 能力矩阵标签（与后端 QmtCapabilities 字段名对应）
const CAP_LABELS = {
  quote: "行情", kline: "K线", stock_list: "股票列表", sector: "板块",
  trading_calendar: "交易日历", trade: "交易", account: "账户",
  condition_order: "条件单", credit: "信用", option: "期权",
  futures: "期货", l2_tick: "L2逐笔", financial: "财务", realtime_push: "实时推送",
};
export function capLabel(k) { return CAP_LABELS[k] || k; }

// 将后端结构化探测诊断格式化为可读文本（sdk 定位/导入/目录线索）
export function probeText(p) {
  const lines = [];
  lines.push(`\n—— 环境诊断 ——`);
  lines.push(`Python ${p.python_version || "?"}`);
  lines.push(`目录存在：${p.client_exists ? "是" : "否"}`);
  if (p.has_bin_x64 != null) lines.push(`含 bin.x64：${p.has_bin_x64 ? "是" : "否"} · 含 userdata_mini：${p.has_userdata_mini ? "是" : "否"}`);
  lines.push(`xtquant 定位：${p.xtquant_site || "未找到"}`);
  lines.push(`xtquant 可导入：${p.xtquant_importable ? "是" : "否"}`);
  if (p.import_error) lines.push(`导入错误：${String(p.import_error).slice(0, 180)}`);
  if (p.hint) lines.push(`提示：${p.hint}`);
  return lines.join("\n");
}
