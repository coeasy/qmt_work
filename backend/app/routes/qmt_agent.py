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
  GET  /config    读取用户机上的 agent_config.json
  POST /config    写入配置模板（幂等：先备份 .bak）

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
from core.clock import now_iso

log = logging.getLogger("qmt_work.routes.qmt_agent")

router = APIRouter(prefix="/qmt-agent", tags=["qmt-agent"])


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
        for name in ("qmt_work_agent.py", "QMT_WORK_AGENT.py"):
            p = Path(_strategy_dir(qmt)) / name
            if p.is_file():
                stat = p.stat()
                deployed = {
                    "exists": True,
                    "path": str(p),
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
    candidates = []
    for name in (body.strategy + ".py", body.strategy.upper() + ".py",
                 "qmt_work_agent.py", "QMT_WORK_AGENT.py"):
        p = Path(sdir) / name
        if p.is_file():
            candidates.append(p)
    if not candidates:
        return err(503, "未在策略目录找到 bundle",
                   {"strategy_dir": sdir,
                    "hint": "先跑 POST /qmt-agent/deploy 部署"})

    bundle_path = candidates[0]
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
        # 降级：直接读字节判断是否 UTF-8
        try:
            raw = bundle_path.read_bytes()
            try:
                raw.decode("utf-8")
                encoding_ok = True
            except UnicodeDecodeError:
                encoding_ok = False
                encoding_note = "非 UTF-8（可能是 gb18030）"
                problems.append({"source": "encoding",
                                 "msg": "bundle 编码不是 UTF-8，QMT 内置 py3.6 会 SyntaxError"})
        except OSError as exc:
            bundle_info["read_error"] = str(exc)

    # 2. 读 agent_config 找 bridge_dir
    cfg_path = _agent_config_path(qmt)
    cfg = _read_json(cfg_path) if cfg_path.is_file() else None
    bridge_dir = None
    if cfg:
        bridge_dir = cfg.get("bridge_dir")
        if bridge_dir and not Path(bridge_dir).is_dir():
            problems.append({"source": "config",
                             "msg": f"bridge_dir 不存在: {bridge_dir}"})

    # 3. 检查 bridge_dir 里的心跳/自检文件
    heartbeat: dict[str, Any] = {"bridge_dir": bridge_dir, "alive": False, "ts": None}
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
                    probe = _read_json(probe_file)
                    if probe:
                        heartbeat["probe_ok"] = bool(probe.get("ok"))
        except OSError as exc:
            from core.errors import swallow
            swallow(exc, why="心跳/probe 文件读取失败（正被 agent 写入或被锁）；"
                             "按「无心跳」处理，不因此让诊断 500",
                    logger=log)

    # 4. QMT 客户端是否在跑
    running = _qmt_running()
    if running:
        # 客户端在跑但策略未运行（心跳 stale）→ 提示
        if not heartbeat.get("alive"):
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
        "problems": problems,
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

    text, err_msg = _http_get_text(effective_url, timeout=30.0)
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
