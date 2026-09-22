import { useCallback } from "react";
import { useWorkspaceStore } from "@/stores/workspace";
import { normalizeCode } from "@/shared/format";

/**
 * 「点一只股票 ⇒ 打开**行情工作台**」的**唯一出口**（2026-09-20 第 17 轮）。
 *
 * ## 为什么要抽这一层
 *
 * 需求是「支持在**各个列表**中点击股票自动跳转到股票工作台」。列表不止一个
 * （自选股 / 报价牌 / 选股结果 / 涨停监控 / ETF / 持仓 / 委托 / 成交 / 侧栏自选…），
 * 每个都自己写一遍 `open("workbench", {...})` 就会出现：
 * - 有的传 `{ code, name }`、有的只传 `{ code }` ⇒ 打开后标题/名称不一致；
 * - 有的写 `"quote"`（只开 K 线页）而不是 `"workbench"` ⇒ 用户点一只票只看到 K 线；
 * - 忘了 `normalizeCode` ⇒ 裸代码 `600519` 到后端查不到。
 *
 * 抽成 hook 后调用方只有一种写法，想漏也漏不掉（与 `useLiveQuotes` 同一套路）。
 *
 * ## 标题为什么可以先写成代码
 * 点进来时名称常常还没到位（WS 未推 / REST 兜底未回）。标题在 `open()` 那一刻定死，
 * 所以这里先给 `name || code`；名称到位后由 `MarketWorkbench` 调 `renameTab` 改成真名
 * —— 改名机制见 `stores/workspace.ts::renameTab`。
 */
export function useOpenWorkbench(): (code: string, name?: string) => void {
  const open = useWorkspaceStore((st) => st.open);
  return useCallback(
    (code: string, name?: string) => {
      const c = normalizeCode(code);
      if (!c) return; // 空代码不开页（避免开出一个「无标的」的空工作台）
      const nm = String(name ?? "").trim();
      open("workbench", { code: c, name: nm }, { title: nm || c });
    },
    [open],
  );
}
