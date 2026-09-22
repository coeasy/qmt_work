import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { BackupStats, PathsResponse } from "@/services/api";

/**
 * 「主库备份占用」面板的契约回归（2026-09-20 实测缺陷修复）。
 *
 * ## 背景：11GB 的隐形占用
 *
 * 备份是**整库全量复制**。1.14GB 的主库按「保留 10 份」就是 **11GB** 常驻磁盘
 * —— 实测用户数据目录就是 11GB（C: 321GB 仅剩 47GB / 已用 86%），而界面上
 * **一处都没有提**（`list_backups()` 零调用方）。用户只能等磁盘满了才发现。
 *
 * ## 锁的不是布局，而是三条**错了会出真事**的区分
 *
 * ① **「未变化、已跳过」不是失败**：`last.action === "skipped"` 表示已有备份精确
 *    覆盖当前状态，渲染成「备份失败」就是错误归因；
 * ② **超上限 ≠ 一定能回收**：预算比单份备份还小时受 `min_keep` 地板保护
 *    （`reclaimable_bytes === 0`），此时说「可回收 X」是假承诺，
 *    说「一切正常」又掩盖了磁盘正在被吃；
 * ③ **未纳管的库副本必须点名**：它们不在保留策略内、也不会被本页删除，
 *    不显示就永远查不出「磁盘去哪了」（实测真实目录里有一个 603MB 的
 *    `app.db.bak_bardate`）。
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
import {
  DataPaths,
  backupLastText,
  backupOccupancyText,
  backupStrayText,
  backupWarn,
  canPruneBackups,
} from "@/domains/system/DataPaths";
// eslint-disable-next-line import/first
import { pathsApi } from "@/services/api";

const api = pathsApi as unknown as {
  get: ReturnType<typeof vi.fn>;
  runBackup: ReturnType<typeof vi.fn>;
  pruneBackups: ReturnType<typeof vi.fn>;
};

function bk(over: Partial<BackupStats> = {}): BackupStats {
  return {
    dir: "C:/Users/x/AppData/Roaming/qmt-work-frontend-next/backups",
    count: 10,
    total_bytes: 11_400_000_000,
    total_size: "11.4GB",
    keep: 10,
    min_keep: 2,
    max_total_bytes: 4_294_967_296,
    max_total_size: "4.0GB",
    over_count: false,
    over_budget: false,
    retain_count: 10,
    reclaimable_bytes: 0,
    reclaimable_size: "0B",
    newest: { name: "app.20260920_201924.db", size: 1_140_000_000,
      size_str: "1.1GB", mtime: "2026-09-20T20:19:24" },
    oldest: { name: "app.20260919_075220.db", size: 1_140_000_000,
      size_str: "1.1GB", mtime: "2026-09-19T07:52:20" },
    interval: 3600,
    strays: [],
    stray_count: 0,
    stray_total_bytes: 0,
    stray_total_size: "0B",
    last: { action: "created", detail: "备份完成 app.20260920_201924.db [1.1GB]" },
    ...over,
  };
}

function base(over: Partial<PathsResponse> = {}): PathsResponse {
  const dir = (kind: "export" | "cold" | "db", over2: Record<string, unknown> = {}) =>
    ({
      kind,
      label: kind === "db" ? "主库目录（app.db）" : kind === "cold" ? "冷 K 线仓目录" : "离线数据导出目录",
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
    db: dir("db", { backups: bk() }),
    ...over,
  };
}

beforeEach(() => {
  api.get.mockResolvedValue(base());
  api.runBackup.mockResolvedValue({
    action: "created", path: "C:/x/backups/app.20260920_210000.db",
    message: "已备份：app.20260920_210000.db", ...base(),
  });
  api.pruneBackups.mockResolvedValue({
    removed: ["app.20260919_075220.db"], removed_count: 1,
    freed_bytes: 1_140_000_000, freed_size: "1.1GB",
    note: "已按保留策略清理", ...base(),
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// ------------------------------------------------------------- 文案纯函数

describe("备份占用的文案（纯函数）", () => {
  it("占用句必须同时给出份数、总量与两个上限", () => {
    const t = backupOccupancyText(bk());
    expect(t).toContain("10 份");
    expect(t).toContain("11.4GB");
    expect(t).toContain("上限 4.0GB");
    expect(t).toContain("最多 10 份");
    expect(t).toContain("至少保留 2 份");
  });

  it("未设体积上限时如实说明「未设」，不假装有闸门", () => {
    const t = backupOccupancyText(bk({ max_total_bytes: 0, max_total_size: "" }));
    expect(t).toContain("未设体积上限");
    expect(t).not.toContain("上限 ");
  });

  it("★ skipped 必须说成「未产生新文件」，绝不能说成失败", () => {
    const t = backupLastText(bk({ last: { action: "skipped", detail: "主库未变化" } }));
    expect(t).toContain("未产生新文件");
    expect(t).not.toContain("失败");
  });

  it("failed 才说失败，并带上原因", () => {
    const t = backupLastText(bk({ last: { action: "failed", detail: "磁盘写满" } }));
    expect(t).toContain("失败");
    expect(t).toContain("磁盘写满");
  });

  it("本次进程内没跑过时如实说「尚未执行」，不编造时间", () => {
    const t = backupLastText(bk({ last: { action: "idle" } }));
    expect(t).toContain("尚未执行");
  });

  it("★ 超上限但无可回收时给出手工处置路径，而不是假承诺", () => {
    const w = backupWarn(bk({ over_budget: true, reclaimable_bytes: 0, retain_count: 2 }));
    expect(w).toContain("已超出体积上限");
    expect(w).toContain("至少保留 2 份");
    expect(w).toContain("手工删除");
    expect(w).not.toContain("可回收 0B");
  });

  it("有可回收量时明确报出数字与份数", () => {
    const w = backupWarn(bk({ over_budget: true, reclaimable_bytes: 8_000_000_000,
      reclaimable_size: "7.5GB", count: 10, retain_count: 3 }));
    expect(w).toContain("可回收 7.5GB");
    expect(w).toContain("7 份");
  });

  it("一切正常时不产生任何提示（不能天天报警）", () => {
    expect(backupWarn(bk())).toBe("");
  });

  it("★ 未纳管副本必须点名到文件名与体积", () => {
    const t = backupStrayText(bk({
      strays: [{ name: "app.db.bak_bardate", size: 603_000_000,
        size_str: "575.1MB", mtime: "2026-09-19T00:00:00" }],
      stray_count: 1, stray_total_bytes: 603_000_000, stray_total_size: "575.1MB",
    }));
    expect(t).toContain("app.db.bak_bardate");
    expect(t).toContain("575.1MB");
    expect(t).toContain("不受保留策略管理");
    expect(t).toContain("不会被自动清理");
  });

  it("没有未纳管副本时不产生提示", () => {
    expect(backupStrayText(bk())).toBe("");
  });

  it("canPrune 只在真有可回收量时为真", () => {
    expect(canPruneBackups(bk({ reclaimable_bytes: 0 }))).toBe(false);
    expect(canPruneBackups(bk({ reclaimable_bytes: 1 }))).toBe(true);
  });
});

// ------------------------------------------------------------- 面板渲染

describe("数据目录面板 · 备份占用", () => {
  it("渲染占用、上限与最近一次备份结果", async () => {
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/主库备份 10 份，共 11\.4GB/)).toBeTruthy());
    expect(screen.getByText(/最近一次备份：已完成/)).toBeTruthy();
    expect(screen.getByText(/backups$/)).toBeTruthy();
  });

  it("★ 主库没变时显示「未产生新文件」，不是「失败」", async () => {
    api.get.mockResolvedValue(base({
      db: { ...base().db, backups: bk({ last: { action: "skipped", detail: "主库自 app.x 以来未发生变化" } }) },
    }));
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/未产生新文件/)).toBeTruthy());
    expect(screen.queryByText(/最近一次备份：失败/)).toBeNull();
  });

  it("★ 超上限且无可回收时给出说明，并把清理按钮置灰", async () => {
    api.get.mockResolvedValue(base({
      db: { ...base().db, backups: bk({ over_budget: true, reclaimable_bytes: 0, retain_count: 2 }) },
    }));
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/已超出体积上限/)).toBeTruthy());
    const btn = screen.getByRole("button", { name: /清理旧备份（无可回收）/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("★ 有可回收量时按钮可点，且必须走确认弹窗（删除不可逆）", async () => {
    api.get.mockResolvedValue(base({
      db: { ...base().db, backups: bk({ over_budget: true, reclaimable_bytes: 8_000_000_000,
        reclaimable_size: "7.5GB", count: 10, retain_count: 3 }) },
    }));
    render(<DataPaths />);
    // ⚠️ 「可回收 7.5GB」同时出现在**提示句**与**按钮文案**里 ⇒ 必须按 role 定位，
    //    用 getByText 会命中两个元素而报 "Found multiple elements"。
    const btn = (await waitFor(() =>
      screen.getByRole("button", { name: /清理旧备份（可回收 7\.5GB）/ }))) as HTMLButtonElement;
    expect(screen.getByText(/按当前保留策略可回收 7\.5GB/)).toBeTruthy();
    expect(btn.disabled).toBe(false);

    // 点一次只是**打开确认框**，不能直接发请求
    btn.click();
    await waitFor(() => expect(screen.getByText("清理旧备份")).toBeTruthy());
    expect(api.pruneBackups).not.toHaveBeenCalled();

    // 取消 = 什么都不做
    screen.getByRole("button", { name: "取消" }).click();
    await waitFor(() => expect(screen.queryByText(/删除不可恢复/)).toBeNull());
    expect(api.pruneBackups).not.toHaveBeenCalled();
  });

  it("确认后才真的清理，并把释放量如实回报", async () => {
    api.get.mockResolvedValue(base({
      db: { ...base().db, backups: bk({ over_budget: true, reclaimable_bytes: 1_140_000_000,
        reclaimable_size: "1.1GB", count: 10, retain_count: 9 }) },
    }));
    render(<DataPaths />);
    const btn = (await waitFor(() =>
      screen.getByRole("button", { name: /清理旧备份（可回收 1\.1GB）/ }))) as HTMLButtonElement;
    btn.click();
    await waitFor(() => expect(screen.getByText("清理旧备份")).toBeTruthy());
    screen.getByRole("button", { name: /^删除 1 份旧备份$/ }).click();
    await waitFor(() => expect(api.pruneBackups).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/已清理 1 份旧备份，释放 1\.1GB/)).toBeTruthy());
  });

  it("「立即备份」调用后端并展示后端给的结论文案", async () => {
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/主库备份 10 份/)).toBeTruthy());
    screen.getByRole("button", { name: "立即备份" }).click();
    await waitFor(() => expect(api.runBackup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/已备份：app\.20260920_210000\.db/)).toBeTruthy());
  });

  it("★ 未纳管的库副本必须渲染出来（含体积），否则「磁盘去哪了」永远查不出", async () => {
    api.get.mockResolvedValue(base({
      db: { ...base().db, backups: bk({
        strays: [{ name: "app.db.bak_bardate", size: 603_000_000,
          size_str: "575.1MB", mtime: "2026-09-19T00:00:00" }],
        stray_count: 1, stray_total_bytes: 603_000_000, stray_total_size: "575.1MB",
      }) },
    }));
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText(/app\.db\.bak_bardate/)).toBeTruthy());
    expect(screen.getByText(/575\.1MB/)).toBeTruthy();
  });

  it("老后端不返回 backups 时不整块炸掉，其余目录照常显示", async () => {
    const data = base();
    delete (data.db as { backups?: unknown }).backups;
    api.get.mockResolvedValue(data);
    render(<DataPaths />);
    await waitFor(() => expect(screen.getByText("主库目录（app.db）")).toBeTruthy());
    expect(screen.queryByText(/主库备份/)).toBeNull();
    expect(screen.queryByRole("button", { name: "立即备份" })).toBeNull();
  });
});
