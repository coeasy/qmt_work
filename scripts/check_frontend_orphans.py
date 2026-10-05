#!/usr/bin/env python3
"""前端孤儿导出门禁：`frontend-next/src` 里「导出了但没有任何消费者」的符号。

为什么要它
----------
后端早有 `backend/scripts/audit_orphan_modules.py`（零引用模块），前端却一直没有
对应物 —— 于是「导出了没人用」这件事只能靠人肉审计发现。R26 实测：一次扫描就找出
**9 个**零引用导出，其中既有真漏接（`fmtSigned` —— 涨跌额一直在用不带符号的
`fmtPrice()` 渲染），也有说谎的 docstring（`__dashPrice` 写着「供测试断言使用」，
实测 src/tests 零引用），还有重复真源（`DEFAULT_PAGE` 声明了没用，而 `"dashboard"`
字面量在 store 里又硬写两次）。这类问题不会让程序崩，但会让「读代码得到的结论」
与「程序实际行为」长期分叉。

判据（保守，宁少报不误报）
--------------------------
- 只看 `frontend-next/src` 下的**顶层 export**（function/const/let/class/interface/
  type/enum），只看 `src`，不把 `tests` 的导出算进来；
- 「有消费者」= 该名字在 **`src` 或 `tests` 任一处**出现过（**含测试**：
  测试也是消费者，不把测试算进来会制造假告警）；
- 出现次数 ≤ 1（即只有声明那一处）⇒ 零引用 ⇒ 需裁定；
- 裁定表 `ADJUDICATED` 每一条必须写明**为什么**它零引用也是活的，
  且必须**仍然零引用** —— 一旦有了真实消费者就报 stale，逼人删条目
  （与 `check_capability_coverage` 的「豁免反腐烂」同一纪律）。

用法：
    cd backend && .venv/Scripts/python.exe ../scripts/check_frontend_orphans.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend-next" / "src"
TESTS = ROOT / "frontend-next" / "tests"

SKIP_DIRS = {"node_modules", "dist", "dist-electron", ".tmp-test"}

#: 顶层导出声明。
_RE_DECL = re.compile(
    r"^export\s+(?:async\s+)?(?:function|const|let|class|interface|type|enum)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.M,
)

#: 显式裁定：`src 相对路径::符号名` → 为什么它零引用也是活的。
#: ⚠️ 每一条都必须能独立站住；**将来补了真实消费者，就删掉这一条**（留着会报 stale）。
ADJUDICATED: dict[str, str] = {
    "services/http.ts::setApiKey": (
        "零调用但合法（R25 已留档）：后端对 loopback 请求免鉴权、桌面端前后端同机，"
        "故全程不带 Key 也能用。保留是为 ① 前后端不同机的非 loopback 部署 "
        "② 排障时在控制台临时写 Key —— 刻意不做 UI 入口。"
    ),
    "domains/_shared/PagePlaceholder.tsx::makePlaceholder": (
        "既定扩展点而非缺陷：它是 routes.tsx 中 status:\"planned\" 机制的组成部分"
        "（MenuBar 为 planned 渲染「待实现」角标）。当前 43 个页面全为 done/partial，"
        "故暂无人调用；除非同时决定移除 planned 机制，否则不得当孤儿删除。"
    ),
    "shared/types.ts::AccountAggregate": (
        "载荷类型：对应 GET /account/aggregate，该端点按 V11 决策属「分析/Agent 向」"
        "（见 scripts/check_capability_coverage.py 豁免表），UI 不建域。类型留作接口文档。"
    ),
    "shared/types.ts::SlippageReport": (
        "载荷类型：对应 GET /account/slippage（分析/Agent 向，UI 不建域，已在"
        "check_capability_coverage 豁免表登记）。类型留作接口文档。"
    ),
    "shared/types.ts::BoardInfo": (
        "载荷类型：对应 /market/board* 板块族端点的响应形状；UI 当前只用其中部分字段"
        "（内联类型），完整形状留作契约文档，避免接线时凭记忆写错。"
    ),
    "shared/types.ts::NetValuePoint": (
        "载荷类型：净值曲线点位（回测/模拟盘净值序列），当前 UI 用 ECharts 直接吃数组，"
        "未走该类型。保留为后端响应契约的显式文档。"
    ),
}


def _scan_files() -> list[Path]:
    out: list[Path] = []
    for base in (SRC, TESTS):
        if not base.is_dir():
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if name.endswith((".ts", ".tsx")):
                    out.append(Path(dirpath) / name)
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main() -> int:
    files = _scan_files()
    if len(files) < 50:
        print(f"!! 扫描面过小（{len(files)} 个文件）—— 路径或过滤器写错了？")
        return 1

    texts = {p: _read(p) for p in files}
    blob = "\n".join(texts.values())

    orphans: dict[str, tuple[str, int]] = {}   # key -> (相对路径, 展开出现次数)
    declared: set[str] = set()
    for p, t in texts.items():
        # 只把 src 下的声明当作候选（tests 里声明的辅助函数不算产品面）
        try:
            rel_to_src = p.relative_to(SRC)
        except ValueError:
            continue
        for name in sorted(set(_RE_DECL.findall(t))):
            key = f"{rel_to_src.as_posix()}::{name}"
            declared.add(key)
            total = len(re.findall(r"\b" + re.escape(name) + r"\b", blob))
            if total <= 1:
                orphans[key] = (rel_to_src.as_posix(), total)

    problems: list[str] = []

    # 1) 未裁定的零引用导出
    unadjudicated = sorted(k for k in orphans if k not in ADJUDICATED)
    for k in unadjudicated:
        problems.append(
            f"零引用导出未裁定：{k}（在 src 与 tests 中都没有消费者）"
        )

    # 2) 裁定表反腐烂：条目必须仍存在、仍零引用、理由足够具体
    for key, reason in sorted(ADJUDICATED.items()):
        if key not in declared:
            problems.append(
                f"裁定表过期：{key} 已不存在（符号被改名/删除）⇒ 删掉该条目"
            )
            continue
        if key not in orphans:
            problems.append(
                f"裁定表过期：{key} 现在已有消费者 ⇒ 删掉该条目（留着会让清单腐烂）"
            )
        if len(reason.strip()) < 12:
            problems.append(f"裁定表理由过短：{key}")

    print(f"前端导出扫描：{len(files)} 个文件 / 声明 {len(declared)} 个符号；"
          f"零引用 {len(orphans)} 个（已裁定 {len(orphans) - len(unadjudicated)}）")
    if orphans:
        print("\n零引用导出清单：")
        for k in sorted(orphans):
            flag = "已裁定" if k in ADJUDICATED else "★未裁定"
            print(f"  [{flag}] {k}")
    if problems:
        print("\n发现以下问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("frontend-orphan gate: OK（零引用导出全部已裁定，无过期条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
