import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { PAGES } from "@/app/routes";

/**
 * 阶段 3 贯通验收 · 前端入口侧。
 *
 * 后端 `backend/tests/test_e2e_flows.py` 验的是「API → 后端链路 → 事件回推」；
 * 本文件验的是同一批 10 条流程的**另一半**：「前端入口 → API」。
 * 两侧共用唯一真源 `backend/tests/contracts/e2e_flows.json`
 * —— 流程涉及的端点/页面发生变化时必须同步清单，否则这里会红。
 *
 * 存在意义：此前 §5 的 10 条流程「后端有能力、前端无入口」是静默断裂
 * （页面不报错、只是功能永远到不了），只有机械化核对才能挡住。
 */

interface FlowManifest {
  flows: { id: string; title: string; endpoints: string[]; pages: string[] }[];
  api_only: { endpoint: string; reason: string }[];
}

const manifest: FlowManifest = JSON.parse(
  readFileSync(
    resolve(__dirname, "../../backend/tests/contracts/e2e_flows.json"),
    "utf-8",
  ),
);

function collectSources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) collectSources(p, out);
    else if (p.endsWith(".ts") || p.endsWith(".tsx")) out.push(p);
  }
  return out;
}

const SRC = resolve(__dirname, "../src");
const SOURCE_BLOB = collectSources(SRC)
  .map((f) => readFileSync(f, "utf-8"))
  .join("\n");

/** 端点 → 前端源码里应出现的路径片段（去 /api/v1 前缀与路径参数） */
function pathToken(endpoint: string): string {
  const path = endpoint.split(" ")[1] ?? "";
  return path.replace("/api/v1", "").split("{")[0] ?? "";
}

const apiOnly = new Set(manifest.api_only.map((a) => a.endpoint));

describe("§5 核心流程 · 前端入口核对", () => {
  it("清单本身完整：10 条流程，每条都有端点与入口页", () => {
    expect(manifest.flows).toHaveLength(10);
    for (const f of manifest.flows) {
      expect(f.endpoints.length, `${f.id} 无端点`).toBeGreaterThan(0);
      expect(f.pages.length, `${f.id} 无入口页`).toBeGreaterThan(0);
    }
    // api_only 必须写明理由，禁止静默豁免
    for (const a of manifest.api_only) {
      expect(a.reason.length, `${a.endpoint} 豁免未写理由`).toBeGreaterThan(10);
    }
  });

  for (const flow of manifest.flows) {
    it(`${flow.id} · ${flow.title}：端点均有前端调用点`, () => {
      const missing = flow.endpoints
        .filter((ep) => !apiOnly.has(ep))
        .filter((ep) => !SOURCE_BLOB.includes(pathToken(ep)));
      expect(missing, `前端未出现这些调用点：${missing.join(", ")}`).toEqual([]);
    });

    it(`${flow.id} · ${flow.title}：入口页均已注册且可用`, () => {
      for (const key of flow.pages) {
        const page = PAGES[key as keyof typeof PAGES];
        expect(page, `页面未注册：${key}`).toBeTruthy();
        expect(page?.status, `页面 ${key} 仍是占位，流程走不通`).toBe("done");
      }
    });
  }
});
