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
#:
#: ★ 三轮修复，每一轮都是**假绿灯**（门禁「看不见」就等于放行）：
#:   1. 客户端真正发 DELETE 的助手叫 **`http.del`**（`services/http.ts:177`
#:      `del: (path, opts) => request("DELETE", ...)`）。全仓 `http.del(` 有 **9 处**、
#:      `http.delete(` **0 处** —— 而原正则只列了 `delete`，于是这 9 个 DELETE 端点
#:      **从未被对账过**：后端把它们改名/删掉，前端照样运行时 404，门禁全绿。
#:   2. 泛型用 ``[^<>]*`` 描述，碰到嵌套实参的 `Record<string, unknown>`（写成
#:      ``http.post<Record<string, unknown>>(...)``）时尾部多出一个 `>` 而匹配失败
#:      —— `limitupApi.start/stop/reset` 就是这样整条漏掉的。
#:   3. 泛型改成 ``[^(\n]*`` 后仍要求**不跨行** —— 但前端为类型排版，泛型参数会
#:      排成 10 行（`system.ts` 的 `remoteAccessStatus` / `setRemoteAccessMode`）。
#:      于是「`http.get<` 开头、路径写在十几行之后」的调用**整条对不上**：
#:      它既没进 `found`，也不进 `unresolved`，门禁显示全绿而实际没查。
#:   修法：泛型段改成「直到第一个 `(`」且不排斥换行（`[^()]*`），嵌套尖括号天然成立。
#:   再叠一层自证（见 :func:`uncovered_call_sites`）：凡是 `http.<method>` 出现处，
#:   必须被本正则或 unresolved 正则覆盖，否则直接判检查失效。
CALL_RE = re.compile(
    r"""\bhttp\.(get|post|patch|put|delete|del)\b[^()]*\(\s*([`"'])([^`"']*)\2""",
    re.VERBOSE,
)
#: 泛型段允许跨行后，未解析正则也要跟上（`Record<string, unknown>` / 多行泛型）。
UNRESOLVED_CALL_RE = re.compile(
    r"\bhttp\.(get|post|patch|put|delete|del)\b[^()]*\(\s*([A-Za-z_$][\w$.]*)"
)
#: 只做**站点计数**用：每个 `http.<method>` 都是一个调用点，一个都不能隐身。
CALL_SITE_RE = re.compile(r"\bhttp\.(get|post|patch|put|delete|del)\b")
#: 模板字面量里的插值 ``${kid}`` → 路径参数占位 ``{kid}``
INTERP_RE = re.compile(r"\$\{[^}]*\}")
#: 调用点之后的查询参数窗口：``{ query: { a: 1, b: x } }``
QUERY_OBJ_RE = re.compile(r"query:\s*\{([^{}]*)\}", re.S)
QUERY_NAME_RE = re.compile(r"(\w+)\s*:")

ROUTES_DIR = ROOT / "backend" / "app" / "routes"
#: 后端签名里不算查询参数的名字/注解
_SIG_EXCLUDE_NAMES = {"ctx", "body", "request", "response", "payload", "self"}
_SIG_EXCLUDE_ANN = ("AppContext", "dict", "Dict", "Body", "Depends", "Request", "UploadFile")


def load_backend_endpoints() -> set[str]:
    raw = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return {k.strip() for k in raw if isinstance(k, str)}


#: 前端 http 助手名 → 规范 HTTP 方法名。
#: `del` 是 `delete` 的别名（`services/http.ts:177` `del: (path, opts) =>
#: request("DELETE", ...)`），而契约里写的是规范名 `DELETE` —— 不做这层映射，
#: 刚被 CALL_RE 认出来的 9 个 DELETE 端点会全部误报成「后端不存在」（假红）。
_METHOD_ALIASES = {"del": "delete"}


def normalize(method: str, path: str) -> str:
    """把前端路径字面量归一成 ``METHOD /api/v1/...`` 的契约形态。"""
    m = method.strip().lower()
    m = _METHOD_ALIASES.get(m, m)
    p = path.split("?", 1)[0].strip()
    p = INTERP_RE.sub("{param}", p)
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith(PREFIX + "/") and p != PREFIX:
        p = PREFIX + p
    # 契约里的路径参数名各异（{kid} / {rid} / {job_id}），统一成 {param} 再比
    p = re.sub(r"\{[^}]*\}", "{param}", p)
    return f"{m.upper()} {p}"


def contract_normalized(endpoints: set[str]) -> set[str]:
    out = set()
    for e in endpoints:
        method, _, path = e.partition(" ")
        out.add(normalize(method, path))
    return out


def scan_frontend() -> tuple[dict[str, list[str]], list[str], dict[str, set[str]]]:
    """返回 ({归一化端点: [出处...]}, [无法静态解析的调用点...], {归一化端点: 查询参数名集合})。"""
    found: dict[str, list[str]] = {}
    unresolved: list[str] = []
    query_params: dict[str, set[str]] = {}
    for ts in sorted(API_DIR.rglob("*.ts")):
        text = ts.read_text(encoding="utf-8")
        for m in CALL_RE.finditer(text):
            method, _, path = m.group(1), m.group(2), m.group(3)
            if "${" in path and not INTERP_RE.search(path):
                unresolved.append(f"{ts.name}: {method} {path}")
                continue
            key = normalize(method, path)
            found.setdefault(key, []).append(f"{ts.name}:{text[:m.start()].count(chr(10)) + 1}")
            # 查询参数名提取：路径内联 `?a=b` + 调用点后随的 { query: {...} }
            names: set[str] = set()
            if "?" in path:
                qs = path.split("?", 1)[1]
                for seg in INTERP_RE.sub("x", qs).split("&"):
                    if seg:
                        names.add(seg.split("=")[0])
            tail = text[m.end():m.end() + 400]
            qm = QUERY_OBJ_RE.search(tail)
            if qm:
                # 只在 query 对象与本次调用相邻（中间没有其他 http. 调用）时归属
                if "http." not in text[m.end():m.end() + qm.start()]:
                    names.update(QUERY_NAME_RE.findall(qm.group(1)))
            if names:
                query_params.setdefault(key, set()).update(names)
        # 形如 http.get(someVar) 的调用点
        for m in UNRESOLVED_CALL_RE.finditer(text):
            if not CALL_RE.match(text, m.start()):
                unresolved.append(f"{ts.name}: {m.group(1)} {m.group(2)}")
    return found, unresolved, query_params


def uncovered_call_sites() -> list[str]:
    """自证：``http.<method>`` 出现处必须被 CALL_RE 或 UNRESOLVED_CALL_RE 覆盖。

    这个函数存在的唯一理由，是让**门禁自己**无法变成假绿灯。
    前两轮漏配（`http.del`、嵌套泛型）都是「门禁说 0 个问题」而实际整批端点
    从未被对账。有了这一层，新增任何一种前端调用写法（多行泛型、变量路径、
    换行插值…）都会在这里暴露出来，而不是悄无声息地被跳过。
    """
    out: list[str] = []
    for ts in sorted(API_DIR.rglob("*.ts")):
        text = ts.read_text(encoding="utf-8")
        covered = {m.span() for m in CALL_RE.finditer(text)}
        covered |= {m.span() for m in UNRESOLVED_CALL_RE.finditer(text)}
        for m in CALL_SITE_RE.finditer(text):
            start = m.start()
            if not any(c[0] <= start <= c[1] for c in covered):
                out.append(f"{ts.name}:{text[:start].count(chr(10)) + 1}")
    return out


def backend_query_params() -> dict[str, set[str]]:
    """从路由处理函数**签名**提取查询参数名（FastAPI 口径：签名标量参数=查询参数）。

    返回 {归一化端点: {参数名...}}。这补上「只核路径不核参数名」的盲区：
    前端发 ``?code=`` 而后端签名读 ``sector`` 时，FastAPI 静默忽略未知参数、
    返回 200 + 默认值 —— 看似正常实则错误数据（2026-09-29 code/sector 实锤类）。
    """
    out: dict[str, set[str]] = {}
    if not ROUTES_DIR.exists():
        return out
    decl_re = re.compile(
        r"""@router\.(get|post|put|delete|patch)\(\s*["']([^"']+)["'][^)]*\)\s*\n\s*async def\s+(\w+)\s*\(([^)]*)\)""",
        re.S,
    )
    for py in sorted(ROUTES_DIR.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for m in decl_re.finditer(text):
            method, path, _fn, sig = m.group(1), m.group(2), m.group(3), m.group(4)
            names: set[str] = set()
            for part in sig.split(","):
                part = part.strip()
                if not part or "=" not in part and ":" not in part:
                    continue
                pname = part.split(":", 1)[0].split("=", 1)[0].strip().lstrip("*")
                ann = part.split(":", 1)[1].split("=", 1)[0].strip() if ":" in part else ""
                if pname in _SIG_EXCLUDE_NAMES or not pname:
                    continue
                if any(x in ann for x in _SIG_EXCLUDE_ANN):
                    continue
                names.add(pname)
            if names:
                out.setdefault(normalize(method, path), set()).update(names)
    return out


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
    frontend, unresolved, fe_query = scan_frontend()
    be_query = backend_query_params()

    # ★ 参数级对账：前端发送的查询参数名必须存在于后端签名（否则 FastAPI
    #   静默忽略 → 200 + 默认值 = 看似正常的错误数据）
    param_drift: dict[str, set[str]] = {}
    for key, names in sorted(fe_query.items()):
        known = be_query.get(key)
        if known is None:
            continue  # 后端签名解析不到（POST body 形态等）→ 不误报
        unknown = names - known
        if unknown:
            param_drift[key] = unknown

    # ★ 自检：两边都解析不出东西 ⇒ 检查是「永远绿」的
    if len(backend) < 50:
        print(f"✗ 后端契约只解析到 {len(backend)} 个端点 —— 检查失效", file=sys.stderr)
        return 1
    if len(frontend) < 20:
        print(f"✗ 前端只解析到 {len(frontend)} 个端点 —— 正则可能已与代码形态脱节",
              file=sys.stderr)
        return 1

    # ★ 自证：不允许有「既没被对账、也没被报未解析」的隐身调用点。
    #   这是把「门禁看不见」从静默风险变成硬失败的唯一办法。
    hidden = uncovered_call_sites()
    if hidden:
        print(f"✗ 发现 {len(hidden)} 个前端调用点本检查**解析不到**"
              f"（既未对账也未报未解析 —— 假绿灯风险）：\n    " + "\n    ".join(hidden[:20]),
              file=sys.stderr)
        print("  请在 CALL_RE / UNRESOLVED_CALL_RE 中补上这种写法后重跑。", file=sys.stderr)
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

    if param_drift:
        print("\n✗ 前端发送了后端签名**不认**的查询参数（FastAPI 静默忽略 → 200 + 默认值）：")
        for k, unknown in sorted(param_drift.items()):
            print(f"    {k}  未知参数: {', '.join(sorted(unknown))}   ← {', '.join(frontend[k])}")
            if be_query.get(k):
                print(f"      后端可认: {', '.join(sorted(be_query[k]))}")
        print("\nAPI PARAM DRIFT DETECTED")
        return 1

    print("\n✓ 前端调用的每个端点都能在后端契约里找到")
    print(f"✓ 参数级对账通过（前端带查询参数的端点 {len(fe_query)} 个，全部命中后端签名）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
