"""多券商 QMT 客户端接入层：安装台账 / 并行端口方案 / DBF 文件单。

为什么要这一层
--------------
``connectors/install_registry.py``（多安装发现、端口台账、冲突审计）与
``connectors/qmt_file_order.py``（DBF 文件单）此前**只是库代码 + 单测**，
没有任何 API 入口 —— 只能靠手写脚本调用。**能力存在但不可发现，等于不存在**
（同一个教训见 ``routes/capabilities.py::capabilities_mcp`` 的注释）。

端点
----
* ``GET  /qmt/installs``               多安装台账 + 声明端口冲突 + 活体双绑定
* ``GET  /qmt/installs/port-plan``     多券商并行的端口方案（第 i 套 +i*stride）
* ``GET  /qmt/file-order/monitors``    DBF 文件单 + 批量单 CSV 两条通道的 armed 状态
* ``POST /qmt/file-order/schema``      读用户导出的模板 DBF，返回真实列类型/长度
* ``POST /qmt/file-order/stage``       生成一份**待核对**的 DBF（永不进监控目录）

三条硬边界
----------
1. **不猜盘符**。安装根来自「显式 roots」或「已配置券商连接的客户端路径回溯」
   （``find_install_root``）。两者都没有时返回 ``source="none"``，
   **不假装"扫了全盘没找到"** —— 那是把"没查"说成"查了没有"。
2. **活体探针三态**。只有 ``live=1`` 才跑 ``netstat``；探针不可用时
   ``live_probe="unavailable"``，``ok`` 不得因此变绿。
3. **文件单只落 staging**。文件单的语义是"监控目录里出现文件就下单"，所以
   "生成一份待核对的文件"与"把文件交给客户端"必须是两步。本 API **只做第一步**，
   且**不接受** ``allow_monitor_dir`` 参数 —— 第二步只能由人在终端里完成。
   这不是保守，是防止一次拼错的脚本把调试单变成真委托。
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Depends

from app.routes._common import audit_log, err, ok
from core.clock import local_now
from core.config import export_dir
from core.context import AppContext, get_ctx

log = logging.getLogger("qmt_work.routes.qmt_client")
router = APIRouter()

#: 文件单 staging 目录（相对 ``export_dir()``）。**不是**任何客户端的监控目录。
STAGING_SUBDIR = "file_order_staging"

#: 单次 stage 允许的最大行数。文件单是"丢文件就下单"，一次几千行等于批量误单，
#: 因此设上限并如实报错，而不是默默截断。
MAX_STAGE_ROWS = 200


# ----------------------------------------------------------------------
# roots 解析：显式 > 已配置连接回溯 > none
# ----------------------------------------------------------------------
def _split_roots(raw: str) -> list[str]:
    """把 ``roots`` 参数切成目录列表。支持 ``,`` / ``;`` / 换行 / ``|`` 分隔。"""
    out: list[str] = []
    for chunk in (raw or "").replace(";", ",").replace("|", ",").replace("\n", ",").split(","):
        p = chunk.strip().strip('"').strip("'")
        if p and p not in out:
            out.append(p)
    return out


def _roots_from_connectors(ctx: AppContext) -> list[str]:
    """从已配置券商连接的客户端路径回溯安装根，再取父目录作为扫描根。

    ★ 为什么不直接扫盘符：本项目要**通用支持多券商**，写死 ``P:/stock`` 只能
      在作者那台机器上跑。已配置的连接是"用户确实在用的客户端"这一事实的唯一
      可信来源；从它回溯既通用又不会扫到无关目录。
    """
    from connectors.install_registry import find_install_root

    roots: list[str] = []
    try:
        conns = ctx.broker_manager.all_connections()
    except (AttributeError, RuntimeError) as exc:  # 装配缺失时降级为 none
        log.debug("读取券商连接失败（按无连接处理）：%s", exc)
        return roots
    for conn in conns:
        cfg = getattr(conn, "cfg", None)
        for raw in (getattr(cfg, "client_path", "") or "",
                    getattr(cfg, "bridge_dir", "") or ""):
            if not raw:
                continue
            found = find_install_root(raw)
            if not found:
                continue
            parent = os.path.dirname(found)
            if parent and parent not in roots:
                roots.append(parent)
    return roots


def _resolve_roots(raw: str, ctx: AppContext) -> tuple[list[str], str]:
    """返回 ``(roots, source)``；source ∈ ``explicit`` / ``from_connectors`` / ``none``。"""
    explicit = _split_roots(raw)
    if explicit:
        return explicit, "explicit"
    derived = _roots_from_connectors(ctx)
    if derived:
        return derived, "from_connectors"
    return [], "none"


def _collect_installs(roots: list[str]) -> list:
    """扫描安装。**同时接受「父目录」和「安装根本身」**。

    ``discover`` 的语义是扫父目录；用户直接把安装根填进来是极常见的用法，
    若只调 ``discover`` 就会得到空列表 —— 然后界面显示"没找到任何 QMT"，
    而实际上路径完全正确。这里对每个 root 先试"它自己就是安装"，再试"它的子目录"。
    """
    from connectors.install_registry import discover, find_install_root, load_install

    found: list = []
    seen: set[str] = set()
    parents: list[str] = []
    for root in roots:
        # ★ 用 find_install_root 的**返回值**而不是布尔值：用户给的可能不是安装根，
        #   而是 userdata / bin.x64 / 甚至 XtItClient.exe 本身。原来传 max_up=0
        #   只允许"它自己就是安装根"，与 §15.3 承诺的"任意入口向上定位"不符，
        #   这类路径会被误判成"父目录"丢给 discover，结果一个安装都找不到。
        located = find_install_root(root)
        if located:
            try:
                inst = load_install(located)
            except Exception as exc:  # noqa: BLE001 — 单个坏路径不该拖垮整轮
                log.debug("加载安装失败 %s: %s", located, exc)
            else:
                key = os.path.normcase(inst.root)
                if key not in seen:
                    seen.add(key)
                    found.append(inst)
            continue
        parents.append(root)
    if parents:
        for inst in discover(parents):
            key = os.path.normcase(inst.root)
            if key not in seen:
                seen.add(key)
                found.append(inst)
    return found


def _no_roots_hint() -> str:
    return ("没有可扫描的目录：请传 roots（安装的**父目录**或安装根本身），"
            "或先在「连接管理」里配置一条指向该客户端的连接，本端点会从连接路径回溯安装根。")


# ----------------------------------------------------------------------
# 安装台账
# ----------------------------------------------------------------------
@router.get("/qmt/installs")
async def qmt_installs(roots: str = "", live: int = 0,
                       ctx: AppContext = Depends(get_ctx)):
    """本机 QMT 安装台账 + 端口冲突审计（多券商并行的前提）。

    * ``roots``：逗号分隔。留空则从已配置券商连接的客户端路径回溯。
    * ``live``：``1`` 才跑一次 ``netstat -ano`` 查**活体重复绑定**
      （同一端口被多个 PID 监听 = 串户风险）。

    ★ ``live_probe`` 是**三态**：``skipped``（没要求查）/ ``unavailable``
      （查了但拿不到结果，例如非 Windows）/ ``ok``（真比对过）。
      前两者都**不能**读成"检查了且无冲突"。
    """
    from connectors.install_registry import audit, _win_listening_ports

    resolved, source = _resolve_roots(roots, ctx)
    if not resolved:
        return ok({"roots": [], "source": source, "installs": [], "count": 0,
                   "declared_conflicts": [], "double_bound": {},
                   "live_probe": "skipped", "live_checked": False, "ok": True,
                   "hint": _no_roots_hint()})

    installs = await asyncio.to_thread(_collect_installs, resolved)
    probe = None
    if live:
        # netstat 是阻塞调用（最坏 15s 超时）⇒ 必须离开事件循环，否则整站卡住。
        # 拿不到结果时它是 None，audit 会记 "unavailable" —— 不会变成绿灯。
        listeners = await asyncio.to_thread(_win_listening_ports)
        probe = lambda: listeners  # noqa: E731

    report = audit(installs, probe=probe)
    report.update({"roots": resolved, "source": source, "count": len(installs)})
    if not installs:
        report["hint"] = ("这些目录里没有找到 QMT 安装（判据是 bin.x64/XtItClient.exe）。"
                          "roots 需要给**父目录**（例如 P:/stock）或安装根本身。")
    return ok(report)


@router.get("/qmt/installs/port-plan")
async def qmt_port_plan(roots: str = "", stride: int = 100,
                        ctx: AppContext = Depends(get_ctx)):
    """多券商并行端口方案：第 i 套安装整体 ``+ i*stride``。

    为什么必须成对偏移：``formulaworker.serverport``（58600）与 xtdata 端口
    （58610）分别服务**交易侧**与**行情侧**，只改一侧会让行情与交易指向不同实例 ——
    看的是 A 家的行情、下的是 B 家的单。

    方案自撞 / 超 65535 一律 ``400``，**不返回"看着能跑"的表**。
    """
    from connectors.install_registry import InstallError, plan_ports

    if stride <= 0:
        return err(400, "stride 必须为正整数（第 i 套安装整体 +i*stride）")
    resolved, source = _resolve_roots(roots, ctx)
    if not resolved:
        return ok({"roots": [], "source": source, "stride": stride,
                   "plan": {}, "count": 0, "hint": _no_roots_hint()})

    installs = await asyncio.to_thread(_collect_installs, resolved)
    if not installs:
        return ok({"roots": resolved, "source": source, "stride": stride,
                   "plan": {}, "count": 0,
                   "hint": "这些目录里没有找到 QMT 安装（判据是 bin.x64/XtItClient.exe）。"})
    try:
        plan = plan_ports(installs, stride=stride)
    except InstallError as exc:
        return err(400, f"端口方案不可用：{exc}")
    return ok({"roots": resolved, "source": source, "stride": stride,
               "plan": plan, "count": len(installs),
               "note": "本端点只给方案，不改任何 broker.ini；落盘需人工确认。"})


# ----------------------------------------------------------------------
# 文件单：监控目录 + staging
# ----------------------------------------------------------------------
@router.get("/qmt/file-order/monitors")
async def qmt_file_order_monitors(roots: str = "",
                                  ctx: AppContext = Depends(get_ctx)):
    """各安装各账号的 DBF 文件单监控目录，以及**总开关是否打开**。

    ``armed = is_automonitor and monitor_path``。实测四套安装全部 ``is_automonitor=0``：
    此时往 ``export_data`` 丢 DBF **不会产生任何委托**，文件只会躺在目录里。
    ``armed_count == 0`` 时返回 ``warning``，避免"我生成文件了所以会下单"的误判。
    """
    from connectors.qmt_file_order import batch_order_of, find_monitor_settings

    resolved, source = _resolve_roots(roots, ctx)
    if not resolved:
        return ok({"roots": [], "source": source, "accounts": [], "total": 0,
                   "armed_count": 0, "monitor_paths": [], "batch_order": [],
                   "hint": _no_roots_hint()})

    installs = await asyncio.to_thread(_collect_installs, resolved)
    rows: list[dict] = []
    batch_rows: list[dict] = []
    monitor_paths: list[str] = []
    for inst in installs:
        for account, batch in batch_order_of(inst.userdata):
            batch_rows.append({
                "install": inst.label,
                "root": inst.root,
                "account": account,
                "monitor_path": batch["monitor_path"],
                "key_enable": batch["key_enable"],
                "key_sequence": batch["key_sequence"],
                "implemented": False,
            })
            if batch["monitor_path"] and batch["monitor_path"] not in monitor_paths:
                monitor_paths.append(batch["monitor_path"])
        for account, setting in find_monitor_settings(inst.userdata):
            rows.append({
                "install": inst.label,
                "root": inst.root,
                "account": account,
                "monitor_path": setting.monitor_path,
                "backup_path": setting.backup_path,
                "armed": setting.armed,
                "is_automonitor": setting.is_automonitor,
                "is_incexport": setting.is_incexport,
                "is_clear": setting.is_clear,
                "orderid_feedback": setting.orderid_feedback,
            })
            for p in (setting.monitor_path, setting.backup_path):
                if p and p not in monitor_paths:
                    monitor_paths.append(p)

    armed = sum(1 for r in rows if r["armed"])
    out = {"roots": resolved, "source": source, "accounts": rows,
           "total": len(rows), "armed_count": armed,
           "monitor_paths": monitor_paths,
           # ★ 实测四套安装的 Config.xml 里都有 <batchOrderSetting>（批量单 CSV），
           #   全部 key_enable=0。此前 /monitors 只报 DBF 一条通道，会让人以为
           #   "外部下单只有文件单一种"。字段集未解 ⇒ 明确标 implemented=False，
           #   只盘点、不写、不下单。
           "batch_order": batch_rows,
           "batch_order_enabled": sum(1 for r in batch_rows if r["key_enable"]),
           "channels": {"dbf_file_order": len(rows),
                        "batch_order_csv": len(batch_rows)}}
    if batch_rows:
        out["batch_order_note"] = ("批量单 CSV 通道已发现但字段集未解，本系统不提供写入；"
                                   "key_enable 全为 0，界面里也未启用。")
    if not installs:
        out["hint"] = "这些目录里没有找到 QMT 安装（判据是 bin.x64/XtItClient.exe）。"
    elif not rows:
        out["hint"] = "找到安装，但没有账号的 Config.xml 里有 DbfMonitorSetting。"
    elif armed == 0:
        out["warning"] = ("所有账号的文件单监控都没打开（is_automonitor=0）："
                          "生成的 DBF 放进 export_data 也不会产生委托，"
                          "需要在客户端界面里开启「自动监控」。")
    return ok(out)


@router.get("/qmt/file-order/readiness")
async def qmt_file_order_readiness(roots: str = "", client_running: str = "",
                                   ctx: AppContext = Depends(get_ctx)):
    """文件单「能不能真下单」的完整前置链（**只读**，不写任何文件）。

    ``armed`` 只是总开关。本端点把监控目录、可写性、列名/列序、INSERTTIME 口径、
    客户端在线一次性摊开，并给出 ``blocking_step`` —— 避免把「开关开了」
    读成「可以下单」。

    ★ 不做写探测：armed 时往监控目录写探针文件**就等于下单**，所以只用
      ``os.access`` 判只读权限，返回里 ``probe_write_skipped=True`` 明示这一点。

    ★ ``client_running`` 由调用方上报（``1``/``0``）；不传时为 ``None``，
      该项如实标「未检查」，不默认成「在运行」。
    """
    from connectors.qmt_file_order import file_order_readiness

    resolved, source = _resolve_roots(roots, ctx)
    if not resolved:
        return ok({"roots": [], "source": source, "accounts": [], "total": 0,
                   "ready_count": 0, "hint": _no_roots_hint()})

    running = {"1": True, "true": True, "0": False, "false": False}.get(
        (client_running or "").strip().lower())
    installs = await asyncio.to_thread(_collect_installs, resolved)
    rows: list[dict] = []
    for inst in installs:
        rec = await asyncio.to_thread(file_order_readiness, inst.userdata,
                                      client_running=running)
        rec["install"] = inst.label
        rec["root"] = inst.root
        rows.append(rec)

    ready = sum(1 for r in rows if r.get("ready"))
    out = {"roots": resolved, "source": source, "accounts": rows,
           "total": len(rows), "ready_count": ready}
    if not installs:
        out["hint"] = "这些目录里没有找到 QMT 安装（判据是 bin.x64/XtItClient.exe）。"
    elif ready == 0:
        out["warning"] = ("没有一个账号 ready：文件单当前无法产生委托，"
                          "看每项的 blocking_step 定位卡在哪一步。")
    return ok(out)


def _as_int(value, default=None):
    """宽松取整数：数字直接用，纯数字字符串转 int，其余原样返回交给校验报错。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value if value is not None else default


def _canon(value) -> str:
    """把写入值与 DBF 回读值归一到同一可比形式。

    列可能被模板校准成 C 型（回读是 ``"10.5"``）或 N 型（回读是 ``10.5``），
    直接 ``str(a) != str(b)`` 会把格式差异误报成写错单；但小数**绝不能**
    用 int() 兜底 —— 那正是"10.5 写成 10"被放过的那条老路。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "T" if value else "F"
    if isinstance(value, float):
        return "%g" % value
    return str(value).strip()


def _row_from_body(item: dict):
    """把一条 JSON 行转成 ``FileOrderRow``；字段名与 DBF 列一一对应。"""
    from connectors.qmt_file_order import FileOrderRow

    return FileOrderRow(
        stock_code=str(item.get("stock_code", "") or "").strip(),
        volume=_as_int(item.get("volume", 0)),
        order_type=_as_int(item.get("order_type", -1)),
        price_type=_as_int(item.get("price_type", -99)),
        mode_price=float(item.get("mode_price", 0) or 0),
        account_id=str(item.get("account_id", "") or "").strip(),
        act_type=str(item.get("act_type", "") or ""),
        broker_type=str(item.get("broker_type", "") or ""),
        strategy=str(item.get("strategy", "") or ""),
        note=str(item.get("note", "") or ""),
        trade_param=str(item.get("trade_param", "") or ""),
        insert_time=str(item.get("insert_time", "") or ""),
        basket_path=str(item.get("basket_path", "") or ""),
        extra_param=str(item.get("extra_param", "") or ""),
        note1=str(item.get("note1", "") or ""),
    )


def _staging_dir(create: bool = True) -> str:
    """staging 目录。``create=False`` 只算路径 —— 安全检查必须在建目录**之前**，
    否则"拒绝了但已经把目录建出来了"会在用户的监控目录里留下痕迹。"""
    p = export_dir() / STAGING_SUBDIR
    if create:
        os.makedirs(p, exist_ok=True)
    return str(p)


def _guard_readable(path: str, roots: list[str]) -> str | None:
    """只允许读「安装目录下」或「export_dir 下」的文件。

    本模块有一个"读用户给的模板 DBF"的能力，若不加约束就等于开了一个
    **任意文件读取**接口（虽然只解析 DBF 头，但存在性/大小也是信息）。
    返回 ``None`` 表示放行，否则返回拒绝原因。
    """
    from connectors.qmt_file_order import is_under

    target = os.path.abspath(path)
    allowed = [os.path.abspath(r) for r in roots if r]
    allowed.append(os.path.abspath(str(export_dir())))
    if any(is_under(target, a) for a in allowed):
        return None
    return ("template_path 只允许指向安装目录（roots 下）或 export_dir 下的文件；"
            f"收到 {path!r}")


@router.post("/qmt/file-order/schema")
async def qmt_file_order_schema(body: dict, roots: str = "",
                                ctx: AppContext = Depends(get_ctx)):
    """读一份**用户在客户端里导出的模板 DBF**，返回校准后的列类型与长度。

    为什么需要这个端点
    ------------------
    本模块写 DBF 用的是「客户端自有文件的方言」（版本 0x03 / C 型 / 保守列宽）。
    **下单表的真实列宽未知** —— 列宽写错不会报错，客户端只是读不出值或读错位。
    因此正确的顺序是：

      1. 在客户端界面把 ``is_incexport`` 置 1，导出一份模板 DBF；
      2. 调本端点读出真实 schema；
      3. ``POST /qmt/file-order/stage`` 时带上 ``template_path``，按真实列宽写。

    返回 ``usable`` 只在 **15 列全齐** 时为 true。缺列时**不能**按部分列写 ——
    客户端按列序读，少一列就是后面全部错位。
    """
    from connectors.qmt_file_order import DBF_COLUMNS, FileOrderError, adopt_schema

    template = str((body or {}).get("template_path", "") or "").strip()
    if not template:
        return err(400, "template_path 不能为空（指向客户端导出的模板 DBF）")
    resolved, source = _resolve_roots(roots, ctx)
    reason = _guard_readable(template, resolved)
    if reason:
        return err(400, reason)
    if not os.path.exists(template):
        return err(400, f"模板文件不存在：{template}")

    try:
        fields = adopt_schema(template)
    except FileOrderError as exc:
        return err(400, f"模板无法校准：{exc}")

    got = [f.name for f in fields]
    missing = [c for c in DBF_COLUMNS if c not in got]
    extra = [n for n in got if n not in DBF_COLUMNS]
    return ok({
        "template_path": template,
        "fields": [{"name": f.name, "type": f.type, "length": f.length,
                    "decimals": f.decimals} for f in fields],
        "count": len(fields),
        "missing": missing,
        "extra": extra,
        "usable": not missing,
        "roots_source": source,
        "note": ("usable=false 时不要用它写单：客户端按列序读，缺列会让后面全部错位"
                 if missing else "列已齐，可在 stage 时带 template_path 按真实列宽写"),
    })


@router.post("/qmt/file-order/stage")
async def qmt_file_order_stage(body: dict, roots: str = "",
                               ctx: AppContext = Depends(get_ctx)):
    """把下单行写成一份 **DBF 待核对文件**（落在 staging 目录，不进监控目录）。

    ★ 本端点**故意不提供** ``allow_monitor_dir``。文件单是"监控目录里出现文件
      就下单"，所以"生成文件"和"交给客户端"必须由人来分成两步 ——
      一个能一步到位的 HTTP 接口，等于把"调试"和"真下单"合并成同一个调用。

    ★ 反向自检：如果 staging 目录恰好落在某个账号的监控目录里（用户把
      ``export_dir`` 指到了 ``export_data``），**拒绝生成**并报 400 ——
      这种情况下"生成即下单"，不能靠"我记得没配错"来保证安全。
      监控目录来源有**两路**：roots 扫到的安装，以及 staging 路径自己向上
      能读到的 Config.xml（roots 为空时后者仍能挡住）；两路都没有时如实报
      ``monitor_checked=false`` + warning，不把"空集上的检查"说成通过。

    ★ 三道写入护栏：列序必须完整覆盖 15 列、列宽不足**拒绝**而非静默截断、
      同名待核对文件**拒绝覆盖**。INSERTTIME 在写之前冻结，保证人核对的
      与写进去的是同一个时间戳。

    返回生成结果 + **全 15 列回读核对**（含价格与账号，否则报 500）。
    """
    from connectors.qmt_file_order import (FileOrderError, is_under, read_dbf,
                                           write_order_file)

    if not isinstance(body, dict):
        return err(400, "请求体必须是对象")
    if body.get("allow_monitor_dir"):
        return err(400, "本端点不接受 allow_monitor_dir：文件单的交付只能由人在客户端里完成，"
                        "API 只负责生成待核对文件")

    items = body.get("rows")
    if not isinstance(items, list) or not items:
        return err(400, "rows 必须是非空数组（每项一行下单记录）")
    if len(items) > MAX_STAGE_ROWS:
        return err(400, f"单次最多 {MAX_STAGE_ROWS} 行（收到 {len(items)} 行）："
                        f"文件单是丢文件即下单，批量误单不可回滚")
    bad_type = [i for i, it in enumerate(items) if not isinstance(it, dict)]
    if bad_type:
        return err(400, f"rows[{bad_type[0]}] 不是对象")

    try:
        rows = [_row_from_body(it) for it in items]
    except (TypeError, ValueError) as exc:
        return err(400, f"下单行字段类型非法：{exc}")

    problems: list[str] = []
    for i, row in enumerate(rows):
        for p in row.validate():
            problems.append(f"第 {i + 1} 行: {p}")
    if problems:
        return err(400, "下单行校验失败：" + "；".join(problems))

    # ★ 冻结 INSERTTIME：to_record() 在缺省时取当前时刻，写一次性调用、
    #   回读核对再调用一次，跨秒就会得到两个不同值 ⇒ 自检随机报 500。
    #   更要紧的是"人核对过的那份文件"与"实际写进去的值"必须是同一个时间戳。
    # ★ 2026-11-10：改走 core.clock.local_now()（唯一时刻实现），格式串逐字不变
    #   —— test_clock_unity 禁止在生产代码内联 datetime.now().strftime。
    stamp = local_now().strftime("%Y-%m-%d %H:%M:%S.000")
    for row in rows:
        if not row.insert_time:
            row.insert_time = stamp

    staging = _staging_dir(create=False)
    resolved, source = _resolve_roots(roots, ctx)
    from connectors.qmt_file_order import (batch_order_of, monitor_paths_near,
                                           monitor_paths_of)

    monitor_paths: list[str] = []

    def _add(paths) -> None:
        for p in paths:
            if p and p not in monitor_paths:
                monitor_paths.append(p)

    if resolved:
        installs = await asyncio.to_thread(_collect_installs, resolved)
        for inst in installs:
            _add(monitor_paths_of(inst.userdata))
            _add(b["monitor_path"] for _, b in batch_order_of(inst.userdata))
    # ★ 与 roots 无关的第二路：从 staging 路径自身向上读 Config.xml。
    #   少了它，roots 解析不出来时 monitor_paths 恒为空，反向自检就成了
    #   "在空集上检查" —— 看起来通过，实际什么都没挡住。
    _add(await asyncio.to_thread(monitor_paths_near, staging))

    hit = [p for p in monitor_paths if is_under(staging, p)]
    if hit:
        return err(400, f"staging 目录位于文件单监控目录内（{', '.join(hit)}）："
                        f"生成即等于下单，请先修改 export_dir 配置")

    # 可选：按用户导出的模板 DBF 校准列宽/类型（默认值只是"客户端同款方言"）
    fields = None
    template = str(body.get("template_path", "") or "").strip()
    if template:
        from connectors.qmt_file_order import DBF_COLUMNS, adopt_schema
        reason = _guard_readable(template, resolved)
        if reason:
            return err(400, reason)
        if not os.path.exists(template):
            return err(400, f"模板文件不存在：{template}")
        try:
            fields = adopt_schema(template)
        except FileOrderError as exc:
            return err(400, f"模板无法校准：{exc}")
        got = [f.name for f in fields]
        missing = [c for c in DBF_COLUMNS if c not in got]
        if missing:
            # 少一列 ⇒ 客户端按列序读会从那一列开始全部错位。宁可不写。
            return err(400, f"模板缺列 {missing}，不能按部分列写单（列序错位即写错单）")

    os.makedirs(staging, exist_ok=True)

    name = os.path.basename(str(body.get("name", "") or "")).strip()
    if not name:
        # 文件名带微秒以保证同一秒内多次 stage 不互相覆盖；格式串逐字不变。
        name = "order_%s.dbf" % local_now().strftime("%Y%m%d_%H%M%S_%f")
    if not name.lower().endswith(".dbf"):
        name += ".dbf"
    target = os.path.join(staging, name)
    if not is_under(target, staging):
        return err(400, "name 非法（不允许跳出 staging 目录）")
    if os.path.exists(target):
        # 待核对文件的语义是"人看过这一份，再决定要不要交付"。同名静默覆盖会让
        # 已核对的内容被后来一次调用换掉，而交付动作里看不出任何异常。
        return err(400, f"目标文件已存在：{name}。待核对文件不允许被静默覆盖，"
                        f"请换一个 name 或先人工处理 staging 目录里的旧文件")

    try:
        info = write_order_file(target, rows, fields=fields, monitor_paths=monitor_paths)
    except FileOrderError as exc:
        return err(400, str(exc))

    try:
        back = read_dbf(target)
    except FileOrderError as exc:
        return err(500, f"生成的 DBF 无法回读：{exc}")

    # 回读核对：**全部 15 列**逐列比对。
    # ★ 原来只比 4 列，恰好漏掉 MODE_PRICE（价格）和 ACCOUNT_ID（账号）——
    #   而 C 型列浮点被取整写成 "10" 正是本项目修过的真实缺陷。自检若不看价格，
    #   "写完自检"四个字就是假的：写错价的单会一路绿灯走到人工核对。
    from connectors.qmt_file_order import DBF_COLUMNS

    if len(back) != len(rows):
        return err(500, f"回读行数不一致：写 {len(rows)} 行、读回 {len(back)} 行")
    for i, (row, got) in enumerate(zip(rows, back)):
        want = row.to_record()
        for col in DBF_COLUMNS:
            if _canon(want[col]) != _canon(got.get(col, "")):
                return err(500, f"第 {i + 1} 行 {col} 回读不一致："
                                f"写 {want[col]!r}、读回 {got.get(col)!r}")

    audit_log("api", "qmt.file_order.stage", name,
              {"rows": len(rows), "monitor_checked": info.get("monitor_checked"),
               "columns": info.get("columns")})
    result = {
        "path": target,
        "name": name,
        "rows": len(rows),
        "bytes": info["bytes"],
        "columns": info["columns"],
        "monitor_checked": info.get("monitor_checked", False),
        "monitor_paths": monitor_paths,
        "roots_source": source,
        "readback": back,
        "delivery": "未交付：本端点只生成待核对文件，把它放进监控目录只能由人在客户端里完成",
    }
    if not result["monitor_checked"]:
        # 三态纪律："没查到任何监控目录" ≠ "查过了且不在监控目录里"。
        # 文件已生成，所以必须把这一点**放在响应里**，不能只靠调用方去猜。
        result["warning"] = ("未能定位任何客户端监控目录（roots 未解析出安装、"
                             "staging 路径附近也没有 Config.xml）："
                             "本轮没有做过「不在监控目录内」的核对，"
                             "交付前请人工确认 export_dir 不指向任何 export_data。")
    return ok(result)
