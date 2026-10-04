"""R1 数据完整性审计：easy_tdx 门面七族能力的字段级空值率扫描。

把「哪些数据不完善」变成可量化清单（字段非空率），而不是靠猜。

用法（后端 venv，需可访问 TDX 公共行情）：
    cd backend && .venv/Scripts/python.exe scripts/audit_data_completeness.py

输出三类状态：
- OK      非空率 = 100%
- PARTIAL 0 < 非空率 < 100%（部分缺失，需判定是数据缺口还是协议限制）
- EMPTY   非空率 = 0%（恒空：协议不支持的诚实降级，或真缺陷）

判定 EMPTY 时必须区分「协议限制的诚实降级」与「真缺陷」——协议限制项已在
datasource/tdx_transport.py 顶部 docstring 登记（MAC 无五档、北交所无行情、
批量快照无内外盘）。
"""
from __future__ import annotations

import json
import sys
from datetime import date as _date
from datetime import timedelta as _td
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datasource.tdx_transport import EasyTdxClient, active_backend  # noqa: E402

SAMPLES = ["sh600519", "sz000001", "sh688981", "sz300750"]  # 沪主板/深主板/科创板/创业板

# 已知的协议限制（诚实降级），审计中不视为缺陷。
# 键为字段名（跨调用形态通用），另设 _KNOWN_EMPTY_DATASETS 登记整表为空的场景。
KNOWN_PROTOCOL_LIMITS = {
    # easy_tdx 走 MAC 快照接口，协议本身不提供五档盘口（通达信需专用盘口接口）
    "buy_levels", "sell_levels", "sum_buy_vol", "sum_sell_vol",
    # MAC 批量快照无内外盘（仅单标的 symbol_info 富化路径可取）
    "inside_dish", "outer_disc",
    # MAC 快照无自由流通股本（只有总股本/流通股本），选股用流通股本兜底
    "free_float_shares",
}

# 整表为空但属预期的场景（时间相关 / 后台任务未就绪），非缺陷
KNOWN_EMPTY_DATASETS = {
    "minutes.history",       # 历史分时需逐日请求，单次调用可能无匹配区间
    "trades.today",          # 逐笔成交：非交易时段或无撮合时无数据
}


def _rows_of(x) -> list[dict]:
    """统一成 list[dict]（对象 / dict / DataFrame / NamedTuple 多态）。"""
    if isinstance(x, dict):
        return [x]
    if isinstance(x, (list, tuple)):
        out = []
        for r in x:
            if isinstance(r, dict):
                out.append(dict(r))
            elif hasattr(r, "_asdict") and callable(getattr(r, "_asdict")):
                out.append(dict(r._asdict()))
            elif hasattr(r, "__dict__"):
                out.append({k: v for k, v in vars(r).items()})
            else:
                out.append({"_value": r})
        return out
    if hasattr(x, "to_dict") and callable(getattr(x, "to_dict")):
        try:
            return x.to_dict("records")
        except Exception:  # noqa: BLE001
            return []
    return []


def _unwrap(x) -> list[dict]:
    """按门面返回包装拆包（points / rows / records / tables / topics）。"""
    if x is None:
        return []
    for attr in ("points", "rows", "records", "topics"):
        if hasattr(x, attr):
            return _rows_of(getattr(x, attr))
    if hasattr(x, "tables"):
        out: list[dict] = []
        for t in (getattr(x, "tables") or ()):
            if hasattr(t, "rows"):
                out.extend(_rows_of(t.rows))
        return out
    return _rows_of(x)


def _nonempty(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if isinstance(v, (list, tuple, dict)) and len(v) == 0:
        return False
    return True


def _audit_rows(tag: str, rows: list[dict]) -> list[dict]:
    """统计该族所有字段的非空率，打印非 OK 项。"""
    if not rows:
        known = tag in KNOWN_EMPTY_DATASETS
        print(f"\n### {tag}: 0 行（无数据）"
              f"{'（预期：时间相关/后台未就绪）' if known else '（需判定）'}")
        return [{"family": tag, "field": "<EMPTY_DATASET>", "total": 0,
                 "nonempty": 0, "rate": 0.0, "status": "EMPTY",
                 "known_limit": known}]
    keys = sorted({k for r in rows for k in r})
    print(f"\n### {tag}（{len(rows)} 行）")
    issues = []
    for k in keys:
        vals = [r.get(k) for r in rows]
        n = sum(1 for v in vals if _nonempty(v))
        rate = n / len(vals)
        st = "OK" if rate == 1.0 else ("EMPTY" if n == 0 else "PARTIAL")
        if st == "OK":
            continue
        sample = vals[0] if _nonempty(vals[0]) else None
        is_known = k in KNOWN_PROTOCOL_LIMITS
        flag = "" if is_known else "  ← 需判定"
        print(f"  [{st:7s}] {k:22s} 非空 {n}/{len(vals)} 样例={sample!r}{flag}")
        issues.append({"family": tag, "field": k, "nonempty": n, "total": len(vals),
                       "rate": round(rate, 3), "status": st,
                       "known_limit": is_known})
    if not issues:
        print("  （全部字段非空率 100%）")
    return issues


def main() -> int:
    print(f"后端: {active_backend()}")
    cli = EasyTdxClient()
    cli.connect()
    issues: list[dict] = []
    codes = [c for c in SAMPLES if not c.startswith("bj")]

    def _check(tag: str, fn):
        nonlocal issues          # 闭包内修改外层列表：+= 是赋值，必须声明
        try:
            issues += _audit_rows(tag, _unwrap(fn()))
        except Exception as e:  # noqa: BLE001
            print(f"\n### {tag} FAIL: {type(e).__name__}: {e}")
            issues.append({"family": tag, "field": "<EXCEPTION>", "total": 0,
                           "nonempty": 0, "rate": 0.0, "status": "FAIL"})

    # 1. 快照：批量路径（内外盘按设计缺席）+ 单标的路径（symbol_info enrich）
    _check("quotes.get_snapshots[批量]", lambda: cli.quotes.get_snapshots(codes))
    _check("quotes.get_snapshots[单标的]", lambda: cli.quotes.get_snapshots(["sh600519"]))

    # 2. K 线
    for period in ("1d", "1m"):
        _check(f"bars.get({period})",
               (lambda p=period: getattr(cli.bars.get("sh600519", p, 60, adjust=""),
                                         "bars", None)))

    # 3. 分时 / 逐笔
    _check("minutes.today", lambda: cli.minutes.today("sh600519"))
    _check("minutes.history",
           lambda: cli.minutes.history("sh600519", _date.today() - _td(days=7)))
    _check("trades.today", lambda: cli.trades.today("sh600519"))

    # 4. helpers
    _check("helpers.stock_profile_table", lambda: cli.helpers.stock_profile_table(codes))
    _check("helpers.daily_shares", lambda: cli.helpers.daily_shares(codes))
    _check("helpers.daily_price_limits", lambda: cli.helpers.daily_price_limits(codes))
    _check("helpers.stock_topics", lambda: cli.helpers.stock_topics("600519.SH"))

    # 5. f10
    _check("f10.stock_score", lambda: cli.f10.stock_score("600519.SH"))

    # 6. 清单（ETF 依赖后台清扫缓存，空列表 = 本机会话尚未跑完）
    for mk in ("sh", "sz"):
        _check(f"codes.all({mk})", (lambda m=mk: cli.codes.all(m)))
    _check("codes.all_indices", lambda: cli.codes.all_indices())
    for mk in ("sh", "sz"):
        _check(f"codes.etfs({mk})", (lambda m=mk: cli.codes.etfs(m)))
    print("\n### 说明：codes.etfs 依赖 tdx_etf_list.json 缓存（TTL 7 天）；"
          "空列表 = 本机会话尚未跑完清扫，非缺陷")

    # 7. 北交所诚实降级验证
    print("\n### 北交所诚实降级（应明确异常或空数据，绝不伪造）")
    for tag, fn in (("bars.get(bj)", lambda: cli.bars.get("bj920002", "1d", 20, adjust="")),
                    ("quotes.get_snapshots(bj)", lambda: cli.quotes.get_snapshots(["bj920002"]))):
        try:
            rows = _unwrap(fn())
            print(f"  {tag}: 返回 {len(rows)} 行（0 = 诚实降级）")
        except Exception as e:  # noqa: BLE001
            print(f"  {tag}: 明确异常 {type(e).__name__}: {e} ✓")

    # 汇总
    print("\n" + "=" * 62)
    fails = [i for i in issues if i["status"] == "FAIL"]
    empties = [i for i in issues if i["status"] == "EMPTY" and not i.get("known_limit")]
    partials = [i for i in issues if i["status"] == "PARTIAL"]
    known = [i for i in issues if i["status"] == "EMPTY" and i.get("known_limit")]
    print(f"汇总：FAIL {len(fails)} | 未知恒空 {len(empties)} | 部分缺失 "
          f"{len(partials)} | 已知协议限制 {len(known)}")
    if empties:
        print("★ 未知恒空（真缺陷候选，必须判定）：")
        for i in empties:
            print(f"    {i['family']}.{i['field']}")
    if partials:
        print("部分缺失：")
        for i in partials:
            print(f"    {i['family']}.{i['field']} {i['rate']:.0%}")
    print(json.dumps({"fail": len(fails), "unknown_empty": len(empties),
                      "partial": len(partials), "known_limit": len(known)},
                     ensure_ascii=False))
    return 1 if (fails or empties) else 0


if __name__ == "__main__":
    raise SystemExit(main())
