#!/usr/bin/env python3
"""P1-5：前端 API 层与后端 REST 契约的**漂移检查**（不是生成器，是守卫）。

问题
----
``frontend-next/src/services/api/*.ts`` 是**手工维护**的：每个调用点写一个
路径字面量，例如 ``http.get<Quote>("/market/quote", {...})``。
后端改名/删端点时，前端不会编译报错 —— 它只会在**运行时**拿到 404，
而界面上的表现是「加载失败」这种最没有信息量的提示。

本脚本把「后端真正注册了哪些端点」与「前端声明要调哪些端点」对账：

- 后端真源 = ``backend/tests/contracts/rest_endpoints.json``
  （由 ``backend/tests/contracts/introspect.py`` 从**运行中的 app** 导出，
  不是手写的，所以它不会跟着前端一起漂）。
- 前端真源 = ``services/api/**/*.ts`` 里 ``http.<method><T>("<path>")`` 的字面量。

判据
----
前端调用的端点必须**逐一**存在于后端契约中。发现「前端调了后端没有的端点」即失败
（这正是会 404 的那一类）。反向（后端有、前端没调）**不算失败** —— 前端不需要
覆盖全部端点，硬要求覆盖只会逼人写假调用。

★ 静态解析不到的调用点（路径来自变量/拼接）会被单独列出，**不静默跳过** ——
  否则「检查通过」可能只是因为解析不到。
用法：
    python scripts/check_api_contract_drift.py            # 校验
    python scripts/check_api_contract_drift.py -v         # 列出全部对账明细
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "backend" / "tests" / "contracts" / "rest_endpoints.json"
API_DIR = ROOT / "frontend-next" / "src" / "services" / "api"
PREFIX = "/api/v1"

#: ``http.get<T>("/x")`` / ``http.post("/x", ...)`` —— 泛型可选，路径可为
#: 单/双引号字符串或模板字面量。
CALL_RE = re.compile(
    r"""\bhttp\.(get|post|patch|put|delete)\s*(?:<[^<>]*>)?\s*\(\s*([`"'])([^`"']*)\2""",
    re.VERBOSE,
)
#: 模板字面量里的插值 ``${kid}`` → 路径参数占位 ``{kid}``
INTERP_RE = re.compile(r"\$\{[^}]*\}")


def load_backend_endpoints() -> set[str]:
    raw = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return {k.strip() for k in raw if isinstance(k, str)}


def normalize(method: str, path: str) -> str:
    """把前端路径字面量归一成 ``METHOD /api/v1/...`` 的契约形态。"""
    p = path.split("?", 1)[0].strip()
    p = INTERP_RE.sub("{param}", p)
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith(PREFIX + "/") and p != PREFIX:
        p = PREFIX + p
    # 契约里的路径参数名各异（{kid} / {rid} / {job_id}），统一成 {param} 再比
    p = re.sub(r"\{[^}]*\}", "{param}", p)
    return f"{method.upper()} {p}"


def contract_normalized(endpoints: set[str]) -> set[str]:
    out = set()
    for e in endpoints:
        method, _, path = e.partition(" ")
        out.add(normalize(method, path))
    return out


def scan_frontend() -> tuple[dict[str, list[str]], list[str]]:
    """返回 ({归一化端点: [出处...]}, [无法静态解析的调用点...])。"""
    found: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for ts in sorted(API_DIR.rglob("*.ts")):
        text = ts.read_text(encoding="utf-8")
        for m in CALL_RE.finditer(text):
            method, _, path = m.group(1), m.group(2), m.group(3)
            if "${" in path and not INTERP_RE.search(path):
                unresolved.append(f"{ts.name}: {method} {path}")
                continue
            key = normalize(method, path)
            found.setdefault(key, []).append(f"{ts.name}:{text[:m.start()].count(chr(10)) + 1}")
        # 形如 http.get(someVar) 的调用点
        for m in re.finditer(r"\bhttp\.(get|post|patch|put|delete)\s*(?:<[^<>]*>)?\s*\(\s*([A-Za-z_$][\w$.]*)", text):
            unresolved.append(f"{ts.name}: {m.group(1)} {m.group(2)}")
    return found, unresolved


def main() -> int:
    ap = argparse.ArgumentParser(description="前端 API 层 ↔ 后端 REST 契约漂移检查")
    ap.add_argument("-v", "--verbose", action="store_true", help="列出全部对账明细")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not CONTRACT.exists():
        print(f"✗ 后端契约不存在：{CONTRACT}", file=sys.stderr)
        return 1
    if not API_DIR.exists():
        print(f"✗ 前端 API 目录不存在：{API_DIR}", file=sys.stderr)
        return 1

    backend = load_backend_endpoints()
    backend_norm = contract_normalized(backend)
    frontend, unresolved = scan_frontend()

    # ★ 自检：两边都解析不出东西 ⇒ 检查是「永远绿」的
    if len(backend) < 50:
        print(f"✗ 后端契约只解析到 {len(backend)} 个端点 —— 检查失效", file=sys.stderr)
        return 1
    if len(frontend) < 20:
        print(f"✗ 前端只解析到 {len(frontend)} 个端点 —— 正则可能已与代码形态脱节",
              file=sys.stderr)
        return 1

    missing = sorted(k for k in frontend if k not in backend_norm)

    print(f"后端契约端点   : {len(backend)}")
    print(f"前端调用端点   : {len(frontend)}")
    print(f"静态解析不到   : {len(unresolved)}")
    if args.verbose:
        for k in sorted(frontend):
            mark = "✓" if k in backend_norm else "✗"
            print(f"  [{mark}] {k}   ← {', '.join(frontend[k])}")
        for u in unresolved:
            print(f"  [?] {u}")
        unused = sorted(backend_norm - set(frontend))
        print(f"  -- 后端有、前端未调用（不算失败）: {len(unused)}")

    if missing:
        print("\n✗ 前端调用了后端**不存在**的端点（运行时会 404）：")
        for k in missing:
            print(f"    {k}   ← {', '.join(frontend[k])}")
        print("\nAPI CONTRACT DRIFT DETECTED")
        return 1

    print("\n✓ 前端调用的每个端点都能在后端契约里找到")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
