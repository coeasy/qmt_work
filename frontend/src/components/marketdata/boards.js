// 板块深链导航（自 MarketData.jsx 原样拆出）。
// 深链板块页（F3 联动）：先经 /market/board/lookup 把名称解析为确切板块代码，
// 再带 code 跳转（后端板块榜按 code 精确选中），避免 name.includes 误匹配 /
// 同名板块命中错项 / 榜单未加载时静默失败（P1-8）。
import { api } from "../../api.js";
import { navTo } from "../../lib/nav.js";

export async function toBoards(nm) {
  try {
    const r = await api.marketBoardLookup({ name: nm, limit: 1 });
    const m = (r && r.matches && r.matches[0]) || null;
    if (m && m.code) {
      navTo("boards", { params: { code: m.code, name: m.name || nm } });
      return;
    }
  } catch { /* 降级：按名称深链 */ }
  navTo("boards", { params: { name: nm } });
}
