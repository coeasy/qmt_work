"""策略市场 REST 路由：模板目录 / 发布 / 安装 / 导入导出。"""
import os
import tempfile

from fastapi import APIRouter

from app.routes._common import ok, err
from tools import strategy_market as sm

router = APIRouter()


@router.get("/strategy-market/catalog")
async def market_catalog():
    """内置模板目录。"""
    return ok(sm.strategy_catalog())


@router.get("/strategy-market/market")
async def market_list(tag: str | None = None, limit: int = 50):
    """市场策略列表（可按 tag 过滤）。"""
    return ok(sm.list_market(limit=limit, tag=tag))


@router.get("/strategy-market/market/{id}")
async def market_get(id: str):
    """取单条市场策略。"""
    row = sm.get_market(id)
    if row is None:
        return err(404, f"市场策略不存在：{id}")
    return ok(row)


@router.post("/strategy-market/publish")
async def market_publish(body: dict):
    """发布一条用户策略到市场。body: {strategy_id, title, author?, description?, type, content, tags?:[...]}"""
    try:
        rec = sm.publish_to_market(
            body.get("strategy_id", ""),
            {k: body.get(k) for k in ("title", "author", "description", "type", "tags", "id")},
            body.get("content", ""))
        return ok(rec)
    except RuntimeError as exc:
        return err(503, str(exc))


@router.post("/strategy-market/install")
async def market_install(body: dict):
    """把市场策略安装到 QMT 客户端 mpython 目录。body: {id, client_path}"""
    client_path = (body.get("client_path") or "").strip()
    if not client_path:
        return err(400, "client_path 不能为空")
    try:
        return ok(sm.install_from_market(body.get("id", ""), client_path))
    except ValueError as exc:
        return err(404, str(exc))


@router.post("/strategy-market/export")
async def market_export(body: dict):
    """导出 .zip bundle。body: {ids:[...], path?}"""
    ids = body.get("ids") or []
    out_path = body.get("path") or os.path.join(
        tempfile.gettempdir(), f"strategy_bundle_{os.getpid()}.zip")
    try:
        return ok(sm.export_bundle(ids, out_path))
    except RuntimeError as exc:
        return err(503, str(exc))


@router.post("/strategy-market/import")
async def market_import(body: dict):
    """导入 .zip bundle。body: {path}"""
    path = body.get("path", "")
    if not path or not os.path.isfile(path):
        return err(400, f"bundle 不存在：{path}")
    try:
        return ok(sm.import_bundle(path))
    except (ValueError, RuntimeError) as exc:
        return err(400, str(exc))


@router.post("/strategy-market/export-json")
async def market_export_json(body: dict):
    """导出单策略 JSON。body: {id, path?}"""
    out_path = body.get("path") or os.path.join(
        tempfile.gettempdir(), f"strategy_{os.getpid()}.json")
    try:
        return ok(sm.export_json(body.get("id", ""), out_path))
    except (ValueError, RuntimeError) as exc:
        return err(400, str(exc))


@router.post("/strategy-market/import-json")
async def market_import_json(body: dict):
    """导入单策略 JSON。body: {path}"""
    path = body.get("path", "")
    if not path or not os.path.isfile(path):
        return err(400, f"JSON 不存在：{path}")
    try:
        return ok(sm.import_json(path))
    except (ValueError, RuntimeError) as exc:
        return err(400, str(exc))
