"""G10 分析输出 REST 端点（组合聚合 + 统一导出）。

- ``POST /market/portfolio/aggregate``：持仓聚合（市值/盈亏/权重/行业暴露/风险贡献）。
- ``POST /market/export``：统一导出（csv/json/xlsx），列定义复用标准模型字段名。

均为写操作/计算型（agent_visible=False，人工确认域），不自动暴露为 MCP tool。
"""
import re
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter

from app.analysis.portfolio import Position, aggregate
from app.export import to_csv, to_excel, to_json
from app.routes._common import err, ok

router = APIRouter()


@router.post("/market/portfolio/aggregate")
async def portfolio_aggregate(body: Dict[str, Any]):
    """组合聚合：{positions:[{code,name,sector,qty,price,cost}]} →
    {total_market_value,total_pnl,items,sector_exposure,risk_contribution}。"""
    raw = body.get("positions") or []
    if not isinstance(raw, list) or not raw:
        return err(400, "positions 须为非空列表")
    try:
        positions = [Position(
            code=str(p.get("code", "")),
            name=str(p.get("name", "")),
            sector=str(p.get("sector", "")),
            qty=float(p.get("qty") or 0),
            price=float(p.get("price") or 0),
            cost=float(p.get("cost") or 0),
        ) for p in raw]
    except (TypeError, ValueError) as exc:
        return err(400, f"positions 字段非法：{exc}")
    return ok(aggregate(positions))


@router.post("/market/export")
async def market_export(body: Dict[str, Any]):
    """统一导出：{format: csv|json|xlsx, filename, columns:[{key,label}], rows:[dict]}。
    csv/json 返回文本内容 + 建议文件名；xlsx 落盘返回路径。"""
    fmt = str((body or {}).get("format") or "csv").lower()
    if fmt == "excel":
        fmt = "xlsx"
    if fmt not in ("csv", "json", "xlsx"):
        return err(400, f"format 非法：{fmt}（可选 csv/json/xlsx）")
    rows = body.get("rows") or []
    columns = body.get("columns")
    if not rows:
        return err(400, "rows 不能为空")
    if not columns:
        first = rows[0]
        columns = [{"key": k, "label": k} for k in (first.keys() if isinstance(first, dict) else [])]
    if not columns:
        return err(400, "rows 须为对象数组（用于推导列定义）")

    safe_name = re.sub(r"[^\w\-.]", "_", str(body.get("filename") or "export")) or "export"
    try:
        if fmt == "csv":
            return ok({"format": "csv", "content": to_csv(rows, columns),
                       "filename": f"{safe_name}.csv", "count": len(rows)})
        if fmt == "json":
            return ok({"format": "json", "content": to_json(rows),
                       "filename": f"{safe_name}.json", "count": len(rows)})
        from app.config import settings
        out_dir = Path(str(settings.db_path)).parent / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{safe_name}.xlsx"
        content = to_excel(rows, columns, str(path))
        return ok({"format": "xlsx", "path": str(path), "count": len(rows),
                   "note": "openpyxl 不可用时已回退为 CSV 内容", "fallback_csv": content})
    except Exception as exc:  # noqa: BLE001
        return err(500, f"导出失败：{exc}")


@router.get("/market/analysis/scripts")
async def market_analysis_scripts_list():
    """G10-3 分析脚本目录（契约元数据；GET 只读 → G3 自动暴露 MCP）。"""
    from app.analysis.contract import list_scripts
    items = list_scripts()
    return ok({"items": items, "count": len(items)})


@router.post("/market/analysis/run")
async def market_analysis_run(body: Dict[str, Any]):
    """执行分析脚本：{name, params?, data: DataResult 兼容结构}。"""
    from app.analysis.contract import run
    from app.datasource.result import DataResult
    name = (body or {}).get("name")
    data_raw = (body or {}).get("data") or {}
    if not name:
        return err(400, "缺少脚本名 name")
    try:
        dres = DataResult.from_source(
            data_raw.get("results"), source=data_raw.get("source") or "api",
            stale=bool(data_raw.get("stale", False)),
            as_of=data_raw.get("as_of"),
        )
        out = await run(name, dres, **(body.get("params") or {}))
    except KeyError as exc:
        return err(404, str(exc))
    except (ValueError, TypeError) as exc:
        return err(400, str(exc))
    return ok(out)
