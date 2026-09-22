import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { PathsResponse } from "@/services/api";

/**
 * 「数据目录」面板的契约回归（P0-3 II）。
 *
 * 锁的不是布局，而是三条**错了会出真事**的区分：
 *
 * ① **主库必须标「改后需重启」**：主库路径在进程启动时固化，把它渲染成
 *    「已保存」就是最恶劣的一种「点了没反应」—— 用户以为搬好了，重启后对着空库；
 * ② **导出目录 / 冷库必须标「运行期可改」**（否则用户会白白重启一次）；
 * ③ **冷库「配置已改但没装配上」必须显式否定**：界面若只显示新路径，
 *    用户会以为历史已经在新位置，于是放心删掉旧文件。
 */
vi.mock("@/services/api", async (orig) => {
  const actual = await orig<typeof import("@/services/api")>();
  return {
    ...actual,
    pathsApi: {
      get: vi.fn(),
      validate: vi.fn(),
      set: vi.fn(),
      migrateCold: vi.fn(),
      runBackup: vi.fn(),
      pruneBackups: vi.fn(),
    },
  };
});

// eslint-disable-next-line import/first
import { DataPaths } from "@/domains/system/DataPaths";
// eslint-disable-next-line import/first
import { pathsApi } from "@/services/api";

const api = pathsApi as unknown as {
  get: ReturnType<typeof vi.fn>;
  validate: ReturnType<typeof vi.fn>;
  set: ReturnType<typeof vi.fn>;
  migrateCold: ReturnType<typeof vi.fn>;
};

function base(over: Partial<PathsResponse> = {}): PathsResponse {
  const dir = (kind: "export" | "cold" | "db", over2: Record<string, unknown> = {}) =>
    ({
      kind,
      label: kind === "export" ? "离线数据导出目录" : kind === "cold" ? "冷 K 线仓目录" : "主库目录（app.db）",
      path: `D:/qmt/${kind}`,
      file: "",
      configured: false,
      default_path: `D:/qmt/${kind}`,
      mutable: kind !== "db",
      requires_restart: kind === "db",
      note: "",
      exists: true,
      writable: true,
      inside_install: false,
      usable: true,
      reason: "",
      ...over2,
    }) as PathsResponse["export"];
  return {
    install_dir: "D:/qmt",
    config_file: "D:/qmt/qmt_work_config.json",
    export: dir("export"),
    cold: dir("cold", { rows: 0, attached_path: "", in_sync: true, candidates: [] }),
    db: dir("db"),
    ...over,
  };
}

beforeEach(() => {
  api.get.mockResolvedValue(base());
  api.validate.mockResolvedValue({ input: "", ok: true, reason: "", path: "D:/qmt/export",
    exists: true, writable: true, inside_install: false, is_system: false, is_drive_root: false });
  api.set.mockResolvedValue({ saved: true, kind: "export", path: "D:/qmt/export",
    requires_restart: false, ...base() });
  api.migrateCold.mockResolvedValue({ moved: 12, batches: 1, source: "C:/old/bars_cold.db",
    source_kept: true, ...base() });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("数据目录面板", () => {
  it("三类目录都渲染，且主库明确标注「改后需重启」", async () => {
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText("离线数据导出目录")).toBeTruthy());
    expect(screen.getByText("冷 K 线仓目录")).toBeTruthy();
    expect(screen.getByText("主库目录（app.db）")).toBeTruthy();

    // ★ 只有主库需要重启；把三者都标成「已生效」会让用户白白重启，
    //   把主库标成「已生效」则更糟（重启后对着空库）。
    const restartTags = screen.getAllByText("改后需重启");
    expect(restartTags).toHaveLength(1);
  });

  it("未自定义时显示「默认」而不是「已自定义」", async () => {
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText("离线数据导出目录")).toBeTruthy());
    // export / cold 各一个「默认」
    expect(screen.getAllByText("默认")).toHaveLength(2);
  });

  it("冷库配置与装配不一致时必须显式否定（不能只显示新路径）", async () => {
    const data = base();
    data.cold = {
      ...data.cold,
      configured: true,
      path: "D:/new",
      attached_path: "C:/old/bars_cold.db",
      in_sync: false,
    };
    api.get.mockResolvedValue(data);
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/切换未生效/)).toBeTruthy());
  });

  it("发现旧冷仓时给出「迁移」入口", async () => {
    const data = base();
    data.cold = {
      ...data.cold,
      candidates: [{ path: "C:/old/bars_cold.db", rows: 4321 }],
    };
    api.get.mockResolvedValue(data);
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/发现旧冷仓，可迁入/)).toBeTruthy());
    expect(screen.getByText(/4321 行/)).toBeTruthy();
  });

  it("接口不可用时给出失败原因，而不是渲染成「正在读取」", async () => {
    api.get.mockRejectedValue(new Error("后端未启动"));
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/数据目录加载失败：后端未启动/)).toBeTruthy());
  });
});
