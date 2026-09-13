from core.context import AppContext, get_ctx
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.get("/audit")
async def list_audit(action: str = "", limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """查询审计日志（含 hash 链字段，敏感参数已脱敏）。"""
    limit = max(1, min(int(limit), 500))
    cols = ("SELECT id, actor, action, target, params_json, result, ip, created_at, "
            "prev_hash, hash FROM audit_log ")
    if action:
        rows = ctx.db.query(cols + "WHERE action=? ORDER BY id DESC LIMIT ?",
                              (action, limit))
    else:
        rows = ctx.db.query(cols + "ORDER BY id DESC LIMIT ?", (limit,))
    return ok(rows)

@router.get("/audit/verify")
async def verify_audit(limit: int = 200_000, ctx: AppContext = Depends(get_ctx)):
    """校验审计日志 hash 链完整性（D4 防篡改），定位第一处断链。"""
    if ctx.db is None:
        return err(503, "数据库未初始化")
    res = ctx.db.verify_audit_chain(limit=max(1, int(limit)))
    if not res["ok"] and ctx.notifier:
        await ctx.notifier.notify(
            "audit.tampered", "审计链校验失败",
            f"检出 {res['broken_count']} 处断链，首个异常记录 id="
            f"{res['broken'][0]['id'] if res['broken'] else '?'}", res)
    return ok(res)


# ---------------- API Key 管理 ----------------

