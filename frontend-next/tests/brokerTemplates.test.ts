import { describe, expect, it } from "vitest";
import { QUICK_TEMPLATES } from "@/domains/system/Brokers";

/**
 * 「券商连接」页快速模板的回归锁。
 *
 * 为什么必须钉死：
 * 新用户第一次打开「券商连接」页时，「接入模式」下拉里 4 个选项（direct /
 * bridgeFile / bridgeRedis / bridgeZmq）语义差异极大 —— direct 是给「未封禁的
 * xtquant 直连」用的、bridgeFile 是「大 QMT 桥接路径 B」首选、bridgeRedis 和
 * bridgeZmq 需要用户额外装 Redis/pyzmq。
 * 选错的话，界面不会立刻报错，而是用户填完参数点「添加连接」后拿到一个「连接失败
 * 但没有根因」的空诊断（因为选错模式的连接从一开始就注定连不上）。
 *
 * 模板把这一决策外化成按钮：点一下就知道该填哪几个字段。本测试保证这些按钮
 * 不会被后续改动悄悄删掉、accessMode 不跑偏、hint 不含空串（空 hint 会让 tooltip
 * 变空白气泡，用户失去指引）。
 */
describe("QUICK_TEMPLATES · 大 QMT 桥 / 小 QMT 极速版 / 大 QMT 直连", () => {
  it("覆盖三种典型接入场景（不多不少）", () => {
    const labels = QUICK_TEMPLATES.map((t) => t.label);
    expect(labels).toHaveLength(3);
    expect(labels.some((l) => l.includes("大 QMT 桥接"))).toBe(true);
    expect(labels.some((l) => l.includes("小 QMT"))).toBe(true);
    expect(labels.some((l) => l.includes("大 QMT 直连"))).toBe(true);
  });

  it("id 唯一（避免 key 冲突导致 React 渲染抖动）", () => {
    const ids = QUICK_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("模板 id 与 label 语义一致（大 QMT 桥接 = bridgeFile，其余 = direct）", () => {
    for (const t of QUICK_TEMPLATES) {
      if (t.id === "qmt-big-bridge") {
        expect(t.accessMode).toBe("bridgeFile");
      } else {
        expect(t.accessMode).toBe("direct");
      }
    }
  });

  it("每个模板 hint 非空且含关键词（否则用户看不到该填什么）", () => {
    for (const t of QUICK_TEMPLATES) {
      expect(t.hint.trim().length, `模板 ${t.id} 的 hint 是空串`).toBeGreaterThan(5);
      // 大 QMT 桥接 hint 必须提到 deploy 脚本 —— 用户点错模式后能自愈
      if (t.id === "qmt-big-bridge") {
        expect(t.hint).toMatch(/deploy|部署|桥/);
      }
      // 两个 direct 模板 hint 必须提到「路径」或「userdata」 —— 提示用户下一步填什么
      if (t.accessMode === "direct") {
        expect(t.hint).toMatch(/路径|userdata|xtquant/);
      }
    }
  });

  it("大 QMT 桥接 hint 必须点出「推荐」或「首选」—— 这是新用户的第一选择", () => {
    const t = QUICK_TEMPLATES.find((x) => x.id === "qmt-big-bridge");
    expect(t).toBeDefined();
    if (t) {
      expect(t.label).toMatch(/推荐|首选|默认/);
    }
  });

  it("accessMode 只取合法枚举值（避免以后加新接入模式时拼错）", () => {
    const valid = new Set(["direct", "bridgeFile", "bridgeRedis", "bridgeZmq"]);
    for (const t of QUICK_TEMPLATES) {
      expect(valid.has(t.accessMode), `accessMode=${t.accessMode}`.padEnd(12)).toBe(true);
    }
  });
});
