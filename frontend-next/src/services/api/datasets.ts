import { http } from "../http";

/**
 * 数据集 API（`app/routes/datasets.py` 的 `/datasets/*`，R28 多数据类型同步）。
 *
 * ## 契约要点（照抄后端，改动前先对后端）
 *
 * - `GET  /datasets`            —— 按类别分组的数据集清单，每项带**运行时状态**
 *   （`local.rows/codes/first_dt/last_dt` + `last_sync_at`）。
 * - `GET  /datasets/sources`    —— 源可用性。**回答「现在点同步，数据会从哪来」**：
 *   `per_dataset[id].declared` 是声明链（意图：券商优先），
 *   `per_dataset[id].resolved` 是实际链（现实：券商没连就落到 tdx）。
 *   两者要一起展示，否则用户会困惑「我明明配了券商优先」。
 * - `POST /datasets/{id}/sync`  —— 触发同步。**默认只跑 50 只**，
 *   超过 `MAX_SYNC_LIMIT`(500) 后端直接 400 并提示改用定时调度。
 * - `GET  /datasets/{id}/data`  —— 查**本地已下载**的数据（离线可用、不打远程源）。
 *   这是「下载来的数据能用」的证明。
 *
 * ## 零 mock 契约
 *
 * - 数据集清单 / 状态全部来自后端，前端**不内置**任何数据集列表 —— 后端
 *   `datasource/datasets.py` 是唯一真源，加一行前端自动跟上。
 * - `problems[]` 出现即视为同步失败；界面必须红色醒目显示，不许静默。
 * - 同步成功但 `written === 0` 后端会返回 `code != 0`（明说数据源不可用），
 *   前端不要把「HTTP 200」当成成功——必须看 `code`。
 */

export interface DatasetLocalStatus {
  rows: number;
  codes: number;
  first_dt: string;
  last_dt: string;
}

export interface DatasetItem {
  id: string;
  label: string;
  category: string;
  category_label: string;
  capability: string;
  chain: string[];
  store: string;
  cursor: string;
  period: string;
  supports_range: boolean;
  lookback: number;
  retention_days: number;
  cron: string;
  default_enabled: boolean;
  adjust: string;
  unit_note: string;
  tags: string[];
  local: DatasetLocalStatus;
  last_sync_at: string;
}

export interface DatasetCategory {
  id: string;
  label: string;
  items: DatasetItem[];
}

export interface DatasetListResponse {
  total: number;
  categories: DatasetCategory[];
}

export interface DatasetChainInfo {
  declared: string[];
  resolved: string[];
  effective_first: string;
  /** 解析链为空但有内置实现时给出源 id（当前只有 `local`）。 */
  builtin_fallback: string;
  /** 内置源的说明文案（如「内置（不依赖网络）」）。 */
  builtin_note: string;
}

export interface DatasetSourcesResponse {
  broker_available: boolean;
  broker_note: string;
  providers: {
    provider: string;
    name: string;
    active: boolean;
    dependency_available: boolean;
    commercial_ok: boolean;
    capabilities: string[];
    status: string;
  }[];
  health: Record<string, unknown>;
  per_dataset: Record<string, DatasetChainInfo>;
}

export interface DatasetSyncInput {
  mode?: "incremental" | "full";
  limit?: number;
  concurrency?: number;
  source?: string;
  dry_run?: boolean;
  codes?: string[];
}

export interface DatasetSyncResult {
  dataset: string;
  label: string;
  ok: boolean;
  mode: string;
  requested: number;
  written: number;
  skipped: number;
  failed: number;
  source: string;
  sources_used: Record<string, number>;
  problems: string[];
  source_unknown: boolean;
  dry_run: boolean;
  started_at: string;
  finished_at: string;
  duration_s: number;
  detail: Record<string, unknown>;
  local?: DatasetLocalStatus;
}

export interface DatasetDataResponse {
  dataset: string;
  code: string;
  count: number;
  rows: Record<string, unknown>[];
}

export const datasetsApi = {
  list: () => http.get<DatasetListResponse>("/datasets"),
  sources: () => http.get<DatasetSourcesResponse>("/datasets/sources"),
  detail: (id: string) => http.get<DatasetItem>(`/datasets/${encodeURIComponent(id)}`),
  /**
   * 触发同步。**超时放宽到 5 分钟**：默认 15s 是给「查状态」用的，
   * 而「下载 50 只标的」是 IO 密集操作 —— 分钟线、逐笔这类重数据集
   * （`tags` 含 `heavy`）在第三方源上超过 15s 很正常，用默认值会让
   * 用户看到「请求超时」但其实后端还在正常拉数。
   */
  sync: (id: string, body: DatasetSyncInput) =>
    http.post<DatasetSyncResult>(`/datasets/${encodeURIComponent(id)}/sync`, body, {
      timeout: 300_000,
    }),
  data: (id: string, params: { code?: string; limit?: number; start?: string; end?: string }) =>
    http.get<DatasetDataResponse>(`/datasets/${encodeURIComponent(id)}/data`, {
      query: {
        code: params.code || undefined,
        limit: params.limit,
        start: params.start || undefined,
        end: params.end || undefined,
      },
    }),
};
