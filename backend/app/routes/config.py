import asyncio
import json
import logging
import re
from pathlib import Path

from core.clock import now_iso
from core.config import (cold_bars_path, config_file, exe_dir, export_dir,
                         settings, update_config_file)
from core.errors import swallow
from core.context import AppContext, get_ctx
from core.paths import PathError, describe_dir, validate_dir
from datasource.cold_store import (count_rows_readonly, get_cold_store,
                                   init_cold_store, reset_cold_store)
from fastapi import APIRouter, Depends

from app.routes._common import err, ok

router = APIRouter()
log = logging.getLogger("qmt_work.routes.config")

# ---------------- 运行时配置中心（引擎级参数热更新，配置灵活化） ----------------

@router.get("/config/runtime")
async def get_runtime_config(ctx: AppContext = Depends(get_ctx)):
    """读取全部运行时引擎参数（含生效值/默认值/说明；修改后立即生效）。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    return ok(ctx.runtime_config.all())

@router.put("/config/runtime")
async def put_runtime_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """批量更新引擎运行参数（校验类型与下限，热更新无需重启）。"""
    rc = ctx.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    try:
        changed = rc.set_many(body)
    except ValueError as exc:
        return err(400, str(exc))
    ctx.db.audit("admin", "runtime_config.update", "global",
                   {"changed": changed}, "ok")
    return ok({"saved": True, "changed": changed, "config": rc.all()})

@router.post("/config/runtime/reset")
async def reset_runtime_config(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """恢复默认：body.key 指定单个（缺省全部重置）。"""
    rc = ctx.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    key = (body or {}).get("key", "")
    changed = rc.reset(key=key or "")
    ctx.db.audit("admin", "runtime_config.reset", key or "*",
                   {"changed": changed}, "ok")
    return ok({"reset": changed, "config": rc.all()})

@router.get("/config/runtime/history")
async def get_runtime_config_history(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    """变更历史（含回滚入口），按时间倒序。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    try:
        limit = max(1, min(int(limit), 200))
    except (ValueError, TypeError):
        limit = 50
    rows = ctx.runtime_config.history(limit)
    return ok({"rows": rows})

@router.post("/config/runtime/rollback")
async def rollback_runtime_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """回滚到指定历史记录 id：将该记录的旧值重新写回。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    entry_id = int((body or {}).get("id", 0))
    if not entry_id:
        return err(400, "缺少 id")
    ok_flag = ctx.runtime_config.rollback(entry_id)
    if not ok_flag:
        return err(400, "回滚失败：记录不存在或 key 非法")
    ctx.db.audit("admin", "runtime_config.rollback", str(entry_id),
                   {"id": entry_id}, "ok")
    return ok({"rolled_back": entry_id, "config": ctx.runtime_config.all()})


# ---------------- 风控配置（运行期可调，持久化 risk_config） ----------------

@router.get("/config/risk")
async def get_risk_config(ctx: AppContext = Depends(get_ctx)):
    """读取风控参数（含日级限额与熔断实时状态）。

    ★ 风控未初始化时**必须报错**，绝不返回硬编码的"示例参数"。
    曾在这里兜底返回一组写死的数值，界面于是显示「风控已配置 / 单笔上限 10 万」，
    而实际上 ``ctx.risk`` 为 None —— 下单链路根本没有任何风控参与。
    用户以为有保护、其实是裸奔，这比「明确告知风控不可用」危险得多。
    与 ``PUT /config/risk`` 的 503 语义保持一致。
    """
    rm = ctx.risk
    if rm is None:
        return err(503, "风控未初始化：当前下单链路无风控保护，请检查服务启动状态")
    data = rm.to_dict()
    data["daily"] = rm.daily_stats()
    return ok(data)

@router.put("/config/risk")
async def put_risk_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """更新风控参数（持久化到 risk_config 表）。"""
    rm = ctx.risk
    if rm is None:
        return err(503, "风控未初始化")
    try:
        changed = rm.update_from(body)
    except ValueError as exc:
        return err(400, str(exc))
    rm.save_to_db(ctx.db)
    ctx.db.audit("admin", "risk_config.update", "global",
                   {"changed": changed}, "ok")
    return ok({"saved": True, "changed": changed, "config": rm.to_dict()})

@router.get("/config/risk/daily")
async def get_risk_daily(ctx: AppContext = Depends(get_ctx)):
    """日级风控实时用量与熔断状态（B4）。"""
    if ctx.risk is None:
        return err(503, "风控未初始化")
    return ok(ctx.risk.daily_stats())

@router.post("/config/risk/circuit")
async def post_risk_circuit(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """熔断开关：action=trip 手动熔断（停止买入开仓）/ action=reset 解除熔断。"""
    rm = ctx.risk
    if rm is None:
        return err(503, "风控未初始化")
    body = body or {}
    action = str(body.get("action", "reset")).lower()
    if action == "trip":
        reason = str(body.get("reason") or "人工熔断：暂停一切买入开仓")
        rm.trip(reason)
        if ctx.notifier:
            await ctx.notifier.notify("risk.circuit", "风控熔断已开启", reason,
                                        {"reason": reason, "manual": True})
    elif action == "reset":
        rm.reset_circuit()
        if ctx.notifier:
            await ctx.notifier.notify("risk.circuit", "风控熔断已解除",
                                        "已恢复买入开仓，日初净值重新锚定", {"manual": True})
    else:
        return err(400, "action 仅支持 trip / reset")
    ctx.db.audit("admin", f"risk.circuit.{action}", "global",
                   {"body": body}, "ok")
    return ok(rm.daily_stats())


# ---------------- 数据目录（P0-3 II：目录可配置） ----------------

#: 可设置的三类目录。``db`` 是**唯一**需要重启的一类 —— 主库路径在
#: ``Settings`` 构造期就固化了，运行期改不动，改了也不许假装即时生效。
_PATH_KINDS = ("export", "cold", "db")


def _stat_fields(p: Path) -> dict:
    """目录的只读体检（不创建目录、不抛异常）。"""
    d = describe_dir(str(p))
    return {"exists": d["exists"], "writable": d["writable"],
            "inside_install": d["inside_install"], "usable": d["ok"],
            "reason": d["reason"]}


def _count_cold_rows(p: Path) -> int:
    """只读地数一下某个冷仓文件有多少行（用于告诉用户「那边还有多少历史」）。

    实现收敛在 ``datasource.cold_store.count_rows_readonly``（路由层不得直连 DB）：
    语义是「只读打开、不建文件、失败返 -1」，详见该函数 docstring。
    """
    return count_rows_readonly(p)


def _cold_candidates(ctx: AppContext, current: Path) -> list[dict]:
    """可能残留旧冷数据的位置（用于「迁移冷库」）。

    为什么需要猜：改冷库目录后新库是空的，旧库文件还在硬盘上，但**界面已经不再
    显示它** —— 用户于是永远发现不了「历史其实还在，只是搬个家的事」。
    候选来源 = 历史默认位置 + 配置变更历史里出现过的旧值。
    """
    seen: set[str] = set()
    out: list[dict] = []

    def _add(p: Path) -> None:
        try:
            rp = str(Path(p).resolve())
            cur = str(Path(current).resolve())
        except OSError:
            return
        if rp in seen or rp == cur:
            return
        seen.add(rp)
        pp = Path(rp)
        if not pp.is_file():
            return
        out.append({"path": rp, "rows": _count_cold_rows(pp)})

    _add(settings.db_path.parent / "bars_cold.db")
    _add(exe_dir() / "bars_cold.db")
    rc = ctx.runtime_config
    if rc is not None:
        try:
            hist = rc.history(50)
        except Exception:  # noqa: BLE001 历史读不出来只影响候选，不影响主流程
            hist = []
        for h in hist or []:
            if h.get("key") != "offline.cold_dir":
                continue
            for raw in (h.get("old_value"), h.get("new_value")):
                if not raw:
                    continue
                try:
                    val = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(val, str) and val.strip():
                    _add(cold_bars_path(val))
    return out


def _backup_view(ctx: AppContext):
    """取**活的** DBBackup（能报出「上次备份是跳过还是失败」）。

    拿不到（如单测里未跑 bootstrap）时按当前配置新建一个只读视图：占用统计仍然
    正确，只是 `last.action` 恒为 idle。**不返回 None** —— 界面上「备份占用」这一栏
    不能因为拿不到实例就整块消失（那就又变成不可见了）。
    """
    inst = getattr(ctx, "db_backup", None)
    if inst is not None:
        return inst
    from gateway.db_backup import DBBackup
    return DBBackup(
        settings.db_path, keep=settings.db_backup_keep,
        interval=settings.db_backup_interval,
        max_total_mb=settings.db_backup_max_total_mb,
        min_keep=settings.db_backup_min_keep)


def _paths_snapshot(ctx: AppContext) -> dict:
    """三类目录的生效路径 + 可改性 + 体检结果。"""
    rc = ctx.runtime_config
    export_raw = str(rc.get("offline.export_dir") or "") if rc else ""
    cold_raw = str(rc.get("offline.cold_dir") or "") if rc else ""
    cold_file = cold_bars_path(cold_raw)
    export_p = export_dir(export_raw)
    cold_store = get_cold_store()
    kc = ctx.kline_cache
    return {
        "install_dir": str(exe_dir()),
        "config_file": str(config_file()),
        "export": {
            "kind": "export", "label": "离线数据导出目录",
            "path": str(export_p), "file": "",
            "configured": bool(export_raw),
            "default_path": str(export_dir("")),
            "mutable": True, "requires_restart": False,
            "note": "导出 CSV/JSON/Feather 的默认落盘位置；改后立即生效",
            **_stat_fields(export_p),
        },
        "cold": {
            "kind": "cold", "label": "冷 K 线仓目录",
            "path": str(cold_file.parent), "file": str(cold_file),
            "configured": bool(cold_raw),
            "default_path": str(cold_bars_path("").parent),
            "mutable": True, "requires_restart": False,
            "note": "历史 K 线（超过热窗口）存放位置；改后立即生效。"
                    "⚠️ 已有冷数据不会自动搬迁，请用「迁移冷库」",
            "rows": int(cold_store.count()) if cold_store is not None else 0,
            # ★ 生效值 vs 已装配值必须分开报：配置改了但重建失败时，
            #   界面若只显示「配置路径」会让用户以为已经切换成功。
            "attached_path": str(getattr(kc, "cold_path", "") or ""),
            "in_sync": (str(getattr(kc, "cold_path", "") or "") == str(cold_file)),
            "candidates": _cold_candidates(ctx, cold_file),
            **_stat_fields(cold_file.parent),
        },
        "db": {
            "kind": "db", "label": "主库目录（app.db）",
            "path": str(settings.db_path.parent), "file": str(settings.db_path),
            "configured": False,
            "default_path": str(exe_dir() / "data"),
            "mutable": False, "requires_restart": True,
            "note": "主库路径在进程启动时固化，无法运行期切换：写入配置后需重启客户端。"
                    "⚠️ 重启后新目录会新建空库，当前库文件不会自动搬迁 —— "
                    "请先在旧位置关闭客户端、手动复制 app.db（含 -wal/-shm）到新目录再启动",
            # ★ 备份占用必须随主库一起展示（2026-09-20 修复）：备份是**整库全量复制**，
            #   1.14GB 的主库按「保留 10 份」就是 11GB 常驻磁盘，而此前
            #   `list_backups()` 一个调用方都没有 ⇒ 用户直到磁盘满了才发现。
            "backups": _backup_view(ctx).stats(),
            **_stat_fields(settings.db_path.parent),
        },
    }


@router.get("/config/paths")
async def get_paths(ctx: AppContext = Depends(get_ctx)):
    """数据目录总览：主库 / 冷库 / 导出目录的生效路径、可改性与体检结果。"""
    return ok(_paths_snapshot(ctx))


@router.post("/config/paths/validate")
async def validate_path(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """校验一个目录**能不能用**（不落任何配置）。

    ★ 这个端点**永不 400/500**：它服务于「边输边提示」的界面，用户路径打了一半
    是常态。不可用只能是 ``ok=false + reason``，否则界面每次按键都在弹红错。
    """
    raw = str((body or {}).get("path") or "")
    return ok(describe_dir(raw))


@router.put("/config/paths")
async def put_paths(body: dict, ctx: AppContext = Depends(get_ctx)):
    """设置数据目录。``kind``：export（立即生效）/ cold（立即生效）/ db（需重启）。"""
    body = body or {}
    kind = str(body.get("kind") or "").strip().lower()
    raw = body.get("path")
    if kind not in _PATH_KINDS:
        return err(400, "kind 仅支持 export / cold / db")
    try:
        p = validate_dir(raw, create=True)
    except PathError as exc:
        return err(400, str(exc))

    rc = ctx.runtime_config
    if rc is None and kind != "db":
        return err(503, "运行时配置中心未初始化")

    if kind == "export":
        rc.set_many({"offline.export_dir": str(p)})
        ctx.db.audit("admin", "paths.update", "export", {"path": str(p)}, "ok")
        return ok({"saved": True, "kind": "export", "path": str(p),
                   "requires_restart": False, **_paths_snapshot(ctx)})

    if kind == "cold":
        prev_raw = str(rc.get("offline.cold_dir") or "")
        prev_store = get_cold_store()
        rc.set_many({"offline.cold_dir": str(p)})
        try:
            reset_cold_store()
            new_store = init_cold_store(cold_bars_path(str(p)))
        except Exception as exc:  # noqa: BLE001
            # ★ 切换失败必须**整体回滚**：配置回旧值 + 旧冷仓重新装配。
            # 只让配置生效而冷仓没换成功 ⇒ 「配置说在 D 盘、实际还在 C 盘」，
            # 用户以为历史数据在 D 盘，于是放心地格式化 C 盘。
            log.warning("冷仓切换到 %s 失败，回滚到 %s：%s", p, prev_raw, exc)
            try:
                rc.set_many({"offline.cold_dir": prev_raw})
            except ValueError as exc:
                # 回滚本身失败要**显式告警**而不是静默：此时配置值可能停在半路
                # （既不是新的也不是旧的），用户看到的路径与实际装配的不一致。
                log.error("冷仓回滚失败：offline.cold_dir 可能停在不一致状态（%s）", exc)
            if prev_store is not None:
                try:
                    init_cold_store(prev_store.path)
                except Exception as exc:  # noqa: BLE001
                    swallow(exc, why="旧冷仓重建失败：已超过尽力而为的范围，"
                                    "配置已回滚，重启后会自动按配置重建")
            if ctx.kline_cache is not None and prev_store is not None:
                ctx.kline_cache.attach_cold(prev_store)
            return err(500, f"冷仓切换失败（已回滚）：{exc}")
        if ctx.kline_cache is not None:
            ctx.kline_cache.attach_cold(new_store)
        ctx.db.audit("admin", "paths.update", "cold", {"path": str(p)}, "ok")
        return ok({"saved": True, "kind": "cold", "path": str(p),
                   "requires_restart": False,
                   "note": "已有冷数据不会自动搬迁，请在下方点击「迁移冷库」",
                   **_paths_snapshot(ctx)})

    # ---- db：写配置文件，如实告知需重启 ----
    db_file = p / "app.db"
    try:
        update_config_file({"db_path": str(db_file)})
    except OSError as exc:
        return err(500, f"配置文件写入失败：{exc}")
    ctx.db.audit("admin", "paths.update", "db", {"path": str(db_file)}, "ok")
    return ok({"saved": True, "kind": "db", "path": str(p), "file": str(db_file),
               # ★ 绝不假装即时生效：这是「点了没反应」最常见的来源。
               "requires_restart": True,
               "current_file": str(settings.db_path),
               "note": "已写入 qmt_work_config.json，重启客户端后生效。"
                       "当前库文件仍在旧位置，重启前请手动复制到新目录（含 -wal/-shm），"
                       "否则新目录会新建一个空库",
               **_paths_snapshot(ctx)})


@router.post("/config/paths/migrate-cold")
async def migrate_cold(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """把**另一个冷仓文件**里的历史 K 行复制进当前冷仓（**不删源**）。

    ``source`` 省略时自动取第一个可迁移的候选（旧默认位置 / 变更历史里的旧值）。
    """
    store = get_cold_store()
    if store is None:
        return err(503, "冷仓未初始化")
    src = str((body or {}).get("source") or "").strip()
    if not src:
        cands = _cold_candidates(ctx, Path(store.path))
        if not cands:
            return err(400, "未指定源冷仓，且未发现可迁移的旧冷仓文件")
        src = cands[0]["path"]
    try:
        res = await asyncio.to_thread(store.copy_from_file, src)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"迁移失败：{exc}")
    if res.get("error"):
        return err(400, res["error"])
    ctx.db.audit("admin", "paths.migrate_cold", src,
                   {"moved": res.get("moved", 0)}, "ok")
    return ok({"moved": int(res.get("moved") or 0),
               "batches": int(res.get("batches") or 0),
               "source": res.get("src") or src, "source_kept": True,
               "note": "已复制完成；源文件保留未删，确认无误后可自行清理",
               **_paths_snapshot(ctx)})


@router.post("/config/paths/db-backups/prune")
async def prune_db_backups(ctx: AppContext = Depends(get_ctx)):
    """按保留策略清理旧备份（**不可逆**：文件直接删除，不进回收站）。

    ★ 只删 ``backups/`` 里匹配 ``app.YYYYMMDD_HHMMSS.db`` 的**受管备份**，
    至少保留 ``min_keep`` 份；主库目录里的其它同名文件（如 ``app.db.bak_*``）
    **一律不碰** —— 它们可能是用户自己留的副本，删了无法恢复。
    """
    view = _backup_view(ctx)
    res = await asyncio.to_thread(view.prune_now)
    try:
        ctx.db.audit("admin", "paths.prune_db_backups", str(view.backups_dir),
                     {"removed": res.get("removed_count", 0),
                      "freed": res.get("freed_bytes", 0)}, "ok")
    except Exception as exc:  # noqa: BLE001 审计写失败不影响已完成的清理
        swallow(exc, why="备份清理已完成，审计写失败不回滚也不报错")
    return ok({**res, **_paths_snapshot(ctx)})


@router.post("/config/paths/db-backups/run")
async def run_db_backup(ctx: AppContext = Depends(get_ctx)):
    """立刻做一次备份。

    ★ 返回值必须区分 **created / skipped / failed** 三种结果：``backup_once`` 的
    返回值 ``None`` 既可能是「主库没变、已跳过」也可能是「失败」，界面把「跳过」
    渲染成「备份失败」就是错误归因（本项目明令禁止）。
    """
    view = _backup_view(ctx)
    # ★ 审计行**本身会写主库**，因此必须在「记录指纹」之前写掉 —— 否则刚记下的
    #   指纹立刻失效，用户连点两次「立即备份」会白复制两份整库（实测 1.14GB × 2）。
    #   backup_once 的 after_create 回调正是为此存在。
    def _audit_created(dst) -> None:
        ctx.db.audit("admin", "paths.run_db_backup", str(dst), {"action": "created"}, "ok")

    # 1GB+ 主库的一致性复制是**同步重活**：必须丢到线程里，否则整个事件循环
    # （含 WS 广播与所有 HTTP）在复制期间一起卡住。
    path = await asyncio.to_thread(view.backup_once, "manual", _audit_created)
    st = dict(view.last_status or {})
    action = str(st.get("action") or "failed")
    if action == "created":
        msg = f"已备份：{st.get('name', '')}（{st.get('detail', '')}）"
    elif action == "skipped":
        # 跳过 = 没有任何副作用 ⇒ **不写审计**（写了反而会让下一次跳过失效）
        msg = f"未做新备份：{st.get('detail', '')}"
    else:
        msg = f"备份失败：{st.get('detail', '')}"
        try:
            ctx.db.audit("admin", "paths.run_db_backup", str(view.backups_dir),
                         {"action": "failed", "detail": str(st.get("detail") or "")}, "fail")
        except Exception as exc:  # noqa: BLE001
            swallow(exc, why="失败审计写不进去不能掩盖「备份失败」这个事实本身")
    return ok({"action": action, "path": path or "", "message": msg,
               **_paths_snapshot(ctx)})


# ---------------- 外观配置（P1-G：换机器 / 清缓存不再丢） ----------------

#: 后端**不认识**皮肤目录（那在前端 ``design/skins.ts``），只负责**存**与**形状校验**。
#: 假装认识目录就会在「前端加了一套皮肤、后端还不知道」时把用户的合法选择判为无效。
_UI_FIELDS = ("skin_id", "accent", "density", "wallpaper")
_UI_DEFAULTS: dict[str, str] = {
    "skin_id": "light", "accent": "", "density": "comfortable", "wallpaper": "",
}
_ACCENT_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DENSITIES = ("compact", "comfortable")


def _ui_load(ctx: AppContext) -> dict:
    rc = ctx.runtime_config
    out = dict(_UI_DEFAULTS)
    raw = str(rc.get("ui.appearance") or "") if rc else ""
    if raw:
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            loaded = None
        if isinstance(loaded, dict):
            for k in _UI_FIELDS:
                if isinstance(loaded.get(k), str):
                    out[k] = loaded[k]
    return out


def _ui_validate(patch: dict) -> dict:
    """校验外观字段；非法直接抛 ValueError（→ 400，绝不静默改成默认值）。

    ★ 静默改默认是这类配置最常见的坑：用户选了红色 accent，因为格式不对被悄悄
    换成默认，界面上却显示「已保存」—— 于是用户以为保存成功，反复刷新看不到变化。
    """
    out: dict[str, str] = {}
    for k, v in (patch or {}).items():
        if k not in _UI_FIELDS:
            raise ValueError(f"未知外观字段：{k}")
        s = str(v or "").strip()
        if k == "accent" and s and not _ACCENT_RE.match(s):
            raise ValueError("accent 须为 #RGB 或 #RRGGBB 形式的颜色值")
        if k == "density" and s and s not in _DENSITIES:
            raise ValueError(f"density 仅支持 {' / '.join(_DENSITIES)}")
        if k == "skin_id" and len(s) > 64:
            raise ValueError("skin_id 过长（>64）")
        out[k] = s
    return out


@router.get("/config/ui")
async def get_ui_config(ctx: AppContext = Depends(get_ctx)):
    """读取外观配置（皮肤 / 主色 / 密度 / 背景图）。

    ★ 与 localStorage 的关系：界面本地仍有一份（离线可用），但**服务器这份是权威**，
    换机器或清缓存后能拉回来 —— 此前外观只存 localStorage，换台机器就回到默认皮肤。
    """
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    return ok({"appearance": _ui_load(ctx), "defaults": dict(_UI_DEFAULTS),
               "fields": list(_UI_FIELDS)})


@router.put("/config/ui")
async def put_ui_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """更新外观配置（可按字段部分提交）。"""
    rc = ctx.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    try:
        patch = _ui_validate(body or {})
    except ValueError as exc:
        return err(400, str(exc))
    merged = _ui_load(ctx)
    merged.update(patch)
    rc.set_many({"ui.appearance": json.dumps(merged, ensure_ascii=False)})
    ctx.db.audit("admin", "ui.update", "global", {"changed": sorted(patch)}, "ok")
    return ok({"saved": True, "changed": sorted(patch), "appearance": merged})


@router.post("/config/ui/reset")
async def reset_ui_config(ctx: AppContext = Depends(get_ctx)):
    """恢复默认外观。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    ctx.runtime_config.reset("ui.appearance")
    return ok({"reset": True, "appearance": _ui_load(ctx)})


@router.get("/config/ui/export")
async def export_ui_config(ctx: AppContext = Depends(get_ctx)):
    """导出外观配置（可抄给另一台机器）。"""
    if ctx.runtime_config is None:
        return err(503, "运行时配置中心未初始化")
    return ok({"version": 1, "kind": "qmt_ui_appearance",
               "exported_at": now_iso(), "appearance": _ui_load(ctx)})


@router.post("/config/ui/import")
async def import_ui_config(body: dict, ctx: AppContext = Depends(get_ctx)):
    """导入外观配置（接受导出对象本身，也接受它的 ``appearance`` 字段）。"""
    rc = ctx.runtime_config
    if rc is None:
        return err(503, "运行时配置中心未初始化")
    payload = (body or {}).get("appearance")
    if not isinstance(payload, dict):
        payload = body or {}
    try:
        patch = _ui_validate(payload)
    except ValueError as exc:
        return err(400, f"导入内容不合法：{exc}")
    if not patch:
        return err(400, "导入内容里没有可识别的外观字段")
    merged = _ui_load(ctx)
    merged.update(patch)
    rc.set_many({"ui.appearance": json.dumps(merged, ensure_ascii=False)})
    ctx.db.audit("admin", "ui.import", "global", {"changed": sorted(patch)}, "ok")
    return ok({"imported": True, "changed": sorted(patch), "appearance": merged})


# ---------------- 健康检查 ----------------

