from fastapi import APIRouter

from app.routes._common import audit_log, err, ok

# --- stdlib imports injected by fix_route_imports ---



router = APIRouter()

@router.post("/strategies/generate")
async def strategies_generate(body: dict):
    """创建/提交strategies / generate（POST /strategies/generate）。"""
    from tools.strategy_gen import generate_strategy
    try:
        audit_log("api", "strategies_generate", "gen", body)

        return ok(generate_strategy(
            body.get("strategy_type") or body.get("strategy", ""), body.get("code", "600519.SH"),
            body.get("client_path", ""), body.get("account_id", ""),
            body.get("params") or {}))
    except ValueError as exc:
        return err(400, str(exc))

@router.post("/strategies/save")
async def strategies_save(body: dict):
    """创建/提交strategies / save（POST /strategies/save）。"""
    from tools.strategy_gen import save_qmt_strategy
    try:
        audit_log("api", "strategies_save", body.get('name',''), body)

        return ok(save_qmt_strategy(body.get("filename", "strategy.py"),
                                    body.get("content", ""),
                                    body.get("client_path", "")))
    except OSError as exc:
        return err(400, f"写入失败：{exc}")


# ---------------- 手动交易（Trade 页） ----------------

