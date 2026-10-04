# -*- coding: utf-8 -*-
"""REST 接口载荷完整性审计：把「哪些接口字段不完善」变成可量化清单。

与 ``audit_data_completeness.py`` 的分工
----------------------------------------
- ``audit_data_completeness.py`` 审计 **datasource 门面**（easy_tdx 七族），回答
  「数据源给没给」。
- 本脚本审计 **REST 接口载荷**（用户真正看到的那一层），回答「**接口吐没吐**」。

两者必须分开，因为门面非空 ≠ 接口非空。v0.4.7 就栽在这条缝上：``get_quote`` 的
字段映射写了两份，界面路径正常，而 ``stock-info`` 面板走 ``get_instrument_detail``
按契约名 ``getattr`` 拿到 ``None`` —— **门面全绿、接口全空**。故本脚本存在的意义
就是把「门面 → 序列化 → 载荷」这最后一跳也纳入量化。

用法（后端 venv；进程内 TestClient，需可访问 TDX 公共行情）::

    cd backend && .venv/Scripts/python.exe scripts/audit_api_payloads.py
    cd backend && .venv/Scripts/python.exe scripts/audit_api_payloads.py --json out.json

判定口径
--------
对每个端点的 ``data`` 载荷做**递归叶子路径**展开（list 内元素按路径聚合），
每片叶子先分「缺失」与「结构为空」两类 —— 这两者的**含义完全不同**：

- **缺失**（``null`` / 空串）= 该有的值没给 ⇒ 恒缺即为真缺陷候选；
- **结构为空**（``{}`` / ``[]``）= 值本身就是「没有」的合法表示
  （如无参数指标的 ``params={}``、未指定复权时的空档）；把它们算成缺陷会淹没真信号。

状态：
- ``OK``      全部叶子有值
- ``STRUCT``  只有容器为空（信息性，不计缺陷）
- ``EMPTY``   全部叶子**缺失**（恒空）
- ``PARTIAL`` 部分缺失
- ``SKIP``    端点未就绪（``code != 0``：离线无源 / 无券商，属正常降级）

``EMPTY``/``PARTIAL`` 还必须过 ``KNOWN`` 规则表分类：
**协议限制**（诚实降级）/ **设计上可选**（仅在必要时给值）/ **环境相关**
（无券商、未跑同步、非交易时段）/ **仅脚本·Agent 消费**。
未被规则命中且**以缺失为主**的才是「真缺陷候选」，脚本以非零码退出。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

#: 需要审计的端点（``path``, ``固定查询参数``）。查询参数未给全时由路由签名补齐。
#: 只挑「只需行情源、不需券商」的端点 —— 券商类端点离线必 503（诚实降级），
#: 审计它们只会得到一堆 SKIP，没有信息量。
TARGETS: list[tuple[str, dict]] = [
    ("/market/overview", {}),
    ("/market/breadth", {}),
    ("/market/indices", {}),
    ("/market/quote", {"code": "600519.SH"}),
    ("/market/kline", {"code": "600519.SH", "period": "1d", "count": 60}),
    ("/market/minutes", {"code": "600519.SH"}),
    ("/market/ticks", {"code": "600519.SH"}),
    ("/market/stock-info", {"code": "600519.SH"}),
    ("/market/boards", {}),
    ("/market/moneyflow", {}),
    ("/market/capital", {}),
    ("/market/rotation", {}),
    ("/market/limitup", {}),
    ("/market/etfs", {}),
    ("/market/indicators", {}),
    ("/market/periods", {}),
    ("/market/sources", {}),
    ("/market/providers", {}),
    ("/market/session", {}),
    ("/market/coverage", {}),
    ("/reference/calendar", {}),
    ("/reference/sectors", {}),
    ("/reference/financial", {"code": "600519.SH"}),
    ("/capabilities", {}),
    ("/capabilities/summary", {}),
    ("/platform/status", {}),
]

#: 规则表：``(端点, 叶子后缀, 归类)``。端点 ``"*"`` = 任意端点。
#: 匹配：``leaf == suffix`` / ``leaf.endswith("." + suffix)`` / ``leaf.startswith(suffix + ".")``。
#:
#: ★ **顺序敏感：先匹配先赢**。所以"更具体"的规则必须排在"更宽"的规则之前
#:   （例如 ``items.pb`` 必须排在展开产物规则 ``items`` 之前，否则会被吞掉）。
#: 每一条都必须能说清「为什么它空着是对的」—— 说不出理由的就不该进这张表。
KNOWN: list[tuple[str, str, str]] = [
    # ---- 协议限制（诚实降级）----
    ("*", "ask", "协议限制：MAC 无五档盘口"),
    ("*", "bid", "协议限制：MAC 无五档盘口"),
    ("*", "ask_vol", "协议限制：MAC 无五档盘口"),
    ("*", "bid_vol", "协议限制：MAC 无五档盘口"),
    ("*", "asks", "协议限制：MAC 无五档盘口"),
    ("*", "bids", "协议限制：MAC 无五档盘口"),
    ("*", "sum_buy_vol", "协议限制：MAC 无五档盘口"),
    ("*", "sum_sell_vol", "协议限制：MAC 无五档盘口"),
    # 指数快照（腾讯源）不提供 PB；个股快照有 PB，故只对 /market/indices 生效
    ("/market/indices", "pb", "协议限制：指数快照无 PB"),
    # 指数没有涨跌停价；源给不出就是给不出（有则照给，故是 PARTIAL 而非 EMPTY）
    ("/market/indices", "high_limit", "协议限制：指数无涨跌停价"),
    ("/market/indices", "low_limit", "协议限制：指数无涨跌停价"),

    # ---- 设计上可选：字段存在，只在「有必要时」才给值 ----
    ("/market/kline", "adjust", "设计上可选：空串=不复权（请求未指定时的真实口径）"),
    ("/platform/status", "reasons.screening", "设计上可选：null=没有问题（有问题才给原因）"),
    ("/market/periods", "periods.reason", "设计上可选：仅对被禁用的周期给原因"),
    ("*", "optional_dependency", "设计上可选：仅对可选依赖的源给值"),
    ("*", "requires", "设计上可选：仅对声明了依赖的源给值"),
    # 一条覆盖 items.param_schema 及其全部子树（default / default.scope / …）
    ("/capabilities", "items.param_schema",
     "设计上可选：参数 schema 只在该参数确有该属性时给出"),
    ("/capabilities", "items.summary", "设计上可选：少数不可暴露的端点无摘要"),
    ("/capabilities", "items.tool_name", "设计上可选：不可暴露为 MCP 工具的端点无 tool_name"),
    ("/market/indicators", "items.params", "结构为空：无参指标的 params 就是 {}"),

    # ---- 环境相关：不是缺陷，是「现在没有」 ----
    ("/market/overview", "breadth", "环境相关：全市场聚合需券商或完整扫描"),
    ("/market/overview", "breadth_summary", "环境相关：随 breadth 一起缺席"),
    ("/market/overview", "breadth_trend", "环境相关：随 breadth 一起缺席"),
    ("/market/coverage", "sync.detail", "环境相关：尚未跑过日线同步（last_run 为空）"),

    # ---- 仅脚本 / Agent 消费：前端零引用，但 /platform/status 本身也是 MCP 工具 ----
    ("/platform/status", "execution.active_conn_id",
     "仅 Agent 消费：/platform/status 亦为 MCP 工具"),

    # ---- 展开产物（**必须最后**：宽规则，会把 items.* 全部吃掉）----
    ("/market/indices", "items",
     "展开产物：items 里的 null = 该只指数拉取失败（路由明示「不因一只失败拖垮整条」）"),
]

CATEGORIES = ("协议限制", "设计上可选", "环境相关", "仅 Agent 消费", "结构为空", "展开产物")

_MAX_SAMPLES = 3


def _kind(v) -> str:
    """叶子取值分类：``ok`` / ``struct``（容器为空）/ ``missing``（null 或空串）。"""
    if v is None:
        return "missing"
    if isinstance(v, str):
        return "ok" if v.strip() else "missing"
    if isinstance(v, (list, tuple, dict)):
        return "ok" if len(v) else "struct"
    return "ok"


def _leaves(obj, prefix: str, acc: dict[str, list]) -> None:
    """递归收集叶子路径 → 取值列表（list 元素按同一路径聚合）。"""
    if isinstance(obj, dict):
        if not obj:
            acc.setdefault(prefix or "<empty>", []).append({})
            return
        for k, v in obj.items():
            _leaves(v, f"{prefix}.{k}" if prefix else str(k), acc)
    elif isinstance(obj, list):
        if not obj:
            acc.setdefault(prefix or "<empty>", []).append([])
            return
        for it in obj:
            _leaves(it, prefix, acc)
    else:
        acc.setdefault(prefix or "<value>", []).append(obj)


def _verdict(endpoint: str, leaf: str) -> str:
    """查规则表：返回归类，未命中返回空串（= 真缺陷候选）。**先匹配先赢**。"""
    for ep, suffix, cat in KNOWN:
        if ep != "*" and ep != endpoint:
            continue
        if (leaf == suffix or leaf.endswith("." + suffix)
                or leaf.startswith(suffix + ".")):
            return cat
    return ""


def _audit_payload(tag: str, payload) -> list[dict]:
    acc: dict[str, list] = {}
    _leaves(payload, "", acc)
    issues: list[dict] = []
    lines: list[str] = []
    for path in sorted(acc):
        vals = acc[path]
        kinds = [_kind(v) for v in vals]
        missing = kinds.count("missing")
        struct = kinds.count("struct")
        ok = kinds.count("ok")
        total = len(kinds)
        if missing == 0 and struct == 0:
            continue                      # 全有值
        if ok == 0 and missing == 0:
            st = "STRUCT"
        elif missing == total:
            st = "EMPTY"
        else:
            st = "PARTIAL"
        cat = _verdict(tag, path)
        sample = next((v for v in vals if _kind(v) == "ok"), None)
        if st == "STRUCT" and not cat:
            cat = "结构为空"
        flag = "" if cat else "  ← 需判定"
        lines.append(f"  [{st:7s}] {path:44s} 有值{ok}/缺失{missing}/空容器{struct} "
                     f"样例={str(sample)[:32]!r}{'  ' + cat if cat else flag}")
        issues.append({"endpoint": tag, "leaf": path, "nonempty": ok, "total": total,
                       "missing": missing, "struct": struct, "status": st,
                       "category": cat})
    print(f"\n### {tag}（{len(acc)} 个叶子路径）")
    print("\n".join(lines) if lines else "  （全部叶子有值）")
    return issues


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="REST 载荷字段级空值率审计")
    ap.add_argument("--json", default="", help="把明细写到该 JSON 文件")
    args = ap.parse_args(argv)

    from fastapi.testclient import TestClient

    from app.main import app
    from core.config import settings

    client = TestClient(app)
    client.headers.update({"X-API-Key": settings.api_key})

    issues: list[dict] = []
    skipped: list[str] = []
    # ★ lifespan 停机噪声必须吞掉：TestClient.__exit__ 会把后台任务取消产生的
    #   ``concurrent.futures.CancelledError`` 抛出来（与 ``tests/conftest.py`` 里
    #   ``app_client`` 夹具压制的是同一类噪声）。若不吞，脚本会在**打印完审计结论后**
    #   才崩，退出码丢失、结论看起来像是失败 —— 又一个「结论被别的异常遮住」。
    try:
        with client:
            for path, fixed in TARGETS:
                try:
                    r = client.get("/api/v1" + path, params=fixed or None)
                except Exception as exc:  # noqa: BLE001
                    print(f"\n### {path} 调用异常：{type(exc).__name__}: {exc}")
                    issues.append({"endpoint": path, "leaf": "<EXCEPTION>",
                                   "status": "FAIL", "total": 0, "nonempty": 0,
                                   "missing": 0, "struct": 0, "category": ""})
                    continue
                if r.status_code != 200:
                    skipped.append(f"{path} HTTP {r.status_code}")
                    continue
                body = r.json()
                if not isinstance(body, dict) or body.get("code") != 0:
                    skipped.append(f"{path} code={body.get('code')} "
                                   f"({str(body.get('message'))[:50]})")
                    continue
                issues += _audit_payload(path, body.get("data"))
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001  CancelledError 属 BaseException
        print(f"[warn] app lifespan 停机噪声（已抑制，不影响上方结论）："
              f"{type(exc).__name__}")

    print("\n" + "=" * 62)
    if skipped:
        print("SKIP（未就绪：离线无源 / 无券商 / 非交易时段，属正常降级）：")
        for s in skipped:
            print(f"  - {s}")

    fails = [i for i in issues if i["status"] == "FAIL"]
    candidates = [i for i in issues
                  if i["status"] in ("EMPTY", "PARTIAL") and not i.get("category")]
    structs = [i for i in issues if i["status"] == "STRUCT"]
    known = [i for i in issues if i.get("category")]
    print(f"\n汇总：FAIL {len(fails)} | 真缺陷候选 {len(candidates)} | "
          f"已归类 {len(known)} | 结构为空 {len(structs)} | SKIP {len(skipped)}")
    if known:
        print("已归类明细：")
        for cat in CATEGORIES:
            items = [i for i in known if i["category"] == cat]
            if not items:
                continue
            print(f"  [{cat}] {len(items)} 项")
            for i in items:
                print(f"      {i['endpoint']} · {i['leaf']}")
    if candidates:
        print("\n★ 真缺陷候选（未归类且以缺失为主，必须判定）：")
        for i in candidates:
            print(f"    {i['endpoint']} · {i['leaf']} "
                  f"(缺失 {i['missing']}/{i['total']})")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"issues": issues, "skipped": skipped,
             "summary": {"fail": len(fails), "candidates": len(candidates),
                         "known": len(known), "struct": len(structs),
                         "skip": len(skipped)}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.json}")
    return 1 if (fails or candidates) else 0


if __name__ == "__main__":
    raise SystemExit(main())
