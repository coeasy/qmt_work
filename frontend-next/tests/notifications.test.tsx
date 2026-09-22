import { describe, expect, it, vi, beforeEach, beforeAll, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { NotificationConfig } from "@/services/api";

/**
 * 「通知渠道」页的契约回归 —— 锁的是**「保存成功但永远发不出去」**这一条。
 *
 * 后端保存渠道时**不校验参数完整性**：只填名字也能存下来，界面显示「启用」，
 * 于是告警链路在一个看起来完全正常的配置页上静默失效，发送记录里只留一句
 * `webhook url missing`。本项目实测里就躺着 2 条这样的僵尸渠道
 * （name 为空、params 为空、enabled=1）。
 *
 * 所以这里锁的是「缺什么必须看得见」，而不是表格长什么样。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    systemApi: {
      ...actual.systemApi,
      notifications: vi.fn(),
      notificationLogs: vi.fn(),
      batchDeleteNotifications: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import { Notifications } from "@/domains/automation/Notifications";
// eslint-disable-next-line import/first
import { systemApi } from "@/services/api";

const api = systemApi as unknown as {
  notifications: ReturnType<typeof vi.fn>;
  notificationLogs: ReturnType<typeof vi.fn>;
  batchDeleteNotifications: ReturnType<typeof vi.fn>;
};

/**
 * ⚠️ `DataTable` 走 `@tanstack/react-virtual`，而 **jsdom 里元素高度恒为 0**
 * ⇒ 可视区间为空 ⇒ **一行都不会渲染**（行内角标自然也找不到）。
 * 不补这个桩，下面的断言会以「文本被拆成多个元素」这种误导性报错失败。
 */
beforeAll(() => {
  for (const k of ["offsetHeight", "clientHeight"]) {
    Object.defineProperty(HTMLElement.prototype, k, { configurable: true, value: 600 });
  }
  Element.prototype.getBoundingClientRect = () =>
    ({ width: 1200, height: 600, top: 0, left: 0, bottom: 600, right: 1200,
       x: 0, y: 0, toJSON() {} }) as DOMRect;
});

/** 照抄实测里那条僵尸渠道的真实形状（GET /notifications 原样返回）。 */
const zombie: NotificationConfig[] = [
  { id: 1, name: "", channel: "webhook", enabled: 1, events: "*", params: {} },
  { id: 2, name: "", channel: "webhook", enabled: 1, events: "*", params: {} },
];

const healthy: NotificationConfig[] = [
  { id: 1, name: "风控告警", channel: "webhook", enabled: 1, events: "*",
    params: { url: "https://example.com/hook" } },
];

beforeEach(() => {
  api.notifications.mockResolvedValue(zombie);
  api.notificationLogs.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("通知渠道页", () => {
  it("缺必填参数的渠道：顶部计数 + 行内点名缺哪个", async () => {
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("2 个渠道配置不完整")).toBeTruthy());
    // ★ 只喊「不完整」等于没说：用户还得去猜到底少填了什么
    expect(screen.getAllByText("缺 URL")).toHaveLength(2);
  });

  it("配置完整的渠道不报警（不能把正常渠道标红）", async () => {
    api.notifications.mockResolvedValue(healthy);
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("完整")).toBeTruthy());
    expect(screen.queryByText(/配置不完整/)).toBeNull();
  });

  it("渠道名为空时显示「（未命名）」而不是一片空白", async () => {
    render(<Notifications />);
    // 空白格会被当成「还没渲染出来」，排查时非常误导
    await waitFor(() => expect(screen.getAllByText("（未命名）")).toHaveLength(2));
  });

  it("邮件渠道缺 SMTP 参数同样要报（按通道判，不是只查 url）", async () => {
    api.notifications.mockResolvedValue([
      { id: 3, name: "邮件", channel: "email", enabled: 1, events: "*",
        params: { host: "smtp.example.com", port: 587 } },
    ]);
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("1 个渠道配置不完整")).toBeTruthy());
    // ★ 必填项按通道取：host/port 填了，剩下三项仍要逐个点名
    expect(screen.getByText("缺 账号、密码 / 授权码、收件人")).toBeTruthy();
  });

  it("接口不可用时给出失败原因，而不是渲染成空列表", async () => {
    api.notifications.mockRejectedValue(new Error("后端未启动"));
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText(/通知渠道加载失败：后端未启动/)).toBeTruthy());
  });
});

describe("通知渠道页 · 清理不完整渠道", () => {
  it("提供一键清理，且只在确实有残缺渠道时出现", async () => {
    const { fireEvent } = await import("@testing-library/react");
    api.batchDeleteNotifications.mockResolvedValue({ deleted: 2 });
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("2 个渠道配置不完整")).toBeTruthy());
    fireEvent.click(screen.getByText("清理不完整"));
    await waitFor(() => expect(screen.getByText("确认清理")).toBeTruthy());
    fireEvent.click(screen.getByText("确认清理"));
    await waitFor(() => expect(screen.getByText(/已清理 2 个配置不完整的渠道/)).toBeTruthy());
    // ★ 删的必须是**残缺的那几条 id**，不能顺手把正常渠道也带走
    expect(api.batchDeleteNotifications).toHaveBeenCalledWith([1, 2]);
  });

  it("全部渠道都完整时，不应出现清理入口", async () => {
    api.notifications.mockResolvedValue(healthy);
    render(<Notifications />);
    await waitFor(() => expect(screen.getByText("完整")).toBeTruthy());
    expect(screen.queryByText("清理不完整")).toBeNull();
  });
});
