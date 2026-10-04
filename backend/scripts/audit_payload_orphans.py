# -*- coding: utf-8 -*-
"""载荷字段 ↔ 前端消费 对账（TD-31 登记的待办之一的**可执行近似**）。

TD-31 的原文说：载荷字段级对账"成本远超本轮收益"，因为响应体是运行期构造的 dict，
静态推导需要一整套类型标注约定。本脚本走的是**另一条路**：不推导类型，而是拿
``audit_api_payloads.py --json`` 产出的**真实叶子路径快照**当"生产者清单"，
再逐字段去前端源码里数引用：

- 前端 **0 引用** → 可能是**孤儿字段**（后端多算、前端零消费）⇒ 需判定：
  是"仅脚本/Agent 消费"（应登记）还是真该删；
- 前端有引用 → 说明这条载荷有消费者，链路是通的。

★ 裁定表**只有一张**：零引用字段先拿去问 ``audit_api_payloads.KNOWN``
  （``(端点, 叶子后缀, 归类)``）。命中即「已判定」并直接打印归类，只有
  **表里没有**的才进「待处理」。为什么必须共用：两个工具各持一份台账时，
  ``sum_buy_vol`` 会在 A 工具里被判定为「协议限制：MAC 无五档盘口」、却仍然
  每轮在 B 工具里被列成"孤儿候选"—— 同一件事说两遍，读者两次都要重新判一遍，
  最后就没有人再看这份清单（TD-33 同族的"报告腐烂"）。

这不是硬门禁（无法区分"仅 Agent 消费"），而是一份**可复核的清单** ——
把"载荷字段不在任何门禁视野里"变成"看得见、逐条判定"。

用法：
    cd backend && .venv/Scripts/python.exe scripts/audit_payload_orphans.py \
        --payload ../logs/_r2_api_payload.json --frontend ../frontend-next/src
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                                    # 共用裁定表；拿不到就**明说**，绝不静默降级
    from audit_api_payloads import _verdict
    _VERDICT_OK = True
except Exception as _exc:               # noqa: BLE001
    _VERDICT_OK = False
    _VERDICT_ERR = repr(_exc)

#: 这些字段名太通用（`type`/`name`/`code`…），在前端到处出现，按名字统计没有信息量，
#: 会制造大量假"有消费"。显式排除，避免这份清单变成噪声。
_TOO_GENERIC = {
    "code", "name", "type", "kind", "value", "data", "id", "ts", "time", "date",
    "text", "label", "unit", "count", "total", "index", "key", "items", "rows",
    "size", "length", "order", "status", "source", "open", "high", "low",
    "close", "price", "volume", "amount", "value", "start", "end", "min", "max",
}


def _load_frontend_blob(root: Path) -> str:
    parts = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in (".ts", ".tsx"):
            try:
                parts.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="载荷字段 → 前端引用数对账")
    ap.add_argument("--payload", required=True, help="audit_api_payloads.py --json 产物")
    ap.add_argument("--frontend", default="../frontend-next/src")
    ap.add_argument("--include-generic", action="store_true",
                    help="连通用字段名一起统计（默认排除，噪声很大）")
    ap.add_argument("--strict", action="store_true",
                    help="存在「待处理」零引用字段时非零退出（供人工收口时用）")
    args = ap.parse_args(argv)

    issues = json.loads(Path(args.payload).read_text(encoding="utf-8"))["issues"]
    blob = _load_frontend_blob(Path(args.frontend))
    if not blob:
        print(f"!! 前端源码为空：{args.frontend}")
        return 1
    if not _VERDICT_OK:
        print(f"!! 裁定表不可用（{_VERDICT_ERR}）—— 本清单无法区分"
              "「已判定」与「待处理」，结果仅供参考")

    # 按字段名归类（同一字段可能出现在多个端点）：seg → [(端点, 完整叶子), ...]
    by_field: dict[str, list[tuple[str, str]]] = {}
    for it in issues:
        leaf = str(it.get("leaf") or "")
        if not leaf or leaf.startswith("<"):
            continue
        seg = leaf.rsplit(".", 1)[-1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", seg):
            continue
        if not args.include_generic and seg in _TOO_GENERIC:
            continue
        ep = str(it.get("endpoint"))
        by_field.setdefault(seg, []).append((ep, leaf))
        # ★ 也把**首段**算进来（注释原意：`breadth_summary.xxx` 归到 `breadth_summary`）。
        #   原实现误用末段 seg，于是 `leaf.startswith(seg + ".")` 恒为 False ——
        #   一条**永不执行的死分支**：一个专找孤儿逻辑的工具自己藏着孤儿逻辑。
        first = leaf.split(".", 1)[0]
        if (first != seg and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", first)
                and (args.include_generic or first not in _TOO_GENERIC)):
            by_field.setdefault(first, []).append((ep, leaf))

    pending: list[tuple[str, list[str]]] = []
    adjudicated: list[tuple[str, str, list[str]]] = []
    consumed = 0
    for field, pairs in sorted(by_field.items()):
        # 词边界匹配，避免 `note` 命中 `notebook`
        if re.search(r"\b" + re.escape(field) + r"\b", blob):
            consumed += 1
            continue
        eps = sorted({e for e, _ in pairs})
        cats = [_verdict(e, leaf) for e, leaf in pairs] if _VERDICT_OK else []
        if _VERDICT_OK and cats and all(cats):
            adjudicated.append((field, cats[0], eps))
        else:
            pending.append((field, eps))

    total_orphan = len(pending) + len(adjudicated)
    print(f"=== 载荷字段（去通用名后）共 {len(by_field)} 个："
          f"前端有引用 {consumed}，零引用 {total_orphan} ===")
    print(f"    零引用中：已判定 {len(adjudicated)}，**待处理 {len(pending)}**")

    if adjudicated:
        print("\n[已判定] 命中 audit_api_payloads.KNOWN（不再需要逐条重判）:")
        for field, cat, eps in adjudicated:
            print(f"  {field:34s} {cat}  › {', '.join(eps[:3])}"
                  f"{' …' if len(eps) > 3 else ''}")

    if pending:
        print("\n★ [待处理] 零引用且**裁定表里没有**（需判定「仅脚本/Agent 消费」还是应删）:")
        for field, eps in pending:
            print(f"  {field:34s} 出现于: {', '.join(eps[:3])}"
                  f"{' …' if len(eps) > 3 else ''}")
    else:
        print("\n[OK] 待处理零引用字段：0 —— 所有零引用字段都已在裁定表内说明理由")

    if args.strict and pending:
        print(f"\n[FAIL] --strict：仍有 {len(pending)} 个待处理零引用字段")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
