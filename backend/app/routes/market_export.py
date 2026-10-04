"""历史 K 线导出 / 同步子路由（P1-1 自 ``market.py`` 拆出）。

为什么单独成模块
----------------
``market.py`` 第二次长到 50.8KB 越过了 Gate 4 的 50KB 上限
（``scripts/check_execution_architecture.py``）。它当时承载三类关注点：

1. **单标的行情**（quote / stock-info / kline / minutes）——直连数据源；
2. **多维聚合**——已先一步拆到 ``market_multidim.py``；
3. **落盘导出**（export / export 读取 / sync）——把本地缓存里的 K 线写成
   CSV/JSON 到用户指定目录。

本模块只承载第 3 类。它们与第 1、2 类**不共享任何模块级状态**，唯一公共依赖是
``_resolve_export_dir()``（本模块私有，原也只被这三条端点调用）。

★ 为什么是「拆文件」而不是「把上限调高」：Gate 4 的存在意义正是「防止再膨胀」
（见其 docstring），把阈值从 50KB 抬到 55KB 只会让下一次膨胀更晚被发现 ——
那正是本项目反复记录的「改判据而不是改代码」假绿灯。

拆分方式是**子路由挂载**：本模块自带 ``router``，由 ``market.py``
``include_router`` 在**原位置**挂上 —— 对外 URL 与 OpenAPI 端点清单完全不变
（``backend/tests/contracts/rest_endpoints.json`` 与
``scripts/check_api_contract_drift.py`` 不受影响）。
"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends

from app.routes._common import BrokerError, _call, _need, err, ok
from app.services.market import kline_io
from core.config import export_dir
from core.context import AppContext, get_ctx
from core.paths import PathError, validate_dir

router = APIRouter()


# ---------------- 历史 K 线导出到本地指定目录（CSV/JSON） ----------------

def _resolve_export_dir(ctx: AppContext, raw) -> str:
    """解析导出目录：未指定时用运行期配置 ``offline.export_dir``（默认 <运行目录>/export）。

    ★ 目录可能来自用户输入，必须经 ``core.paths.validate_dir`` 校验：
    否则填个 ``C:\\Windows\\System32`` 就把导出文件写进系统目录了
    （轻则权限报错，重则污染系统目录）。校验唯一入口，不在这里另写一套。
    """
    rc = ctx.runtime_config
    v = str(raw or "").strip()
    if not v:
        v = str(rc.get("offline.export_dir") or "") if rc else ""
    return str(validate_dir(str(export_dir(v)), create=True))


@router.post("/market/kline/export")
async def kline_export(body: dict, ctx: AppContext = Depends(get_ctx)):
    """批量导出历史 K 线到本地指定目录（CSV / JSON）。

    参数（body JSON）：
    - dest_dir: **可选**，导出目录（不存在自动创建）；省略时用运行期配置
      ``offline.export_dir``（默认 ``<运行目录>/export``，可在「设置 → 数据目录」改）
    - codes: 可选，代码列表；省略则导出本地缓存中全部 code×period 序列
    - period: 可选，仅导出该周期
    - count: 每序列导出最近 N 根；0/省略=导出该序列全部根数
    - format: csv | json，默认 csv
    - refresh: false（默认）快速直接用本地缓存导出；true 先回源券商刷新到缓存再导出（需连接券商）
    - conn_id: 指定 broker 连接（refresh 回源用）

    数据来自本地 K 线缓存（KlineCache），"快速"导出完全离线，无网络调用。
    """
    if ctx.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    body = dict(body or {})
    try:
        body["dest_dir"] = _resolve_export_dir(ctx, body.get("dest_dir"))
    except PathError as exc:
        return err(400, str(exc))
    try:
        out = await kline_io.kline_export(ctx.kline_cache, body)
    except ValueError as exc:
        return err(400, str(exc))
    ctx.db.audit("admin", "kline.export", body.get("dest_dir") or "",
                   {"format": body.get("format") or "csv",
                    "refresh": bool(body.get("refresh", False)),
                    "dest_dir": body.get("dest_dir") or ""},
                   f"files={out.get('exported', 0)} rows={out.get('rows', 0)}")
    return ok(out)


@router.get("/market/kline/export")
async def kline_export_read(code: str, dest_dir: str = "", period: str = "1d",
                            format: str = "csv", ctx: AppContext = Depends(get_ctx)):
    """读取本地导出目录中已导出的历史 K 线文件（离线/断线时也可用）。

    直接读磁盘文件，不依赖券商连接；文件不存在返回 404。
    ``dest_dir`` 省略时用运行期配置（与 POST 同口径）。
    format: csv | json（须与导出时一致）。
    """
    from gateway.kline_cache import KlineCache
    try:
        dest_dir = _resolve_export_dir(ctx, dest_dir)
    except PathError as exc:
        return err(400, str(exc))
    path = KlineCache.file_path(code, period, dest_dir, format)
    if not os.path.exists(path):
        return err(404, f"导出文件不存在：{os.path.basename(path)}"
                        "（请先 POST /market/kline/export 导出）")
    try:
        bars = await asyncio.to_thread(KlineCache.read_export, path, format)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"读取导出文件失败：{exc}")
    return ok({"code": code, "period": period, "format": format,
               "file": path, "count": len(bars), "bars": bars})


# ---------------- 同步全部历史 K 线（日线+周线）到本地指定目录 ----------------

@router.post("/market/kline/sync")
async def kline_sync(body: dict, ctx: AppContext = Depends(get_ctx)):
    """把一批股票的最新历史 K 线（含日线 1d、周线 1w）同步到本地指定目录。

    流程：确定股票集合 → 逐只回源券商拉取最新 K 线写入本地缓存 → 导出到 dest_dir。
    参数（body JSON）：dest_dir(**可选**，省略时用运行期配置 ``offline.export_dir``)/
    codes/sector/periods/count/format/limit/conn_id。
    单只失败不中断整体（errors 列出）。真实行情，缺数据不伪造。
    """
    if ctx.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    try:
        dest = _resolve_export_dir(ctx, (body or {}).get("dest_dir"))
    except PathError as exc:
        return err(400, str(exc))
    body = {**(body or {}), "dest_dir": dest}

    async def _get_sector_stocks(sector: str, conn_id):
        b = _need(conn_id)
        if b is None:
            raise BrokerError("未连接任何券商客户端：省略 codes 需用板块成分，请先连接券商。")
        return await _call(b, b.gateway.get_sector_stocks, sector) or []

    try:
        out = await kline_io.kline_sync(ctx.kline_cache, body, _get_sector_stocks)
    except ValueError as exc:
        return err(400, str(exc))
    except BrokerError as exc:
        return err(503, str(exc))
    except LookupError as exc:
        return err(404, str(exc))
    ctx.db.audit("admin", "kline.sync", body.get("dest_dir") or "",
                   {"format": body.get("format") or "csv",
                    "periods": out["periods"], "count": int(body.get("count") or 250),
                    "codes": out["codes_total"]},
                   f"files={out['files_count']} rows={out['rows']} errors={len(out['errors'])}")
    return ok(out)
