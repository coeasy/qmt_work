import { describe, expect, it, vi, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MODE_LABEL, modeSourceLabel } from "@/domains/system/Brokers";

/**
 * 「大 / 小 QMT 双模式」前后端接线回归锁（2026-10-09）。
 *
 * 背景：真机上「大客户端 + 独立行情」并存的场景暴露了三类问题，其中一类
 * **纯属前端没接线**：
 *   - 后端早就有 `POST /brokers/launch`（full / mini / quote 三模式，按模式分别
 *     判定「是否已在运行」），但前端 `brokerApi` 里**根本没有这个方法**，
 *     界面上也没有任何入口 ⇒ 「启动大 QMT 就用大模式、启动小 QMT 就用小模式」
 *     这条要求在界面上无法落地；
 *   - 候选列表只显示一个裸模式字符串，看不出模式是**按运行进程实测**出来的
 *     还是**按目录猜**的（两者外观一模一样，用户无法验收）。
 *
 * 本文件把这两条接线钉死：删掉按钮 / 改错路径 / 把依据标签去掉都会红。
 */

const SRC = resolve(__dirname, "..", "src");
const BROKERS_TSX = readFileSync(resolve(SRC, "domains/system/Brokers.tsx"), "utf8");
const BROKER_TS = readFileSync(resolve(SRC, "services/api/broker.ts"), "utf8");

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("模式文案 · 与后端 _MODE_LABEL 同源", () => {
  it("三种可启动模式都有中文名（缺一个就会出现裸英文 mode）", () => {
    for (const m of ["full", "mini", "quote"]) {
      expect(MODE_LABEL[m], `模式 ${m} 没有中文名`).toBeTruthy();
    }
  });

  it("大 / 小 QMT 的名字里必须出现「大 QMT」/「小 QMT」", () => {
    expect(MODE_LABEL.full).toContain("大 QMT");
    expect(MODE_LABEL.mini).toContain("小 QMT");
  });
});

describe("模式依据标签 · 让「实测」与「推测」可区分", () => {
  it("process → 绿色「按运行进程判定」", () => {
    const r = modeSourceLabel("process");
    expect(r.text).toContain("运行进程");
    expect(r.tone).toBe("success");
  });

  it("其余（layout / undefined）→ 中性「按目录布局推断」", () => {
    for (const v of ["layout", undefined, "", "whatever"]) {
      const r = modeSourceLabel(v);
      expect(r.text).toContain("目录");
      expect(r.tone).toBe("neutral");
    }
  });
});

describe("brokers/launch 接线", () => {
  it("API 层暴露 launchClient 且打到 /brokers/launch", () => {
    expect(BROKER_TS).toContain("/brokers/launch");
    expect(BROKER_TS).toMatch(/launchClient\s*:/);
  });

  it("launchClient 用 POST 且 body 带 mode（缺 mode 后端会退化成 full）", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve(
          new Response(
            JSON.stringify({
              ok: true,
              data: { launched: true, already_running: false, exe: "X.exe", hint: "h" },
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }) as never,
    );
    const { brokerApi } = await import("@/services/api/broker");
    await brokerApi.launchClient("P:/stock/gd_qmt/userdata", "mini");
    expect(calls.length).toBe(1);
    expect(calls[0]?.url).toContain("/brokers/launch");
    expect(calls[0]?.init?.method).toBe("POST");
    const body = JSON.parse(String(calls[0]?.init?.body ?? "{}"));
    expect(body.mode).toBe("mini");
    expect(body.client_path).toBe("P:/stock/gd_qmt/userdata");
  });
});

describe("候选列表 UI · 三个启动入口 + 模式依据", () => {
  it("每个候选都有「启动大 QMT / 启动小 QMT / 仅补行情」三个入口", () => {
    expect(BROKERS_TSX).toContain("启动大 QMT");
    expect(BROKERS_TSX).toContain("启动小 QMT");
    expect(BROKERS_TSX).toContain("仅补行情");
  });

  it("三个入口各自传对应的 mode（不能都传同一个，否则按钮名与行为不符）", () => {
    expect(BROKERS_TSX).toMatch(/onLaunch\(c,\s*"full"\)/);
    expect(BROKERS_TSX).toMatch(/onLaunch\(c,\s*"mini"\)/);
    expect(BROKERS_TSX).toMatch(/onLaunch\(c,\s*"quote"\)/);
  });

  it("候选行渲染模式依据 Badge（否则用户分不清实测与推测）", () => {
    expect(BROKERS_TSX).toContain("modeSourceLabel(");
  });

  it("启动结果必须三分支呈现：已在运行 / 已启动 / 未启动", () => {
    // 后端返回三种语义，界面合并任意两者都是失真
    expect(BROKERS_TSX).toContain("already_running");
    expect(BROKERS_TSX).toMatch(/未启动|r\.launched/);
    expect(BROKERS_TSX).toContain("已在运行");
    expect(BROKERS_TSX).toContain("启动失败");
  });

  it("启动按钮在请求期间禁用（GUI 冷启动数秒，连点会拉起多个实例）", () => {
    expect(BROKERS_TSX).toMatch(/launchingKey/);
    expect(BROKERS_TSX).toMatch(/disabled=\{!!launchingKey\}/);
  });
});

describe("候选类型契约 · mode_source 必须被声明", () => {
  it("AutoDetectCandidate 声明 mode_source（后端已返回，前端不声明就取不到）", () => {
    expect(BROKER_TS).toMatch(/mode_source\?/);
  });
});
