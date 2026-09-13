import type { Envelope } from "@/shared/types";

/**
 * 统一 REST 客户端。
 *
 * 契约约定（与后端一致，勿擅改）：
 * - 所有端点前缀 /api/v1
 * - 鉴权：Authorization: Bearer <key>（后端同时接受 X-API-Key）
 * - 响应包裹 { code, message, data }；code !== 0 视为业务失败并抛 ApiError
 * - 未连接券商时后端返回 503 + 引导信息 —— 必须原样抛出，绝不粉饰为成功
 */

const BASE = "/api/v1";
const DEFAULT_TIMEOUT = 15_000;
const KEY_STORAGE = "qmt.apiKey";

let apiKey = "";
try {
  apiKey = localStorage.getItem(KEY_STORAGE) ?? "";
} catch {
  apiKey = "";
}

export function getApiKey(): string {
  return apiKey;
}

export function setApiKey(key: string): void {
  apiKey = key;
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* 隐私模式下降级为内存保存 */
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: number;
  readonly brokerUnavailable: boolean;

  constructor(message: string, status: number, code: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.brokerUnavailable = status === 503;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function buildUrl(path: string, query?: Query): string {
  const url = `${BASE}${path}`;
  if (!query) return url;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === "") continue;
    sp.append(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `${url}?${qs}` : url;
}

interface RequestOptions {
  query?: Query;
  body?: unknown;
  timeout?: number;
  retry?: number;
  signal?: AbortSignal;
}

async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const { query, body, timeout = DEFAULT_TIMEOUT, retry = method === "GET" ? 1 : 0 } = opts;

  let lastErr: unknown;
  for (let attempt = 0; attempt <= retry; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
      if (body !== undefined) headers["Content-Type"] = "application/json";

      const res = await fetch(buildUrl(path, query), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: opts.signal ?? ctrl.signal,
      });

      const text = await res.text();
      let payload: unknown = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }

      const env = payload as Envelope<T> | null;

      if (!res.ok) {
        const msg =
          env?.message ??
          (res.status === 503
            ? "未连接券商客户端，请先在「连接管理」中添加并连接券商"
            : `请求失败（HTTP ${res.status}）`);
        throw new ApiError(msg, res.status, env?.code ?? res.status);
      }

      if (env && typeof env === "object" && "code" in env) {
        if (env.code !== 0) {
          throw new ApiError(env.message ?? "业务处理失败", res.status, env.code);
        }
        return env.data;
      }
      // 兼容无包裹响应（如 /live）
      return payload as T;
    } catch (e) {
      lastErr = e;
      // 业务错误不重试；仅网络层错误（AbortError/TypeError）重试
      if (e instanceof ApiError) throw e;
      if (attempt === retry) break;
    } finally {
      clearTimeout(timer);
    }
  }
  throw new ApiError(
    lastErr instanceof Error ? lastErr.message : "网络请求失败",
    0,
    0,
  );
}

export const http = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("POST", path, { ...opts, body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("PUT", path, { ...opts, body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("PATCH", path, { ...opts, body }),
  del: <T>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, opts),
};
