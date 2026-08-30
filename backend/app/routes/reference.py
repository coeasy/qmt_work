from fastapi import APIRouter

from app.routes._common import _call, _need, err

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/reference/calendar")
async def reference_calendar(start: str = "", end: str = ""):
    """获取reference / calendar（GET /reference/calendar）。"""
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_trading_calendar, start, end)

@router.get("/reference/sectors")
async def reference_sectors():
    """获取reference / sectors（GET /reference/sectors）。"""
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_sector_list)

@router.get("/reference/sector-stocks")
async def reference_sector_stocks(sector: str = "沪深A股"):
    """获取reference / sector-stocks（GET /reference/sector-stocks）。"""
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_sector_stocks, sector)

@router.get("/reference/financial")
async def reference_financial(code: str):
    """获取reference / financial（GET /reference/financial）。"""
    b = _need()
    if b is None:
        return err(503, "未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return await _call(b, b.gateway.get_financial, code)

