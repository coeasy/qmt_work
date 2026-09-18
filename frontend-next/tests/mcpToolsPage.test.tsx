import { describe, expect, it, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";

/**
 * 「系统 → MCP 工具」页回归测试。
 *
 * 锁住三件事：
 *  ① **可发现性**：116 个工具必须真的显示出来（此前界面根本看不到 MCP 存在）；
 *  ② **零 mock**：自省失败必须显示错误 + 重试，**绝不**用示例数据冒充工具清单，
 *     也不能显示成「0 个工具」这种看起来正常的假象；
 *  ③ 名称/分组过滤要真的过滤，且计数随之更新。
 *
 * ★ 为什么要测：后端 `GET /capabilities/mcp` 已自测通过，但「接口对」不等于
 * 「页面对」—— 这个项目的历史教训就是测试替身与前端预期一致，掩盖了真实差异。
 */

const TOOLS = [
  "account_status", "algo_cancel", "algo_list", "place_order",
  "get_market_boards", "get_market_etfs", "get_quote_snapshot",
];

const PAYLOAD = {
  tools: TOOLS,
  count: TOOLS.length,
  by_prefix: { account: 1, algo: 2, place: 1, get: 3 },
  endpoint: "/mcp",
  transport: "streamable-http",
  auth: "admin scope 或主密钥",
  bind: "127.0.0.1",
  local_only_note: "",
};

// systemApi 是本页唯一数据源，直接打桩它（不打桩 http，避免受全局 fetch 影响）
vi.mock("@/services/api", () => ({
  systemApi: {
    capabilitiesMcp: vi.fn(),
  },
}));

import { systemApi } from "@/services/api";
import { McpTools } from "@/domains/system/McpTools";

const mock = systemApi.capabilitiesMcp as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("McpTools · MCP 工具浏览页", () => {
  it("① 显示真实工具数与接入端点", async () => {
    mock.mockResolvedValue(PAYLOAD);
    render(<McpTools />);
    await waitFor(() => expect(screen.getByText("7")).toBeTruthy());
    expect(screen.getByText("/mcp")).toBeTruthy();
    // 传输方式在「统计卡」与「接入说明」两处都出现，用 getAllByText
    expect(screen.getAllByText("streamable-http").length).toBeGreaterThan(0);
  });

  it("② 自省失败显示错误 + 重试，不伪造清单", async () => {
    mock.mockRejectedValue(new Error("MCP 服务不可用"));
    render(<McpTools />);
    await waitFor(() => expect(screen.getByText(/MCP 工具清单不可用/)).toBeTruthy());
    expect(screen.getByText(/MCP 服务不可用/)).toBeTruthy();
    // 关键：不能出现「0 个工具」这种看起来正常的假象
    expect(screen.queryByText("工具总数")).toBeNull();
    expect(screen.getByText("重试")).toBeTruthy();
  });

  it("③ 按名称过滤生效且计数随之更新", async () => {
    mock.mockResolvedValue(PAYLOAD);
    render(<McpTools />);
    await waitFor(() => expect(screen.getByText("工具清单（7/7）")).toBeTruthy());

    const input = screen.getByPlaceholderText("按名称过滤…");
    fireEvent.change(input, { target: { value: "algo" } });
    await waitFor(() => expect(screen.getByText("工具清单（2/7）")).toBeTruthy());

    // 过滤后表格里不应再出现非匹配项（虚拟列表在 jsdom 下高度为 0，
    // 故只断言标题计数与输入框状态，不断言行 DOM）
    expect((input as HTMLInputElement).value).toBe("algo");
  });

  it("④ 过滤无结果时给出明确文案，而不是空表", async () => {
    mock.mockResolvedValue(PAYLOAD);
    render(<McpTools />);
    await waitFor(() => expect(screen.getByText("工具清单（7/7）")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText("按名称过滤…"), {
      target: { value: "zzz-不存在" },
    });
    await waitFor(() => expect(screen.getByText("工具清单（0/7）")).toBeTruthy());
  });
});
