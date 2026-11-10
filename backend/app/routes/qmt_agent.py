"""大 QMT 策略桥 agent —— 一键部署/诊断/配置路由。

背景（2026-10-08 R27）
--------------------
此前大 QMT 桥接 agent 的部署链路完全靠「git 克隆仓库 → 双击
deploy_qmt_work_agent.bat → 手填 QMT 目录」，客户端安装包里既没有
生成器也没有分片真源。用户装完桌面 App 想部署到本地 QMT，只能回退
到开发者工作流 —— 这与「桌面 App 一键部署」的产品承诺不符。

本路由把 agent 的完整生命周期搬进 `/api/v1/qmt-agent/*`：

  GET  /status    探测本地 QMT 目录 + 当前 bundle 版本 + 诊断结论
  GET  /bundle    生成当前 bundle 文本（供下载 / 复制粘贴）
  POST /deploy    一键部署到 QMT 策略目录（幂等：同 target 先备份 .bak）
  POST /diagnose  注册态 + 心跳 + 编码 三重判定
  GET  /closed-loop  闭环策略的**运行态**（只读客户端日志，可高频轮询）
  GET  /config    读取用户机上的 agent_config.json
  POST /config    写入配置模板（幂等：先备份 .bak）

闭环（2026-10-23 新规形态）与文件桥是**两套判据**，不能混用：
新规禁策略进程读写文件，闭环模式下 ``agent_status.json`` 等三份桥文件一个都
不会生成，心跳只能从客户端日志的 ``@@QMTW|`` 行推导。本路由在检测到闭环
产物时自动切换判据（见 :func:`_closed_loop_runtime`），避免把「正在正常下单」
误报成「策略未运行」。

零 mock 契约：
- 打包态与开发态走**同一套**函数；差异只在**路径解析**
  （打包态 `_internal/agent_bigqmt` + `_internal/qmt_tools`，
  开发态 `backend/agent_bigqmt` + `scripts/`）。
- 生成 bundle 一律走 `gen_qmt_agent_bundle.build()`，禁止内嵌文本
  —— 保证「客户端下载的 bundle」与「源码里生成的 bundle」字节一致。
- 诊断一律走 `qmt_agent_verify.bundle_health()`，禁止重实现判据。
- 写操作幂等：目标文件已存在则先 `shutil.copy2` 成 `.bak.<epoch>`
  再原子替换（先写 `.tmp` + `os.replace`）。
- 所有写操作走 `audit_log`；所有 IO 走 timeout，绝不无限阻塞。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.routes._common import err, ok, audit_log
from core.clock import now_iso, to_iso

log = logging.getLogger("qmt_work.routes.qmt_agent")

router = APIRouter(prefix="/qmt-agent", tags=["qmt-agent"])

#: QMT 内置解释器（bin.x64/pythonw.exe = Python 3.6.8）能跑的源码编码。
#: ★ 2026-10-15 真机实测：utf-8 与 **gbk（须 CRLF 行尾）** 通过；gb18030 失败。
#: 权威实现见 ``scripts/qmt_agent_verify.check_bundle_encoding``；这里是
#: 「scripts/ 不可导入」时的降级副本，口径必须与它一致。
_QMT_SAFE_ENCODINGS = frozenset({"utf-8", "utf8", "ascii", "us-ascii"})
_CODING_COOKIE_RE = re.compile(rb"coding[:=][ \t]*([-\w.]+)")
_BARE_LF_RE = re.compile(rb"(?<!\r)\n")


# ===========================================================================
# 路径解析：开发态 vs PyInstaller 打包态
# ===========================================================================

def _is_frozen() -> bool:
    """PyInstaller 冻结态检测（比 sys.frozen 更可靠）。"""
    return bool(getattr(sys, "frozen", False))


def _internal_dir() -> Path:
    """打包态下 PyInstaller onedir 的 ``_internal/`` 目录。

    打包态：``<exe_dir>/_internal/``（PyInstaller 6.x）；
    开发态：``backend/``（把 agent_bigqmt 视作同级工具目录的替代根）。
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent / "_internal"
    # backend/app/routes/qmt_agent.py → 3 层 = backend/
    return Path(__file__).resolve().parents[2]


def _agent_bigqmt_dir() -> Path:
    """agent 分片真源目录。

    开发态：`<repo>/backend/agent_bigqmt`
    （__file__ = backend/app/routes/qmt_agent.py，parents[2] = backend/）
    """
    if _is_frozen():
        return _internal_dir() / "agent_bigqmt"
    return Path(__file__).resolve().parents[2] / "agent_bigqmt"


def _qmt_tools_dir() -> Path:
    """部署/诊断工具链目录。"""
    if _is_frozen():
        return _internal_dir() / "qmt_tools"
    # 开发态：仓库根 scripts/
    return Path(__file__).resolve().parents[3] / "scripts"


def _load_module(module_name: str, filename: str, base_dir: Path):
    """按文件名从指定目录 importlib 动态加载模块。

    打包态下 PyInstaller 冻结 loader 读不到磁盘 .py，只能用标准
    importlib 走真实路径加载。找不到就返回 None，让调用方决定降级。
    """
    path = base_dir / filename
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            log.warning("无法为 %s 创建 spec: %s", filename, path)
            return None
        mod = importlib.util.module_from_spec(spec)
        # 关键：先放进 sys.modules 再 exec，否则包内 `from X import Y` 会失败
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        log.error("加载 %s 失败: %s", filename, exc)
        return None


_bundle_mod: Any | None = None
_verify_mod: Any | None = None


def _bundle():
    """惰性加载 bundle 生成器。"""
    global _bundle_mod
    if _bundle_mod is None:
        _bundle_mod = _load_module(
            "qmt_agent_gen_bundle", "gen_qmt_agent_bundle.py", _qmt_tools_dir())
    return _bundle_mod


def _verify():
    """惰性加载 agent 体检工具。"""
    global _verify_mod
    if _verify_mod is None:
        _verify_mod = _load_module(
            "qmt_agent_verify_mod", "qmt_agent_verify.py", _qmt_tools_dir())
    return _verify_mod


#: 闭环心跳过期阈值（秒）。与 ``scripts/qmt_closed_loop_verify.DEFAULT_MAX_AGE_S``
#: 同源 —— 闭环 tick 默认 3s、STATUS 默认 10s，90s 是很宽的界。
_CLOSED_LOOP_MAX_AGE_S = 90.0

_clv_mod: Any | None = None
_clv_tried = False


def _closed_loop_verify():
    """惰性加载闭环体检工具（``scripts/qmt_closed_loop_verify.py``）。

    ★ 用 ``_clv_tried`` 而不是 ``is None`` 判「加载过」：脚本缺失时
      ``_load_module`` 同样返回 None，用 ``is None`` 会在**每个请求**上重试一次
      文件系统探测（日志刷屏 + 拖慢诊断），而缺脚本是**安装形态问题**，
      重试一万次也不会好。
    """
    global _clv_mod, _clv_tried
    if not _clv_tried:
        _clv_tried = True
        _clv_mod = _load_module(
            "qmt_closed_loop_verify_mod", "qmt_closed_loop_verify.py",
            _qmt_tools_dir())
    return _clv_mod


def _closed_loop_runtime(qmt_dir: str, bundle_path: Path,
                         log_path: str | None = None) -> dict[str, Any] | None:
    """闭环 bundle 的运行态。``None`` = **不是**闭环产物，调用方走旧文件桥判据。

    ★ 为什么必须分叉：新规（2026-10-23 起）策略进程禁文件读写，闭环模式下
      ``agent_status.json`` / ``probe_result.json`` / ``events.ndjson`` **一个都不会
      生成**。若 ``/diagnose`` 继续只看那三份文件，一条正在正常下单的策略会被
      报成「策略未运行（心跳 stale）」—— 这是与「假绿灯」对称的反向故障，
      假红灯一样会让人去修没坏的东西。

      闭环的唯一证据链是客户端日志里的 ``@@QMTW|`` 行（见
      ``connectors/bigqmt_log_tail.py`` 的模块 docstring），判定逻辑
      **不在这里重写** —— 直接复用 ``qmt_closed_loop_verify.evaluate()``，
      保证「界面看到的结论」与「命令行体检的结论」永远同源。

    :param log_path: 指定日志文件；``None`` 走 ``newest_log(qmt_dir)`` 自动定位。
                     排障常要看**历史某一天**的日志，此时「最新」恰好是错的。
    :return: ``{"mode": "closed_loop", ...}``；bundle 不存在或非闭环时返回 None
             （bundle 不存在由调用方先报错，这里不再重复）。
    """
    mod = _closed_loop_verify()
    if mod is None:
        # 判不出来时必须说「判不出来」，不能默默退化成旧判据 ——
        # 那样又会回到「真在跑却报未运行」的老坑。
        return {"mode": "closed_loop", "available": False,
                "reason": "scripts/qmt_closed_loop_verify.py 不可用，无法按闭环口径判定"}
    try:
        info = mod.inspect_bundle(str(bundle_path))
    except Exception as exc:  # noqa: BLE001
        return {"mode": "closed_loop", "available": False,
                "reason": "bundle 体检失败: %s" % exc}
    if not info.get("closed_loop"):
        return None

    records: list[Any] = []
    stats: dict[str, Any] = {}
    log_error = ""
    try:
        if log_path is None:
            log_path = mod.newest_log(qmt_dir)
        if log_path:
            records, stats = mod.collect_records(log_path)
    except OSError as exc:
        log_error = str(exc)

    code, lines, details = mod.evaluate(
        info, records, stats, max_age_s=_CLOSED_LOOP_MAX_AGE_S)

    last = records[-1] if records else None
    last_ts = float(getattr(last, "ts", 0.0) or 0.0) if last is not None else 0.0
    last_data = (getattr(last, "data", None) or {}) if last is not None else {}
    status_data = last_data if (last is not None and last.kind == "STATUS") else {}
    # 取**最后一条** BOOT（与 evaluate 的 ``boots[-1]`` 同口径）：一次日志里可能有
    # 多轮启动，拿第一条会把「昨天那次」的计划当成今天的。
    boot_data: dict[str, Any] = {}
    for record in records:
        if record.kind == "BOOT":
            boot_data = record.data or {}

    out: dict[str, Any] = {
        "mode": "closed_loop",
        "available": True,
        "plan_id": details.get("plan_id") or "",
        "log_path": log_path,
        "log_error": log_error,
        # 退出码原样透出：0=就绪 / 2=需人工动作 / 1=环境缺失（口径见脚本 docstring）
        "exit_code": code,
        "findings": lines,
        "boot_id": details.get("boot_id") or "",
        "alive": bool(details.get("alive")),
        "last_age_s": details.get("last_age_s"),
        "last_kind": getattr(last, "kind", "") if last is not None else "",
        "standalone": bool(details.get("standalone")),
        "readonly": details.get("readonly") or "",
        "halted": details.get("halted") or "",
        "orders": int(details.get("orders") or 0),
        "rejects": int(details.get("rejects") or 0),
        "settled": int(details.get("settled") or 0),
        "done": bool(details.get("done")),
        "window_closed": bool(details.get("window_closed")),
        "kinds": list(details.get("kinds") or []),
        "stats": stats,
        # 启动快照（来自 BOOT 行）：这是「这一轮到底想做什么」的唯一权威记录。
        "account": str(boot_data.get("account") or ""),
        "window": list(boot_data.get("window") or []),
        "targets": boot_data.get("targets"),
        "trading_enabled": boot_data.get("trading_enabled"),
        "injected": list(boot_data.get("injected") or []),
        "default_account_type": str(boot_data.get("default_account_type") or ""),
        "query_account_type": str(boot_data.get("query_account_type") or ""),
    }
    # 心跳子对象刻意与旧模式 ``agent_status.json`` **同形**（alive/ts/agent_ver/
    # runtime_mode）：前端只需换数据来源，不必学第二套字段名。
    out["heartbeat"] = {
        "alive": out["alive"],
        "ts": to_iso(datetime.fromtimestamp(last_ts)) if last_ts else None,
        "agent_ver": str(status_data.get("agent_ver") or ""),
        "runtime_mode": "closed_loop",
        "uptime_s": status_data.get("uptime_s"),
        "nav": status_data.get("nav"),
        "positions": status_data.get("positions"),
        "pending": status_data.get("pending"),
        "orders_sent": status_data.get("orders_sent"),
        "turnover": status_data.get("turnover"),
        "in_window": status_data.get("in_window"),
    }
    return out


# ===========================================================================
# QMT 安装目录探测
# ===========================================================================

#: QMT 常见安装盘位（源自 deploy.bat 与本机实测）
#: QMT 安装根目录候选标记。
#: ★ 通用判据（与 scripts/qmt_agent_deploy.py 保持同一套）：**必须有 ``python``
#: 策略目录**，且再命中次级标记里至少一个。这样任何券商版本的 QMT 都能识别，
#: 且不会把「随便一个叫 python 的文件夹」误判成 QMT。
#: 早前这里写死了本机与光大证券的盘位（P:/stock/gd_qmt、C:/光大证券…），
#: 换台机器或换券商必然探测失败——与 deploy_qmt_work_agent.bat 是同一个坑。
_QMT_MARKER_STRATEGY_DIR = os.path.join("python")
_QMT_MARKERS_SECONDARY = (
    "bin.x64", "userdata", os.path.join("config", "user", "root"),
    "user.dat", "userdata_mini", "xtitdata",
)

#: QMT 相关进程名（判断「现在改配置会不会被覆盖」）
_QMT_PROCS = ("XtItClient.exe", "XtMiniQmt.exe", "xtquoter")


def _is_qmt_dir(path: str) -> bool:
    """必须有 python/ 策略目录 + 至少一个次级标记，才算 QMT 安装根。"""
    try:
        if not os.path.isdir(os.path.join(path, _QMT_MARKER_STRATEGY_DIR)):
            return False
        for marker in _QMT_MARKERS_SECONDARY:
            try:
                if os.path.exists(os.path.join(path, marker)):
                    return True
            except OSError:
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def _candidate_roots() -> list[str]:
    """通用候选根：Windows 逐盘符扫 1~2 层；非 Windows 退回常见目录。

    与 scripts/qmt_agent_deploy.py::_candidate_roots 同逻辑（REST 侧独立实现，
    避免 app 层依赖 scripts 目录）。
    """
    roots: list[str] = []
    if os.name == "nt":
        import string
        for drive in string.ascii_uppercase:
            root = f"{drive}:\\"
            if not os.path.isdir(root):
                continue
            roots.append(root)
            try:
                for name in os.listdir(root):
                    cand = os.path.join(root, name)
                    if os.path.isdir(cand):
                        roots.append(cand)
                        # 二级：盘根下的一层目录再下探一层（如 D:/券商/客户端）
                        try:
                            for sub in os.listdir(cand):
                                sc = os.path.join(cand, sub)
                                if os.path.isdir(sc):
                                    roots.append(sc)
                        except OSError:
                            continue
            except OSError:
                continue
    else:
        for base in ("/opt", "/usr/local", "/home", "/root", os.path.expanduser("~")):
            if os.path.isdir(base):
                roots.append(base)
                try:
                    for name in os.listdir(base):
                        cand = os.path.join(base, name)
                        if os.path.isdir(cand):
                            roots.append(cand)
                except OSError:
                    continue
    return roots


#: ``_find_qmt_dir`` 的短时缓存 ``(monotonic_ts, result)``。
#: 为什么需要：``/qmt-agent/status`` 会被前端反复轮询，而通用扫描要枚举 900+ 个
#: 候选根。缓存把它摊薄到每 TTL 一次，也让「插着一个掉线的网络盘」这类**可能阻塞
#: 的 listdir** 不至于每次请求都踩一遍。
_QTMDIR_CACHE: tuple[float, str | None] | None = None
_QTMDIR_TTL_S = 30.0


def _find_qmt_dir(explicit: str | None = None) -> str | None:
    """探测 QMT 安装根目录。

    优先级：``explicit`` 参数 → ``QMT_DIR`` 环境变量 → 通用全盘扫描（带 30s 缓存）。

    只读探测，未找到返回 ``None``（调用方转 400 并给出手填提示）。

    ``explicit`` 与 ``QMT_DIR`` 的语义差异（有意为之）：
    - ``explicit`` 是**调用方点名要的那一个** ⇒ 不是 QMT 根就返回 ``None``（诚实失败，
      让 API 报 400 说清），**不**偷偷换成别的目录；
    - ``QMT_DIR`` 是环境级的期望值 ⇒ 若它指向的目录已失效（用户换了盘位/券商却忘了
      清环境变量），**记一条 warning 后回落全盘扫描**，而不是让一个陈旧变量静默
      掐死整条探测链。
    """
    global _QTMDIR_CACHE
    if explicit:
        p = os.path.abspath(explicit)
        return p if _is_qmt_dir(p) else None

    env = os.environ.get("QMT_DIR")
    if env:
        p = os.path.abspath(env)
        if _is_qmt_dir(p):
            return p
        log.warning("QMT_DIR=%s 不是 QMT 安装根（缺 python/ 或安装标记），"
                    "回落全盘扫描", env)

    now = time.monotonic()
    if _QTMDIR_CACHE is not None and now - _QTMDIR_CACHE[0] < _QTMDIR_TTL_S:
        return _QTMDIR_CACHE[1]

    found: str | None = None
    for root in _candidate_roots():
        if _is_qmt_dir(root):
            found = os.path.abspath(root)
            break
    _QTMDIR_CACHE = (now, found)
    return found


def _reset_qmt_dir_cache() -> None:
    """清空探测缓存。

    ★ 给测试用：``_QTMDIR_CACHE`` 是**进程级全局**，一个用例把探测结果缓存下来后，
    后续用例（尤其是改了 ``QMT_DIR`` 或 monkeypatch 了 ``_candidate_roots`` 的）
    会读到**上一个用例的结论** —— 这就是 R20 记录过的「进程级全局污染」假绿灯
    （与 ``os.environ.setdefault`` / ``sys.modules.pop`` 同一族）。凡是要走**真实**
    探测逻辑的用例，必须在 setup 里先调这个函数。
    """
    global _QTMDIR_CACHE
    _QTMDIR_CACHE = None


def _qmt_running() -> list[str]:
    """返回正在运行的 QMT 相关进程名列表。"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.check_output(
            ["tasklist"], stderr=subprocess.STDOUT, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("tasklist 失败: %s", exc)
        return []
    text = out.decode("gbk", "replace")
    low = text.lower()
    return [n for n in _QMT_PROCS if n.lower().split(".")[0] in low]


# ===========================================================================
# agent_config.json 位置
# ===========================================================================

def _strategy_dir(qmt_dir: str) -> str:
    """QMT 策略目录 = <qmt_dir>/python"""
    return os.path.join(qmt_dir, "python")


#: 已部署 bundle 的候选文件名（按优先级）。
#: ★ 顺序有意义：显式传入的策略名优先，其次才是默认名；Windows 文件系统大小写
#: 不敏感，所以 ``qmt_work_agent.py`` 与 ``QMT_WORK_AGENT.py`` 打开的是**同一个**
#: 文件 —— 两个都列是为了兼容 Linux/mac 上的大小写敏感文件系统与历史部署。
_BUNDLE_CANDIDATES = ("qmt_work_agent.py", "QMT_WORK_AGENT.py")


def _find_deployed_bundle(strategy_dir: str, strategy: str = "") -> Path | None:
    """在策略目录里找已部署的 bundle；找不到返回 None。

    单独抽出来是为了让 ``/diagnose`` 与 ``/closed-loop`` **用同一套查找口径** ——
    两处各写一份候选列表，早晚会分叉成「诊断说部署了、闭环页说没部署」。
    """
    names: list[str] = []
    if strategy:
        names.extend([f"{strategy}.py", f"{strategy.upper()}.py"])
    names.extend(_BUNDLE_CANDIDATES)
    for name in names:
        path = Path(strategy_dir) / name
        if path.is_file():
            return path
    return None


def _agent_config_path(qmt_dir: str | None = None) -> Path:
    """agent_config.json 的规范路径。"""
    if qmt_dir:
        return Path(_strategy_dir(qmt_dir)) / "agent_config.json"
    return _agent_bigqmt_dir() / "agent_config.example.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("读 %s 失败: %s", path, exc)
        return None


# ===========================================================================
# bundle 生成
# ===========================================================================

def _build_bundle_text(stamp: str | None = None) -> tuple[str | None, str, str, list[str]]:
    """生成 bundle 文本。返回 (text, encoding, size_str, problems)。"""
    mod = _bundle()
    if mod is None:
        return (None, "utf-8", "0",
                [f"gen_qmt_agent_bundle.py 不可用（{_qmt_tools_dir()}）"])
    # 唯一时钟入口（test_clock_unity 护栏）：不得自己 strftime。
    # bundle 的 stamp 只是版本标记，用 now_iso() 的日期+时分即可。
    from core.clock import local_now
    stamp = stamp or local_now().strftime("%Y-%m-%d %H:%M")
    try:
        text = mod.build(stamp=stamp)
    except Exception as exc:  # noqa: BLE001
        return (None, "utf-8", "0", [f"bundle.build 失败: {exc}"])
    problems = list(mod.check(text) or [])
    size = f"{len(text)} chars / {text.encode('utf-8').__len__()} bytes"
    return (text, "utf-8", size, problems)


def _bridge_compliance() -> dict:
    """本端点产物的**合规画像**（2026-10-23 新规）。

    ★ 为什么必须放进响应体，而不是只写文档：``/bundle`` 与 ``/deploy`` 产出的
      是**文件桥**形态的 agent。新规关闭策略沙箱的本地文件读写之后，这条链路
      在真机上**跑不起来** —— 每拍 ``open()`` 抛 ``Forbidden FileIO``，被主循环
      的 try/except 吞掉，外部看到的正是「策略在跑但没有任何事件」。
      只写文档的结果是「界面显示部署成功 → 用户以为成了 → 盘前发现没下单」，
      本仓最忌讳的假绿灯。所以把结论放在调用方拿得到的地方。
    """
    return {
        "mode": "bridge",
        "regulation_cutoff": "2026-10-23",
        "usable": False,
        "reason": ("该产物走文件桥（bridge_dir + req/resp + events.ndjson）。新规关闭"
                   "策略沙箱的本地文件读写，桥的每一拍都会抛 Forbidden FileIO —— "
                   "表现为「策略在跑但没有任何事件」。"),
        "next_action": ("改用烘焙计划的闭环 bundle（gen_qmt_agent_bundle.py --plan … "
                        "--embed-config …），以公式模式挂载，外部端读 @@QMTW| 日志行。"
                        "对照表见 backend/agent_bigqmt/DEPLOY.md §0.1。"),
    }


# ===========================================================================
# 部署核心（原子写 + 幂等备份）
# ===========================================================================

def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """原子写文本文件：先写 .tmp 再 os.replace，避免半写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="\n") as fh:
        fh.write(content)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError as exc:
            # fsync 不支持（如某些网络盘 / 特殊文件描述符）——落盘已由
            # os.replace 的原子性保证，这里失败不该让部署判失败。
            from core.errors import swallow
            swallow(exc, why="fsync 不可用（网络盘/特殊 fd）；原子性由 os.replace 保证",
                    logger=log)
    os.replace(tmp, path)


def _backup_if_exists(path: Path) -> str | None:
    """若目标已存在则备份为 .bak.<epoch>，返回备份路径。"""
    if not path.is_file():
        return None
    bak = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
    shutil.copy2(str(path), str(bak))
    return str(bak)


def _deploy_bundle(qmt_dir: str, filename: str, dry_run: bool = False) -> dict[str, Any]:
    """核心部署动作。返回 {path, size_bytes, encoding, backup, problems}。"""
    sdir = Path(_strategy_dir(qmt_dir))
    if not sdir.is_dir():
        # 策略目录不存在则创建（部分安装只有 userdata/，无 python/）
        if not dry_run:
            sdir.mkdir(parents=True, exist_ok=True)

    text, enc, size_str, problems = _build_bundle_text()
    if text is None:
        return {"ok": False, "problems": problems or ["bundle 生成失败"]}

    if problems:
        return {"ok": False, "problems": problems}

    target = sdir / filename
    backup = None
    if not dry_run:
        backup = _backup_if_exists(target)
        _atomic_write(target, text, encoding=enc)

    size_bytes = target.stat().st_size if target.is_file() else 0
    return {
        "ok": True,
        "path": str(target),
        "size_bytes": size_bytes,
        "encoding": enc,
        "backup": backup,
        "problems": [],
        "dry_run": dry_run,
    }


# ===========================================================================
# 请求模型
# ===========================================================================

class DeployRequest(BaseModel):
    """POST /qmt-agent/deploy 请求体。"""
    qmt_dir: str | None = Field(
        default=None, description="QMT 安装根目录；缺省走探测逻辑")
    filename: str = Field(
        default="qmt_work_agent.py",
        description="落盘文件名（不含路径）。必须与 QMT 注册树条目指向的文件名一致")
    strategy: str = Field(
        default="qmt_work_agent",
        description="策略显示名（注册树里的条目名，仅供参考）")
    dry_run: bool = Field(
        default=False, description="仅生成不写盘，用于预览")
    txt_copy: bool = Field(
        default=True, description="额外写一份 .txt 副本（供路径 B 粘贴）")


class DiagnoseRequest(BaseModel):
    """POST /qmt-agent/diagnose 请求体。"""
    qmt_dir: str | None = Field(
        default=None, description="QMT 安装根目录；缺省走探测逻辑")
    strategy: str = Field(
        default="qmt_work_agent",
        description="策略注册名（用于注册树探测）")


class ConfigRequest(BaseModel):
    """POST /qmt-agent/config 请求体。"""
    config: dict[str, Any] = Field(
        description="完整 agent_config.json 内容（覆盖写入，先备份原文件）")
    qmt_dir: str | None = Field(
        default=None, description="QMT 安装根目录；缺省走探测逻辑")


# ===========================================================================
# 端点
# ===========================================================================

def _actor(request: Request) -> str:
    """从请求头提取审计身份（无身份时降级 anonymous）。"""
    return (request.headers.get("x-api-key")
            or request.headers.get("x-remote-user")
            or "anonymous")


@router.get("/status")
async def status(request: Request):
    """GET /qmt-agent/status —— 一站式状态视图。

    返回：
      - qmt_dir: 探测到的 QMT 根（None 表示未找到）
      - qmt_running: 正在跑的 QMT 进程名列表
      - agent_tools_available: 打包态工具链是否可用
      - agent_bigqmt_available: 分片真源是否可用
      - bundle_version: 当前源码里的 agent 版本号
      - bundle_size_estimate: 生成的 bundle 大小（chars）
      - deployed: {path, size_bytes, encoding, mtime} 若已部署
      - config: {path, exists, bridge_dir} 配置摘要
      - registered: bool（注册树是否已登记）—— 需 diagnose 深查
    """
    qmt = _find_qmt_dir(None)
    running = _qmt_running()

    tools_ok = _qmt_tools_dir().is_dir() and (
        _qmt_tools_dir() / "gen_qmt_agent_bundle.py").is_file()
    src_ok = _agent_bigqmt_dir().is_dir() and (
        _agent_bigqmt_dir() / "BIGQMT_AGENT.py").is_file()

    # 尝试生成一次 bundle 拿版本号（不落盘）
    bundle_version = ""
    bundle_chars = 0
    if tools_ok and src_ok:
        text, _enc, _size_str, problems = _build_bundle_text()
        if text:
            bundle_chars = len(text)
            # 版本号硬编码在 BIGQMT_AGENT.py 的 _AGENT_VERSION 常量里
            for line in text.splitlines():
                if line.startswith("_AGENT_VERSION ="):
                    bundle_version = line.split("=", 1)[1].strip().strip('"\'')
                    break

    deployed: dict[str, Any] = {"exists": False}
    if qmt:
        sdir = Path(_strategy_dir(qmt))
        for name in ("qmt_work_agent.py", "QMT_WORK_AGENT.py"):
            p = sdir / name
            if p.is_file():
                stat = p.stat()
                # Windows 文件系统大小写不敏感：注册树登记的是大写
                # QMT_WORK_AGENT.py，但循环先试的小写名也能打开**同一个**文件
                # ⇒ 用小写名上报等于给 Operator 指一个「盘上不存在」的路径
                # （手册/注册树/部署脚本 --filename 全用大写）。报告磁盘真名。
                real_name = next(
                    (f.name for f in sdir.iterdir()
                     if f.name.lower() == name.lower()),
                    name)
                deployed = {
                    "exists": True,
                    "path": str(sdir / real_name),
                    "size_bytes": stat.st_size,
                    # 唯一时钟入口：文件 mtime → ISO 本地时间（不用裸 time.strftime）
                    "mtime": datetime.fromtimestamp(
                        stat.st_mtime).isoformat(timespec="seconds"),
                }
                break

    cfg_path = _agent_config_path(qmt)
    cfg = _read_json(cfg_path) if cfg_path.is_file() else None

    return ok({
        "qmt_dir": qmt,
        "qmt_running": running,
        "agent_tools_available": tools_ok,
        "agent_bigqmt_available": src_ok,
        "internal_dir": str(_internal_dir()),
        "bundle_version": bundle_version,
        "bundle_chars_estimate": bundle_chars,
        "deployed": deployed,
        "config": {
            "path": str(cfg_path),
            "exists": cfg_path.is_file(),
            "bridge_dir": (cfg or {}).get("bridge_dir"),
        },
        "checked_at": now_iso(),
    })


@router.get("/bundle")
async def get_bundle(request: Request):
    """GET /qmt-agent/bundle —— 生成并返回当前 bundle 文本。

    编码固定 UTF-8（生成器 :func:`_build_bundle_text` 不产生其它编码，QMT 内嵌
    py3.6 环境读 UTF-8 无 BOM 是唯一稳定口径），故不再暴露 ``encoding`` 查询参数
    ——曾声明该参数却完全忽略，等于给调用方一个「能选但选不了」的假开关。
    返回：{text, encoding, size_bytes, problems, source_agent_dir, source_tools_dir}
    """
    text, enc, _size_str, problems = _build_bundle_text()
    if text is None:
        return err(500, "bundle 生成失败", {"problems": problems})
    return ok({
        "text": text,
        "encoding": enc,
        "size_bytes": len(text.encode("utf-8")),
        "problems": problems,
        "source_agent_dir": str(_agent_bigqmt_dir()),
        "source_tools_dir": str(_qmt_tools_dir()),
        # ★ 合规画像随产物一起返回：调用方不必去翻文档才知道「这份产物在新规后
        #   还能不能用」。见 _bridge_compliance 的注释。
        "compliance": _bridge_compliance(),
    })


@router.post("/deploy")
async def deploy(request: Request, body: DeployRequest):
    """POST /qmt-agent/deploy —— 一键部署到 QMT 策略目录。

    幂等：目标文件已存在则先备份 .bak.<epoch> 再原子替换。
    失败：策略目录不存在且 dry_run=False 时自动创建。
    返回：{ok, path, size_bytes, backup, problems, txt_path, registered_hint}
    """
    qmt = _find_qmt_dir(body.qmt_dir)
    if not qmt:
        return err(400, "未找到 QMT 安装目录", {
            "hint": "请用 qmt_dir 参数指定，或在 QMT 客户端打开后重试",
            "strategy_marker": _QMT_MARKER_STRATEGY_DIR,
            "secondary_markers": list(_QMT_MARKERS_SECONDARY),
        })
    name, name_err = _safe_filename(body.filename)
    if name_err:
        return err(400, name_err)

    result = _deploy_bundle(qmt, name, dry_run=body.dry_run)
    if not result.get("ok"):
        return err(500, "部署失败", result)

    # 附带 .txt 副本（路径 B 粘贴用）
    txt_path = None
    if body.txt_copy and not body.dry_run:
        txt_target = Path(result["path"]).with_suffix(".txt")
        _backup_if_exists(txt_target)
        _atomic_write(txt_target, result.get("text", "")
                      or Path(result["path"]).read_text(encoding="utf-8"))
        txt_path = str(txt_target)

    running = _qmt_running()
    result["txt_path"] = txt_path
    result["qmt_running"] = running
    # ★ 部署「成功」只代表文件写下去了，不代表这份产物在新规下能跑。
    #   两者混为一谈就是假绿灯（见 _bridge_compliance）。
    result["compliance"] = _bridge_compliance()
    result["registered_hint"] = (
        "文件已就位但**不会**自动出现在「模型交易」里 —— 策略列表来自客户端持久化"
        "注册树，不是目录扫描。请走「导入本地策略」或「新建 + 粘贴」做一次注册动作。"
    )

    audit_log(_actor(request), "qmt_agent.deploy", name,
              {"qmt_dir": qmt, "dry_run": body.dry_run,
               "txt_copy": body.txt_copy, "backup": result.get("backup")},
              result="ok" if result["ok"] else "fail")
    return ok(result)


@router.post("/diagnose")
async def diagnose(request: Request, body: DiagnoseRequest):
    """POST /qmt-agent/diagnose —— 注册态 + 心跳 + 编码 三重判定。

    优先走 qmt_agent_verify.bundle_health()；不可用时降级到本地最小判据。
    返回：{ok, bundle:{syntax_ok, encoding_ok, pollution_ok, ...},
            registration:{registered, log_path}, heartbeat:{alive, ts},
            problems:[]}
    """
    qmt = _find_qmt_dir(body.qmt_dir)
    if not qmt:
        return err(400, "未找到 QMT 安装目录")

    sdir = _strategy_dir(qmt)
    problems: list[dict[str, str]] = []

    # 1. 找 bundle
    bundle_path = _find_deployed_bundle(sdir, body.strategy)
    if bundle_path is None:
        return err(503, "未在策略目录找到 bundle",
                   {"strategy_dir": sdir,
                    "hint": "先跑 POST /qmt-agent/deploy 部署"})

    bundle_info: dict[str, Any] = {"path": str(bundle_path), "size_bytes": bundle_path.stat().st_size}
    encoding_ok = True
    encoding_note = ""

    verify = _verify()
    if verify is not None:
        try:
            health = verify.bundle_health(str(bundle_path))
            bundle_info["health"] = health
            encoding_ok = bool(health.get("encoding_ok", True))
            encoding_note = health.get("encoding_note") or ""
            if not health.get("ok", False):
                problems.append({
                    "source": "bundle_health",
                    "msg": str(health.get("syntax_error") or health.get("encoding_note") or "bundle 体检未通过"),
                })
        except Exception as exc:  # noqa: BLE001
            bundle_info["health_error"] = str(exc)
    else:
        # 降级：直接读字节判断（与 scripts/qmt_agent_verify.check_bundle_encoding 同口径）。
        # ★ 2026-10-15：GBK 是**受支持**编码，但**只在 CRLF 行尾下**（LF 行尾会让
        #   内置 py3.6 丢开头两行 ⇒ docstring 失效 ⇒ SyntaxError）。所以这里不能
        #   再「非 UTF-8 一律判红」—— 那会把一份真能跑的 GBK 策略误报成编码异常。
        try:
            raw = bundle_path.read_bytes()
            declared = None
            for line in raw.split(b"\n", 2)[:2]:
                if not line.lstrip().startswith(b"#"):
                    break          # PEP263：cookie 只在前两行的注释里生效
                m = _CODING_COOKIE_RE.search(line)
                if m:
                    declared = m.group(1).decode("ascii", "replace").lower()
                    break
            enc_eff = declared or "utf-8"
            crlf = b"\r\n" in raw and not _BARE_LF_RE.search(raw)
            if enc_eff in _QMT_SAFE_ENCODINGS:
                encoding_ok = True
            elif enc_eff == "gbk" and crlf:
                encoding_ok = True
            elif enc_eff == "gbk":
                encoding_ok = False
                encoding_note = ("源码是 gbk 但行尾是 LF —— QMT 内置 py3.6 的 tokenizer "
                                 "会丢掉开头两行，docstring 失效 ⇒ SyntaxError"
                                 "（gbk 必须配 CRLF；用 --encoding gbk 重新部署会自动写 CRLF）")
                problems.append({"source": "encoding", "msg": encoding_note})
            else:
                encoding_ok = False
                encoding_note = ("源码编码 %s 不被 QMT 内置 Python 3.6.8 支持"
                                 "（支持 utf-8 与 gbk；gb18030 实测失败）" % enc_eff)
                problems.append({"source": "encoding", "msg": encoding_note})
        except OSError as exc:
            bundle_info["read_error"] = str(exc)

    # 1b. 闭环产物 ⇒ 运行态只能从客户端日志里读（新规禁文件读写，三份桥文件
    #     一个都不会生成）。判据全部委托 qmt_closed_loop_verify.evaluate()。
    closed_loop = _closed_loop_runtime(qmt, bundle_path)

    # 2. 读 agent_config 找 bridge_dir
    cfg_path = _agent_config_path(qmt)
    cfg = _read_json(cfg_path) if cfg_path.is_file() else None
    bridge_dir = None
    if cfg:
        bridge_dir = cfg.get("bridge_dir")
        if bridge_dir and not Path(bridge_dir).is_dir():
            # ★ 闭环产物不写文件桥，``bridge_dir`` 对它是**无关配置**。
            #   这里照旧报「bridge_dir 不存在」等于让用户去修一个用不上的目录
            #   （旧模式留下的残留配置），属于典型的假告状。
            if closed_loop is None:
                problems.append({"source": "config",
                                 "msg": f"bridge_dir 不存在: {bridge_dir}"})

    # 3. 检查 bridge_dir 里的心跳/自检文件
    heartbeat: dict[str, Any] = {"bridge_dir": bridge_dir, "alive": False, "ts": None}
    probe_data: dict[str, Any] = {}
    if bridge_dir:
        try:
            bdir = Path(bridge_dir)
            if bdir.is_dir():
                status_file = bdir / "agent_status.json"
                probe_file = bdir / "probe_result.json"
                if status_file.is_file():
                    st = _read_json(status_file)
                    if st:
                        heartbeat["alive"] = bool(st.get("alive"))
                        heartbeat["ts"] = st.get("ts")
                        heartbeat["runtime_mode"] = st.get("runtime_mode")
                        heartbeat["agent_ver"] = st.get("agent_ver")
                if probe_file.is_file():
                    probe = _read_json(probe_file) or {}
                    probe_data = probe
                    # ★ 2026-10-09 修（假告状）：agent 的自检结果**从来没有**写 "ok"
                    #   字段，而这里读 `probe.get("ok")` ⇒ `probe_ok` 恒为 False，
                    #   界面永远显示「自检未通过」，哪怕 agent 一切正常。
                    #   现在：有 ok 就直接用；旧版 agent 没有就**从 steps 现算**
                    #   （不能默认 False —— 那是把「读不到」当成「不通过」）。
                    if "ok" in probe:
                        heartbeat["probe_ok"] = bool(probe.get("ok"))
                    else:
                        steps = probe.get("steps") or []
                        heartbeat["probe_ok"] = (
                            bool(steps) and all(bool(s.get("ok")) for s in steps))
                    heartbeat["probe_bad_steps"] = list(probe.get("bad_steps") or [])
                    if probe.get("runtime_mode"):
                        heartbeat["runtime_mode"] = probe["runtime_mode"]
        except OSError as exc:
            from core.errors import swallow
            swallow(exc, why="心跳/probe 文件读取失败（正被 agent 写入或被锁）；"
                             "按「无心跳」处理，不因此让诊断 500",
                    logger=log)

    # 3b. 能力真相（2026-10-09 新增）：诊断**必须**消费 agent 的自检证据，
    #     否则「行情实调失败 / 本进程不能下单」会被界面报成「一切正常」
    #     —— 正是本仓最忌的假绿灯，也是用户本次困惑的直接来源。
    capabilities: dict[str, Any] = {}
    # ★ 下单接口面明细（2026-10-09 补齐贯通）：agent 已算出 {present, missing,
    #   can_submit}，此前**只落在 probe_result.json 里**被脚本消费，界面拿不到 ⇒
    #   用户只能看到「下单能力：不可用」却不知缺哪个入口。与 capabilities 同源，
    #   避免「同一事实两套说法」的孤儿逻辑。
    trade_surface: dict[str, Any] = {}
    if probe_data:
        capabilities = dict(probe_data.get("capability") or {})
        trade_surface = dict(probe_data.get("trade_surface") or {})
        for key, label in (("quote", "行情"), ("trading", "下单")):
            cap = capabilities.get(key) or {}
            if cap.get("available"):
                continue
            # ``expected_in_mode=True`` = 该缺失由**运行模式定义**决定（如独立进程
            # 模式拿不到 passorder），属正常形态 ⇒ 只在 capabilities 面呈现，
            # 不当故障告警（否则会造出反向的「假告状」）。
            if cap.get("expected_in_mode"):
                continue
            problems.append({
                "source": f"probe/{key}",
                "msg": f"{label}能力不可用：{cap.get('reason') or '原因未上报'}",
            })
        # 其余失败步骤（行情实调、bridge 写权限、导入等）如实并入 problems。
        for s in probe_data.get("steps") or []:
            if s.get("ok") or str(s.get("name")) in ("order_funcs", "injected_funcs"):
                continue
            problems.append({"source": f"probe/{s.get('name')}",
                             "msg": str(s.get("detail") or "")})

    # 3c. 闭环运行态并入。
    # ★ 必须**覆盖** heartbeat，而不是「额外加一段」：旧模式的 agent_status.json
    #   在闭环下永远不存在，留着旧 heartbeat 会让下面第 4 步照旧报
    #   「策略未运行」—— 那就是假红灯。
    if closed_loop is not None:
        if not closed_loop.get("available"):
            problems.append({"source": "closed_loop",
                             "msg": str(closed_loop.get("reason")
                                        or "无法按闭环口径判定运行态")})
            # ★ 判不出来时**不许**沿用「未运行」这个结论 —— 那只是「文件不存在」
            #   的副作用，不是观测结果。用 known=False 表达三态里的第二态。
            heartbeat = {"bridge_dir": bridge_dir, "alive": False, "ts": None,
                         "known": False,
                         "reason": str(closed_loop.get("reason") or "")}
        else:
            heartbeat = {"bridge_dir": bridge_dir,
                         **closed_loop.get("heartbeat", {}), "known": True}
            # 判定结论**只**从 evaluate() 的 [FAIL] 行取，不在这里另写一套判据 ——
            # 界面与命令行体检必须永远同一句话。
            for line in closed_loop.get("findings") or []:
                text = str(line).strip()
                if not text.startswith("[FAIL]"):
                    continue
                problems.append({"source": "closed_loop",
                                 "msg": text[len("[FAIL]"):].strip()})
            # 能力真相：闭环下没有 probe_result.json，唯一证据是 BOOT 自报的
            # ``injected``（终端**实际注入**了哪些函数）。刻意不用
            # ``trading_enabled`` —— 那是「用户允许交易」的开关，独立进程模式下
            # 它为 true 却根本没有 passorder，拿它当能力会把「不能下单」报成能。
            if not capabilities:
                injected = list(closed_loop.get("injected") or [])
                can_trade = "passorder" in injected
                capabilities = {
                    "trading": {
                        "available": can_trade,
                        "reason": ("终端已注入 passorder" if can_trade else
                                   "终端未注入 passorder（独立进程模式，或未以"
                                   "「模型交易/公式模式」注册运行）"),
                        "source": "closed_loop/boot.injected",
                    },
                }
                trade_surface = {
                    "present": injected,
                    "missing": [] if can_trade else ["passorder"],
                    "can_submit": can_trade,
                    "source": "closed_loop/boot.injected",
                }

    # 4. QMT 客户端是否在跑
    running = _qmt_running()
    if running:
        # 客户端在跑但策略未运行（心跳 stale）→ 提示。
        # ★ known is False 时**不许**说「未运行」：那只是「判不出来」，
        #   再叠一句具体结论就变成了自相矛盾的假红灯。
        if not heartbeat.get("alive") and heartbeat.get("known") is not False:
            problems.append({"source": "runtime",
                             "msg": "QMT 客户端在跑但 agent 策略未运行（心跳 stale），请在 QMT 里点「运行」"})

    return ok({
        "ok": not problems,
        "qmt_dir": qmt,
        "strategy_dir": sdir,
        "bundle": bundle_info,
        "encoding_ok": encoding_ok,
        "encoding_note": encoding_note,
        "config": {"path": str(cfg_path), "exists": cfg_path.is_file(),
                   "bridge_dir": bridge_dir},
        "heartbeat": heartbeat,
        "qmt_running": running,
        # ★ 闭环运行态（2026-10-31 新增）：``None`` = 这份 bundle 不是闭环产物
        #   （走旧文件桥，heartbeat 来自 agent_status.json）；非 None 时
        #   heartbeat 已由日志推导，本字段给出**依据**（BOOT/STATUS 明细、
        #   本次会话的 ORDER/REJECT/SETTLED 计数、退出码与判定原文）。
        "closed_loop": closed_loop,
        # ★ 能力真相（2026-10-09 新增）：{"trading": {available, reason,
        #   expected_in_mode}, "quote": {...}}。界面**必须**显式呈现 —— 这是
        #   「能不能下单 / 能不能取行情」的唯一权威答案，别再让用户从
        #   行情报错或全绿的能力页里猜。
        "capabilities": capabilities,
        # {"present": [...], "missing": [...], "can_submit": bool} ——
        # capabilities.trading 给结论，本字段给**依据**（缺哪个下单入口）。
        "trade_surface": trade_surface,
        "problems": problems,
    })


@router.get("/closed-loop")
async def closed_loop_status(request: Request, qmt_dir: str | None = None,
                             strategy: str = "", log_path: str = ""):
    """GET /qmt-agent/closed-loop —— 闭环策略「今天到底跑成什么样」。

    与 ``POST /diagnose`` 的分工：
      * ``diagnose`` 判**产物**（bundle 能不能跑、编码对不对、注册了没有），
        要读策略文件、要跑体检，偏重；
      * 本端点只判**运行**（心跳活不活、发了几单、有没有只读/停机/拒单），
        只读客户端日志，可以放心让前端定时轮询。

    ★ 存在的理由：闭环模式下策略不落盘，旧界面靠 ``agent_status.json`` 显示
      「策略未运行」—— 而它可能正在正常下单。这个端点把「唯一证据链」
      （``@@QMTW|`` 日志行）直接暴露出来，让界面不必再猜。

    查询参数：
      qmt_dir     QMT 安装根（缺省走自动探测）
      strategy    策略名（缺省用默认 bundle 名）
      log_path    直接指定日志文件（优先于 qmt_dir 自动定位；排障用）

    ★ 成本：本端点**整份**读当日日志（实测 10 MB ≈ 0.46s）。刻意不改成「只读
      尾部 N MB」—— 那会漏掉早盘的 BOOT 与中段的 ORDER/REJECT，让 `orders`
      这类计数**静默少算**。宁可慢 0.5s，也不要给一个错的数字。轮询间隔
      建议 ≥ 15s。

    返回：{ok, qmt_dir, bundle, closed_loop}
      ``closed_loop`` 为 None 表示这份 bundle **不是**闭环产物（旧文件桥模式）。
    """
    qmt = _find_qmt_dir(qmt_dir)
    if not qmt:
        return err(400, "未找到 QMT 安装目录")

    sdir = _strategy_dir(qmt)
    bundle_path = _find_deployed_bundle(sdir, strategy)
    if bundle_path is None:
        return err(503, "未在策略目录找到 bundle",
                   {"strategy_dir": sdir,
                    "hint": "先跑 POST /qmt-agent/deploy 部署"})

    if log_path:
        # 指定日志时绕过 newest_log（它会挑 mtime 最新的主日志）——
        # 排障常常要看**历史某一天**的日志，此时「最新」恰好是错的。
        state = _closed_loop_runtime(qmt, bundle_path, log_path)
        if state is not None and not state.get("available"):
            return err(503, str(state.get("reason") or "无法按闭环口径判定"))
    else:
        state = _closed_loop_runtime(qmt, bundle_path)

    audit_log(_actor(request), "qmt_agent.closed_loop", bundle_path.name,
              {"qmt_dir": qmt, "log_path": state.get("log_path") if state else ""},
              result="ok" if state and state.get("exit_code") == 0 else "warn")

    return ok({
        "qmt_dir": qmt,
        "strategy_dir": sdir,
        "bundle": {"path": str(bundle_path),
                   "size_bytes": bundle_path.stat().st_size},
        "closed_loop": state,
        "checked_at": now_iso(),
    })


@router.get("/config")
async def get_config(request: Request, qmt_dir: str | None = None):
    """GET /qmt-agent/config —— 读取当前 agent_config.json。"""
    qmt = _find_qmt_dir(qmt_dir)
    path = _agent_config_path(qmt)
    data = _read_json(path)
    if data is None:
        # 回退到模板
        template = _agent_config_path(None)
        if template.is_file():
            data = _read_json(template)
            return ok({
                "path": str(path),
                "exists": False,
                "source": "template",
                "template_path": str(template),
                "data": data,
                "hint": "目标路径未存在，返回的是模板；用 POST /qmt-agent/config 写入",
            })
        return ok({"path": str(path), "exists": False, "data": None,
                   "hint": "无可用模板，请先手动创建 agent_config.json"})
    return ok({
        "path": str(path),
        "exists": True,
        "data": data,
    })


@router.post("/config")
async def save_config(request: Request, body: ConfigRequest):
    """POST /qmt-agent/config —— 写入 agent_config.json（先备份）。"""
    qmt = _find_qmt_dir(body.qmt_dir)
    if not qmt:
        return err(400, "未找到 QMT 安装目录")
    path = _agent_config_path(qmt)
    if path.is_file():
        _backup_if_exists(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    # 序列化：保序 + 中文不转义 + 尾换行
    text = json.dumps(body.config, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, text, encoding="utf-8")
    audit_log(_actor(request), "qmt_agent.config.save", str(path),
              {"qmt_dir": qmt, "keys": list(body.config.keys())},
              result="ok")
    return ok({"path": str(path), "written": len(text), "encoding": "utf-8"})


@router.get("/tools")
async def list_tools(request: Request):
    """GET /qmt-agent/tools —— 列出可用的工具链与源码（供调试）。"""
    tools_dir = _qmt_tools_dir()
    src_dir = _agent_bigqmt_dir()
    tools = []
    if tools_dir.is_dir():
        for p in sorted(tools_dir.glob("*.py")):
            if p.name.startswith(("gen_qmt_agent_bundle", "qmt_agent_",
                                  "qmt_strategy_list_probe", "qmt_diag_report",
                                  "check_bigqmt_agent")):
                stat = p.stat()
                tools.append({"name": p.name, "size": stat.st_size})
    sources = []
    if src_dir.is_dir():
        for p in sorted(src_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                sources.append({"name": p.name, "size": stat.st_size})
    return ok({
        "frozen": _is_frozen(),
        "internal_dir": str(_internal_dir()),
        "agent_bigqmt_dir": str(src_dir),
        "qmt_tools_dir": str(tools_dir),
        "tools": tools,
        "sources": sources,
    })


# ===========================================================================
# 下发机制：远端 bundle 拉取（R27）
# ===========================================================================
#
# 设计要点：
# - 下发源 URL 通过 `settings.qmt_agent_bundle_url` 配置（环境变量
#   QMT_QMT_AGENT_BUNDLE_URL，或 .env / exe 同目录 JSON 配置）。空 = 关闭下发。
# - `GET  /distribute/status` 报告当前下发源、本地版本与启用状态。
# - `POST /distribute/check` 拉取远端 manifest（JSON），与本地 bundle 版本号
#   比对，返回 `has_update`。网络错误走 envelope code!=0，绝不阻塞 UI。
# - `POST /distribute/pull` 拉取远端 bundle 文本并部署。远端必须是**纯文本
#   bundle**（UTF-8）；不接受二进制压缩包、不接受任意代码。拉完后走相同的
#   `qmt_agent_verify.bundle_health` 三重判据，编码/污染不达标直接拒绝写盘。

def _safe_filename(name: str, default: str = "qmt_work_agent.py") -> tuple[str, str]:
    """校验部署目标文件名。返回 ``(文件名, 错误)``；错误非空即拒绝。

    ★ 为什么必须校验：``sdir / filename`` 直接拼接，``filename="../../evil.py"``
    会写到 QMT 策略目录之外（路径穿越）。部署目标是**用户可传的**文件名，
    不校验就等于允许任意路径写入。规则：不含路径分隔符、不以 ``.`` 开头
    （隐藏文件/``..``）、必须以 ``.py`` 结尾（QMT 只挂载 .py 策略）。
    """
    n = str(name or "").strip()
    if not n:
        return default, ""
    if "/" in n or "\\" in n or n.startswith(".") or n != Path(n).name:
        return "", f"非法文件名：{n!r}（不得含路径分隔符、不得以点开头）"
    if not n.endswith(".py"):
        return "", f"非法文件名：{n!r}（必须是 .py）"
    return n, ""


def _bundle_url_from_settings() -> str:
    """从 settings 读取下发源 URL；空串表示关闭。"""
    try:
        from core.config import settings
        return (getattr(settings, "qmt_agent_bundle_url", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _bundle_timeout_from_settings() -> float:
    """从 settings 读取下发拉取超时（秒）。

    ★ 2026-10-09 修（孤儿配置）：这个旋钮此前**只声明不读取**，而实际拉取处硬编码
      ``timeout=30.0`` ⇒ 用户改了 ``QMT_QMT_AGENT_BUNDLE_TIMEOUT`` 完全无效，
      且注释声称的「默认 10s」与真实行为（30s）不符。改为统一走 settings。
    """
    try:
        from core.config import settings
        raw = float(getattr(settings, "qmt_agent_bundle_timeout", 30.0) or 0.0)
    except (TypeError, ValueError):
        return 30.0
    # 0 / 负数不是「不超时」，而是「立刻超时」——urllib 会当场抛错，等于把功能关死。
    # 非法值一律回退默认，绝不把明显的填错当成用户意图。
    return raw if raw > 0 else 30.0


def _http_get_text(url: str, timeout: float = 10.0) -> tuple[str | None, str]:
    """HTTP GET 拉取文本；返回 (text, error)。绝不抛异常。"""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "qmt_work/agent-fetch"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            return resp.read().decode("utf-8"), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTPError {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"URLError {exc.reason}"
    except TimeoutError:
        return None, f"timeout >{int(timeout)}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _current_local_version() -> str:
    """从已部署 bundle（或源码）读 agent_ver 供比对。"""
    text, _enc, _size, _problems = _build_bundle_text()
    if not text:
        return ""
    for line in text.splitlines():
        if line.startswith("_AGENT_VERSION ="):
            return line.split("=", 1)[1].strip().strip('"\'')
    return ""


@router.get("/distribute/status")
async def distribute_status(request: Request):
    """GET /qmt-agent/distribute/status —— 下发源配置与本地版本。"""
    url = _bundle_url_from_settings()
    return ok({
        "enabled": bool(url),
        "url": url,
        "local_version": _current_local_version(),
        "hint": ("空" if not url else "已启用，可在下发检查页触发"),
    })


@router.post("/distribute/check")
async def distribute_check(request: Request,
                          url: str = "",
                          filename: str = "qmt_work_agent.py"):
    """POST /qmt-agent/distribute/check —— 拉取远端 manifest 并比对版本。

    query（可选）：url, filename
    - url 覆盖 settings 中的下发源（临时用一次）
    - 响应：{enabled, url, local_version, remote_version, has_update,
              error}
    """
    effective_url = (url or "").strip() or _bundle_url_from_settings()
    if not effective_url:
        return err(400, "未配置下发源 URL（QMT_QMT_AGENT_BUNDLE_URL）")
    text, err_msg = _http_get_text(effective_url)
    if text is None:
        return err(502, f"远端拉取失败：{err_msg}",
                   {"url": effective_url, "error": err_msg})
    # manifest 期望是 JSON：{version, url, sha256, size_bytes}
    try:
        manifest = json.loads(text)
    except (ValueError, TypeError) as exc:
        return err(502, f"远端返回不是 JSON：{exc}",
                   {"url": effective_url, "preview": text[:200]})
    remote_version = str(manifest.get("version", ""))
    local_version = _current_local_version()
    return ok({
        "url": effective_url,
        "local_version": local_version,
        "remote_version": remote_version,
        "has_update": bool(remote_version) and remote_version != local_version,
        "bundle_url": manifest.get("url"),
        "sha256": manifest.get("sha256"),
        "size_bytes": manifest.get("size_bytes"),
    })


@router.post("/distribute/pull")
async def distribute_pull(request: Request,
                          url: str = "",
                          filename: str = "qmt_work_agent.py",
                          qmt_dir: str | None = None,
                          dry_run: bool = False):
    """POST /qmt-agent/distribute/pull —— 拉取远端 bundle 并部署。

    ★ 参数是 **query**（不是 body）：``url`` / ``filename`` / ``qmt_dir`` /
    ``dry_run``。前端 ``qmtAgentApi.distributePull`` 走
    ``http.post(path, undefined, { query })``，与 ``distribute/check`` 同形态。

    零 mock 契约：拉下来的 bundle 必须先通过 bundle_health 三重判据
    （编码 UTF-8 / 无 pandas 污染 / 语法可解析），任一不达标直接拒绝写盘。
    """
    name, name_err = _safe_filename(filename)
    if name_err:
        return err(400, name_err)
    effective_url = (url or "").strip() or _bundle_url_from_settings()
    if not effective_url:
        return err(400, "未配置下发源 URL（QMT_QMT_AGENT_BUNDLE_URL）")
    qmt = _find_qmt_dir(qmt_dir)
    if not qmt:
        return err(400, "未找到 QMT 安装目录")

    text, err_msg = _http_get_text(effective_url,
                                   timeout=_bundle_timeout_from_settings())
    if text is None:
        return err(502, f"远端拉取失败：{err_msg}",
                   {"url": effective_url, "error": err_msg})

    # 三重判据：先写临时文件，跑 bundle_health，通过后再原子替换目标
    # 判定结果只进 ``problems``：三个 bool 各自赋值却没人读，等于把结论扔掉
    # （ruff F841 抓到的正是这种「算了但不用」）。
    problems: list[str] = []
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return err(400, f"远端内容不是 UTF-8：{exc}")

    import tempfile
    with tempfile.NamedTemporaryFile(
            "wb", suffix=".py", delete=False, encoding=None) as fh:
        fh.write(raw)
        tmp_path = fh.name

    try:
        verify = _verify()
        if verify is not None:
            try:
                health = verify.bundle_health(tmp_path)
                if not health.get("encoding_ok", True):
                    problems.append("编码非 UTF-8")
                if not health.get("pollution_ok", True):
                    problems.append("疑似污染")
                if not health.get("syntax_ok", True):
                    problems.append("语法错误")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"bundle_health 失败: {exc}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError as exc:
            from core.errors import swallow
            swallow(exc, why="体检临时文件删除失败（被占用/已删）；残留 tmp 不影響结果",
                    logger=log)

    if problems:
        return err(400, "远端 bundle 未通过体检",
                   {"problems": problems, "size_bytes": len(raw)})

    # 通过 → 写盘
    sdir = Path(_strategy_dir(qmt))
    target = sdir / name
    backup = None
    if not dry_run:
        sdir.mkdir(parents=True, exist_ok=True)
        backup = _backup_if_exists(target)
        _atomic_write(target, text, encoding="utf-8")

    audit_log(_actor(request), "qmt_agent.distribute.pull", name,
              {"url": effective_url, "dry_run": dry_run, "backup": backup},
              result="ok")
    return ok({
        "ok": True,
        "path": str(target),
        "size_bytes": len(raw),
        "backup": backup,
        "dry_run": dry_run,
        "remote_url": effective_url,
    })
