#!/usr/bin/env python3
"""Static gates（AST 全量扫描，无正则糊弄）：

**Gate 1 — 真实下单入口不得绕过 ExecutionService。**
- 扫描 ``app / gateway / tools / engines / mcp_server / connectors``；
- **白名单语义**：任何 ``*.place_order(...)`` 直调均视为绕过执行链，除非 receiver
  本身就是执行服务（receiver 文本含 "execution"，如 ``ExecutionService(...)`` /
  ``self._execution`` / ``get_execution_service()``）；
- 旧启发式（receiver ∈ {gateway, adapter} 或以 .gateway 结尾）会漏掉
  ``self._adapter.place_order`` 等形态，已废弃。

**Gate 2 — ``app/routes/`` 不得直连 DB（P0-1）。**
路由层的职责是「参数校验 + 编排」；SQL 一旦混进路由：

1. **无法单测** —— 要验一条 SQL 必须先把整个 FastAPI 路由与上下文搭起来；
2. **无门禁** —— 拼串写错（漏 ``WHERE``、字段名漂移）没有任何静态检查会发现；
3. **职责漂移** —— 同一张表的读写散落在路由与引擎里，改表结构要全局 grep。

因此 SQL 必须收敛到 ``core/db`` 的仓储方法或 ``app/services/*_store.py``。
本门禁拦三类形态（都是本项目真实出现过的）：

- ``ctx.db.query/query_one/execute/executemany/executemany_in_txn``（含 ``self.db`` 等后缀形态）；
- ``get_db()`` 模块级访问器 —— 它**绕过 ctx 注入**，比 ``ctx.db`` 更隐蔽
  （曾出现在 ``market.py::dataset_snapshots``）；
- ``sqlite3.connect(...)`` 与**裸 SQL 字面量** —— 路由自己开连接读库
  （曾出现在 ``config.py::_count_cold_rows``，读的是独立冷仓文件）。

★ **不拦** ``db.insert`` / ``db.upsert`` / ``db.audit`` / ``db.verify_audit_chain``：
它们接收的是「表名 + 字典」而非 SQL 字符串，属于结构化仓储 API，不是裸 SQL。

**Gate 3 — 内核模块不得出现 ``app.*`` 第二真源（P0-2）。**
``app/*`` 与 ``core/*`` 曾长期并存两套内核（``app.db`` / ``app.config`` /
``app.state`` …），引用会在两个名字间漂移；清 shim 之后**迁移已归零**，
但没有任何机制阻止新代码再写回 ``from app.db import ...``。
本门禁把这条线**冻结**在 0：

- ① **影子模块**：``app/<core 里同名的模块>.py`` 一旦出现即违规 —— 那正是第二真源的物理形态；
- ② **引用**：任何 ``from app.<内核名> import ...`` / ``import app.<内核名>`` 均违规。

内核模块清单**从 ``core/*.py`` 动态推导**，不维护手写列表 ——
``core/`` 里新增一个模块，门禁自动开始拦它的同名 ``app.*`` 引用。

**Gate 4 — 单文件体积上限（P1-1）。**
超过 50KB 的源文件几乎总是「多个关注点挤在一个文件里」的信号：
``registry.py``（64KB）曾同时承载连接治理、行情、K 线、板块；
``market.py``（54KB）曾把单标的行情、缓存运维、多维聚合混在一起。
拆分之后设上限，防止再次膨胀。

判据：``backend`` 下**非测试** ``*.py`` 无 > 50KB。
测试文件（``tests/``）**不在范围内** —— 测试的「大」是断言条数多，
与「关注点混杂」不是同一个问题，强行拆只会增加 import 噪声。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SCAN_ROOTS = tuple(BACKEND / d for d in
                   ("app", "gateway", "tools", "engines", "mcp_server", "connectors"))
SERVICE = BACKEND / "gateway" / "execution.py"
ROUTES = BACKEND / "app" / "routes"
CORE_DIR = BACKEND / "core"

#: Gate 4：单文件体积上限（字节）
MAX_SOURCE_BYTES = 50 * 1024

#: 内核模块名（``core/*.py``），动态推导。见模块 docstring Gate 3。
CORE_MODULES = frozenset(
    p.stem for p in CORE_DIR.glob("*.py") if p.stem != "__init__"
)

#: Gate 3/4 的扫描范围排除项：运行时 / 构建产物 / 打包输出 / 缓存 / 测试。
SKIP_PARTS = frozenset({"runtimes", "dist", "build", "__pycache__",
                        "node_modules", ".venv", "output", "tests"})

#: DB 访问方法名。结构化 API（``insert`` / ``upsert`` / ``audit`` /
#: ``verify_audit_chain``）**刻意不在其中** —— 它们不接 SQL 字符串。
DB_METHODS = frozenset({"query", "query_one", "execute", "executemany",
                        "executemany_in_txn"})

#: 裸 SQL 字面量特征。刻意写得**紧**：
#: - 关键字后必须跟标识符/``*``/数字，避免把中文提示语误判成 SQL
#:   （``"update 失败"`` 不该命中 ``update ... set``）；
#: - 标识符字符集限定 ASCII，避免 ``\w`` 匹配到中文字符。
SQL_RE = re.compile(
    r"^\s*(?:select\s+[A-Za-z_*(0-9]"
    r"|insert\s+into\s+[A-Za-z_]"
    r"|update\s+[A-Za-z_]+\s+set\b"
    r"|delete\s+from\s+[A-Za-z_]"
    r"|create\s+table"
    r"|drop\s+table"
    r"|alter\s+table"
    r"|pragma\s+[A-Za-z_]"
    r"|with\s+[A-Za-z_]+\s+as\s*\()",
    re.IGNORECASE,
)


def _receiver_allowed(receiver: str) -> bool:
    """仅执行服务自身（含 execution 命名）允许调用 place_order。"""
    return "execution" in receiver.lower()


def _is_db_receiver(receiver: str) -> bool:
    """判断一个表达式是否「像数据库句柄」。

    覆盖本项目真实出现过的形态：
    - 后缀 ``.db``（``ctx.db`` / ``self.db`` / ``state.db``）；
    - 裸 ``db`` / ``conn`` / ``cur`` / ``cursor``（路由自开的连接）；
    - ``get_db()`` 模块级访问器 —— 绕过 ctx 注入的隐蔽形态。
    """
    low = receiver.strip().lower()
    if low in ("db", "conn", "cur", "cursor"):
        return True
    if low.endswith(".db") or low.endswith("['db']"):
        return True
    return low.startswith("get_db(") or "get_db()" in low


def _gate_place_order() -> list[str]:
    """Gate 1：真实下单入口不得绕过 ExecutionService。"""
    if "class ExecutionService" not in SERVICE.read_text(encoding="utf-8"):
        return ["ExecutionService is missing"]
    violations: list[str] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path == SERVICE:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                violations.append(f"cannot parse {path.relative_to(ROOT)}: {exc}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "place_order":
                    continue
                receiver = ast.unparse(node.func.value)
                if not _receiver_allowed(receiver):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {receiver}.place_order")
    return violations


def _gate_routes_no_db() -> list[str]:
    """Gate 2：``app/routes/`` 不得直连 DB。"""
    violations: list[str] = []
    files = sorted(ROUTES.rglob("*.py"))
    # ★ 自检：扫不到文件说明路径写错 ⇒ 门禁会「永远绿」。宁可报错也不静默放行。
    if not files:
        return [f"{ROUTES.relative_to(ROOT)} 下没有扫描到任何 .py —— 门禁失效"]
    for path in files:
        rel = path.relative_to(ROOT)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{rel}: 无法解析：{exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    recv = ast.unparse(func.value)
                    if func.attr in DB_METHODS and _is_db_receiver(recv):
                        violations.append(
                            f"{rel}:{node.lineno}: {recv}.{func.attr}(...) 直连 DB")
                    if func.attr == "connect" and "sqlite3" in recv.lower():
                        violations.append(
                            f"{rel}:{node.lineno}: {recv}.connect(...) 直连 DB")
                # 裸 SQL 字面量作为实参（不论 receiver 是谁都能抓到）
                args = list(node.args) + [kw.value for kw in node.keywords]
                for arg in args:
                    for const in ast.walk(arg):
                        if not isinstance(const, ast.Constant):
                            continue
                        if not isinstance(const.value, str):
                            continue
                        if SQL_RE.match(const.value):
                            head = " ".join(const.value.split())[:48]
                            violations.append(
                                f"{rel}:{const.lineno}: 出现裸 SQL 字面量「{head}」")
    return violations


def _iter_backend_py():
    """backend 下的全部 .py，排除运行时/构建产物/打包输出/缓存。"""
    for path in BACKEND.rglob("*.py"):
        if set(path.relative_to(BACKEND).parts) & SKIP_PARTS:
            continue
        yield path


def _is_app_kernel(dotted: str) -> bool:
    """``app.<内核模块>``（含其子模块）即为第二真源引用。"""
    parts = dotted.split(".")
    return len(parts) >= 2 and parts[0] == "app" and parts[1] in CORE_MODULES


def _gate_kernel_namespace() -> list[str]:
    """Gate 3：内核模块只能从 ``core.*`` 引用。"""
    violations: list[str] = []
    # ★ 自检：推导不出内核清单说明路径写错 ⇒ 门禁会「永远绿」。
    if not CORE_MODULES:
        return [f"{CORE_DIR.relative_to(ROOT)} 下没有扫描到任何模块 —— 门禁失效"]

    # ① 影子模块：app/<内核名>.py 的物理存在
    for name in sorted(CORE_MODULES):
        shadow = BACKEND / "app" / f"{name}.py"
        if shadow.exists():
            violations.append(
                f"{shadow.relative_to(ROOT)}: 影子模块（真源在 core/{name}.py）")

    # ② 引用：from app.<内核名> ... / import app.<内核名>
    for path in _iter_backend_py():
        rel = path.relative_to(ROOT)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{rel}: 无法解析：{exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if _is_app_kernel(mod):
                    target = "core." + mod.split(".", 1)[1]
                    violations.append(f"{rel}:{node.lineno}: from {mod} → 应改为 {target}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_app_kernel(alias.name):
                        target = "core." + alias.name.split(".", 1)[1]
                        violations.append(
                            f"{rel}:{node.lineno}: import {alias.name} → 应改为 {target}")
    return violations


def _gate_file_size() -> list[str]:
    """Gate 4：非测试 ``*.py`` 不得 > 50KB。"""
    violations: list[str] = []
    scanned = 0
    for path in _iter_backend_py():
        scanned += 1
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            violations.append(
                f"{path.relative_to(ROOT)}: {size/1024:.1f}KB "
                f"(> {MAX_SOURCE_BYTES/1024:.0f}KB，请按职责拆分)")
    # ★ 自检：扫不到文件说明路径/排除项写错 ⇒ 门禁会「永远绿」。
    if scanned < 100:
        return [f"只扫描到 {scanned} 个 .py —— 门禁失效"]
    return violations


def main() -> int:
    place = _gate_place_order()
    routes = _gate_routes_no_db()
    kernel = _gate_kernel_namespace()
    size = _gate_file_size()
    if place:
        print("direct real order entry detected; route through ExecutionService:",
              file=sys.stderr)
        print("\n".join(place), file=sys.stderr)
    if routes:
        print("bare DB access detected in app/routes; move SQL into "
              "core/db or app/services/*_store.py:", file=sys.stderr)
        print("\n".join(routes), file=sys.stderr)
    if kernel:
        print("app.* kernel namespace re-appeared; import from core.* instead:",
              file=sys.stderr)
        print("\n".join(kernel), file=sys.stderr)
    if size:
        print("oversized source file(s); split by responsibility:", file=sys.stderr)
        print("\n".join(size), file=sys.stderr)
    if place or routes or kernel or size:
        return 1
    print("execution architecture gate: OK (place_order whitelist + routes-no-db + "
          "core-only kernel namespace + file-size cap, AST full scan)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
