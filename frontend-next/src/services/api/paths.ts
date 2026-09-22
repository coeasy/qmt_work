import { http } from "../http";

/**
 * 数据目录 API（`app/routes/config.py` 的 `/config/paths` 组）。
 *
 * ## 契约要点（照抄后端，改动前先对后端）
 *
 * - `GET /config/paths` 返回 `{install_dir, config_file, export, cold, db}`，
 *   **三类目录的结构同构**：`path` / `file` / `configured` / `default_path` /
 *   `mutable` / `requires_restart` / `exists` / `writable` / `inside_install` /
 *   `usable` / `reason` / `note`。
 * - ⚠️ `db` 的 `mutable` 恒为 `false`、`requires_restart` 恒为 `true`：
 *   主库路径在进程启动时固化，**改完必须重启**。界面若把它渲染成「已生效」
 *   就是最典型的一种「点了没反应」。
 * - `cold` 额外带 `rows` / `attached_path` / `in_sync` / `candidates`：
 *   `in_sync=false` 表示「配置改了但冷仓没换成功」，必须显式提示，不能静默。
 * - `POST /config/paths/validate` **永不 4xx/5xx**（服务于边输边提示），
 *   不可用只是 `ok=false + reason`。
 */
export type PathKind = "export" | "cold" | "db";

export interface PathCandidate {
  path: string;
  rows: number;
}

export interface PathDirInfo {
  kind: PathKind;
  label: string;
  /** 目录本体（db 是 app.db 所在文件夹） */
  path: string;
  /** 目录里的那个文件（导出目录没有 ⇒ 空串） */
  file: string;
  /** 是否被显式配置过（false = 正在用默认值） */
  configured: boolean;
  default_path: string;
  /** 能否**运行期**改；false ⇒ 必须重启 */
  mutable: boolean;
  requires_restart: boolean;
  note?: string;
  exists: boolean;
  writable: boolean;
  /** 位于安装目录内 ⇒ 提示「重装可能丢数据」，**不是**拒绝理由 */
  inside_install: boolean;
  usable: boolean;
  reason: string;
  // ---- 仅 cold ----
  rows?: number;
  /** 已装配到 K 线缓存的冷仓文件（与 path 不一致 = 切换未生效） */
  attached_path?: string;
  in_sync?: boolean;
  candidates?: PathCandidate[];
  // ---- 仅 db：备份占用 ----
  backups?: BackupStats;
}

/**
 * 主库备份的**占用快照**（`db.backups`）。
 *
 * ## 为什么必须显示它
 *
 * 备份是**整库全量复制**。1.14GB 的主库按「保留 10 份」就是 **11GB** 常驻磁盘 ——
 * 实测用户数据目录就是 11GB（C: 盘仅剩 47GB / 已用 86%），而此前界面上
 * **一处都没有提**（`list_backups()` 零调用方），用户只能等磁盘满了才发现。
 *
 * ## 渲染纪律
 *
 * - `over_budget && reclaimable_bytes === 0` 是**合法**状态（预算比单份备份还小，
 *   受 `min_keep` 地板保护）：不能渲染成「有空间可回收」（假承诺），
 *   也不能渲染成「一切正常」（掩盖磁盘被吃）。要如实说「已超上限，但为保证可还原性
 *   至少保留 N 份，无法再自动清理」。
 * - `last.action` 有 **created / skipped / failed** 三态：`skipped` 表示「主库没变、
 *   已有备份覆盖」，**不是失败**。把跳过说成失败就是错误归因。
 * - `strays` 是主库目录里**不受保留策略管理**的库副本（如手工留的
 *   `app.db.bak_bardate`，实测 603MB）：只上报，删不删由用户定。
 */
export interface BackupFileInfo {
  name: string;
  size: number;
  size_str: string;
  mtime: string;
}

export interface BackupStats {
  /** 备份目录（`<主库目录>/backups`） */
  dir: string;
  count: number;
  total_bytes: number;
  total_size: string;
  /** 份数上限 */
  keep: number;
  /** 体积上限再严也至少保留的份数 */
  min_keep: number;
  /** 体积上限（字节）；0 = 不限 */
  max_total_bytes: number;
  /** 体积上限的人类可读值；不限时为空串 */
  max_total_size: string;
  over_count: boolean;
  over_budget: boolean;
  /** 按当前策略保留几份 */
  retain_count: number;
  /** 现在就能回收的字节数（0 = 无可回收） */
  reclaimable_bytes: number;
  reclaimable_size: string;
  newest: BackupFileInfo | null;
  oldest: BackupFileInfo | null;
  /** 周期备份间隔（秒） */
  interval: number;
  /** 主库目录里不受策略管理的库副本（只读上报） */
  strays: BackupFileInfo[];
  stray_count: number;
  stray_total_bytes: number;
  stray_total_size: string;
  /** 最近一次备份结果：action = created / skipped / failed / idle */
  last: { action: string; reason?: string; detail?: string; name?: string; path?: string };
}

export interface PathsResponse {
  install_dir: string;
  config_file: string;
  export: PathDirInfo;
  cold: PathDirInfo;
  db: PathDirInfo;
}

export interface BackupActionResult {
  action: "created" | "skipped" | "failed" | string;
  path: string;
  message: string;
}

export interface BackupPruneResult {
  removed: string[];
  removed_count: number;
  freed_bytes: number;
  freed_size: string;
  note: string;
}

export interface PathValidateResult {
  input: string;
  ok: boolean;
  reason: string;
  path: string;
  exists: boolean;
  writable: boolean;
  inside_install: boolean;
  is_system: boolean;
  is_drive_root: boolean;
}

/** PUT /config/paths 的返回 = 动作结果 **叠加**一份最新快照（后端把快照字段平铺在顶层）。 */
export type PathsSetResult = {
  saved: boolean;
  kind: PathKind;
  path: string;
  requires_restart: boolean;
  file?: string;
  current_file?: string;
  note?: string;
} & PathsResponse;

/** 迁移结果 = 动作结果 **叠加**一份最新快照。
 *  ⚠️ 用 `type` 而不是 `interface`：只有类型别名才能写交叉类型（`A & B`），
 *  `interface X {} & Y` 是语法错误。 */
export type PathsMigrateResult = {
  moved: number;
  batches: number;
  source: string;
  source_kept: boolean;
  note?: string;
} & PathsResponse;

/** 备份动作结果 = 动作结果 **叠加**一份最新快照（后端把快照平铺在顶层）。 */
export type BackupRunResult = BackupActionResult & PathsResponse;
export type BackupPruneResultFull = BackupPruneResult & PathsResponse;

export const pathsApi = {
  /** 三类目录的生效路径 / 可改性 / 体检结果 */
  get: () => http.get<PathsResponse>("/config/paths"),

  /** 校验一个目录能不能用（不落配置；永不抛错） */
  validate: (path: string) =>
    http.post<PathValidateResult>("/config/paths/validate", { path }),

  /** 设置目录。db 会返回 requires_restart=true */
  set: (kind: PathKind, path: string) =>
    http.put<PathsSetResult>("/config/paths", { kind, path }),

  /** 把旧冷仓文件里的历史 K 行复制进当前冷仓（不删源）。source 省略时自动挑候选 */
  migrateCold: (source = "") =>
    http.post<PathsMigrateResult>("/config/paths/migrate-cold", { source }),

  /** 立刻做一次主库备份。⚠️ action=skipped 表示「主库没变」，**不是失败** */
  runBackup: () => http.post<BackupRunResult>("/config/paths/db-backups/run", {}),

  /** 按保留策略清理旧备份（**不可逆**：直接删文件，不进回收站） */
  pruneBackups: () => http.post<BackupPruneResultFull>("/config/paths/db-backups/prune", {}),
};
