import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

/**
 * 「自选股只在左侧常驻展示」—— 第 17 轮需求 ③ 的归属锁。
 *
 * ## 背景（为什么会重复）
 * 自选股一度有**两处**入口：
 *   ① 左侧数据面板 `shell/DataPanel`（默认展开，默认就是「自选股」页签，全站可见）；
 *   ② 行情工作台右栏嵌了一份 `<QuoteBoard compact .../>`（五档盘口下方）。
 * 同一份列表在一屏里出现两次的代价：
 *   - 占掉右栏近 200px 竖向空间，把成交流与快捷下单往下顶；
 *   - 两处各自维护「当前标的」的视觉状态，改一处另一处不跟随 ⇒ 像两个不同的列表；
 *   - `QuoteBoard` 为「嵌在窄栏里当切换器」这一个调用方多了 5 个开关
 *     （`onPick` / `active` / `compact` / `autoShrink` / `maxBodyHeight`），
 *     该调用方一撤，它们就成了**永远走不到的分支**。
 *
 * ## 为什么是源码扫描而不是点击
 * 右栏「没有自选股」是**结构性事实**（不引用该组件），渲染断言反而会被
 * jsdom 虚拟表格「一行不渲染」的假阴性骗过去（见 DataTable 的教训）。
 * 这些断言都**可证伪**：把 `<QuoteBoard compact .../>` 加回工作台、
 * 或把某个开关加回 `QuoteBoard`，对应用例立刻失败。
 */

const SRC = path.resolve(__dirname, "../src");

const read = (rel: string) => fs.readFileSync(path.join(SRC, rel), "utf8");

/**
 * 去掉注释后再扫。
 *
 * ⚠️ 必须去注释：`QuoteBoard` 的文档里**刻意写明了**「早先有 onPick / compact / … 五个
 *   开关、现已移除」—— 直接全文 `toContain` 会把这段说明判成「开关还在」，
 *   于是这个守卫生效不了（**注释里的历史不该被当成代码**）。
 */
const code = (rel: string) =>
  read(rel)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/[^\n]*/g, "$1");

describe("自选股的唯一常驻位置 = 左侧数据面板", () => {
  it("行情工作台不再内嵌报价牌 —— 右栏不重复一份自选股", () => {
    const src = read("domains/market/MarketWorkbench.tsx");
    expect(src, "行情工作台又引用了 QuoteBoard（右栏会重复展示自选股）").not.toContain(
      "QuoteBoard",
    );
  });

  it("左侧数据面板确有自选股页签，且默认展开、默认就停在自选股", () => {
    const src = read("shell/DataPanel.tsx");
    expect(src, "DataPanel 没有自选股页签").toContain(`key: "watchlist"`);
    expect(src, "DataPanel 自选股点行没接 useOpenWorkbench").toContain("useOpenWorkbench");
    // 默认值在 ui store 里（DataPanel 只是消费者），这里同步锁住
    const ui = read("stores/ui.ts");
    expect(ui, "数据面板默认不是展开").toContain("dataPanelOpen: true");
    expect(ui, "数据面板默认页签不是自选股").toContain('dataPanelTab: "watchlist"');
  });

  it("报价牌的「窄栏 / 内嵌切换」开关已清干净 —— 不留走不到的分支", () => {
    const src = code("domains/market/QuoteBoard.tsx");
    for (const dead of ["onPick", "autoShrink", "maxBodyHeight", "compact", "active"]) {
      expect(src, `QuoteBoard 仍有已无调用方的开关 ${dead}`).not.toContain(dead);
    }
  });

  it("报价牌点行仍然只走 useOpenWorkbench 这一个出口", () => {
    const src = read("domains/market/QuoteBoard.tsx");
    expect(src).toContain("useOpenWorkbench");
    expect(src, "QuoteBoard 又自己拼 open() 了").not.toContain('open("workbench"');
  });
});
