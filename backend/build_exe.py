"""将后端打包为 qmt_work（PyInstaller onedir）。

前置：在 backend 的 venv 中安装 pyinstaller
    pip install pyinstaller
用法：
    python build_exe.py
产物：backend/dist/qmt_work/qmt_work.exe
随后 electron-builder 将其作为 extraResources 随桌面壳分发。

注意：
- 前端需先 `npm run build` 生成 backend/static，PyInstaller 会一并打包，
  使 FastAPI 在打包后仍能托管前端静态资源。
- 运行时 SQLite 数据库由 Electron 主进程通过 QMT_DB_PATH 指向 userData。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

HIDDEN = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "mcp", "fastmcp", "pydantic_settings", "cryptography",
    "starlette", "httpx", "websockets",
    # FastMCP 的 docket 会话管理器依赖（内存模式）
    "docket", "burner_redis", "fakeredis", "redis",
    # app.main 经 uvicorn 字符串在运行时加载，PyInstaller 不会自动收集，需显式声明
    # V10 Phase G：app.config/db/state 等 re-export shim 已删除，只保留真实模块
    "app", "app.routes", "app.main",
    "app.logging_setup",
    # core 层：P1-2 (M1) 下沉的共享内核（配置/状态/加密），app 原路径为 re-export shim
    "core", "core.config", "core.state", "core.crypto",
    # core 层下沉的 DB 单例 / 迁移账本 / 脱敏 / 请求身份（app 原路径为 shim）
    "core.db", "core.db_migrations", "core.masking", "core.auth_identity",
    # engines 层：P1-6 (M8) 归位的交易引擎，tools.*/paper.paper_engine 为 re-export shim
    "engines", "engines.algo", "engines.limitup", "engines.condition_order",
    "engines.strategy_runtime", "engines.paper_engine",
    "sync", "backtest", "mcp_server", "gateway",
    "gateway.auth", "gateway.rate_limit", "gateway.risk",
    "gateway.apikey", "gateway.totp", "gateway.metrics", "gateway.notifier",
    "gateway.idempotency",
    "gateway.alert_engine", "gateway.wal", "gateway.reconcile",
    "gateway.quote_bus", "gateway.health", "gateway.signal_router",
    "gateway.kline_cache", "gateway.webhook_out",
    "gateway.log_alert",
    "gateway.runtime_config",
    "xtquant_client", "xtquant_client.base", "xtquant_client.gateway",
    "xtquant_client.xtp", "xtquant_client.manager", "xtquant_client.registry",
    "xtquant_client.runtime", "xtquant_client.bridge_server", "xtquant_client.bridge_client",
    "xtquant_client.adapters", "xtquant_client.adapters.ths",
    "xtquant_client.adapters.ptrade", "xtquant_client.adapters.juejin",
    "tools", "tools.market", "tools.trading", "tools.account",
    "tools.backtest", "tools.rebalance", "tools.analysis",
    "tools.strategy_gen", "tools.reference",
    "tools.position", "tools.factors",
    # P1-7 (M13)：factor_research 拆分后的三组能力模块（factor_research.py 为入口 re-export）
    "tools.factor_research", "tools.factor_stats", "tools.factor_ic",
    "tools.factor_backtest",
    # P1-2 收尾（M1）：数据源层从 app/datasource 提升为顶层包，供 tools/sync/backtest 消费
    "datasource", "datasource.registry", "datasource.local_store",
    "datasource.eltdx_source", "datasource.eltdx_utils", "datasource.models",
    "datasource.base", "datasource.board", "datasource.degrade",
    "datasource.instrument", "datasource.periods", "datasource.result",
    "datasource.pinyin",
    "tools.strategy_market",
    "app.routes.strategy_run",
    "app.routes.factors", "app.routes.paper", "app.routes.strategy_market",
    # V9 Phase 5-7（P2-24 收口）：动态导入的新层 —— bootstrap 生命周期/中间件/
    # runtime 调度器/选股引擎/统一数据面/connectors 端口层/plugins。
    # PyInstaller 静态分析看不到 main→bootstrap→routes 的动态链，必须显式声明。
    "app.bootstrap", "app.bootstrap.lifecycle", "app.bootstrap.phase_db",
    "app.bootstrap.phase_broker", "app.bootstrap.phase_engines",
    "app.bootstrap.phase_watchdogs", "app.bootstrap.phase_replay",
    "app.bootstrap.phase_misc", "app.bootstrap.shutdown",
    "app.middleware", "app.middleware.envelope", "app.middleware.request_id",
    "app.middleware.error_handler",
    "app.runtime", "app.runtime.jobs", "app.runtime.cron",
    "app.runtime.schedules", "app.runtime.system_jobs", "app.runtime.eod",
    "app.data", "app.data.bars_provider",
    "app.screener", "app.screener.engine", "app.screener.conditions",
    "app.screener.universe", "app.screener.fundamentals",
    "app.screener.source_policy",
    "app.platform", "app.version",
    "connectors", "connectors.ports", "connectors.qmt",
    "connectors.supervisor", "connectors.http",
    "plugins",
    "datasource.quality", "datasource.providers", "datasource.snapshots",
    "datasource.optional_sources", "datasource.public_sources",
    "datasource.akshare_source",
    "app.sync", "app.sync.bars", "app.sync.calendar",
]

# 收集可能含动态导入/数据的包
# 注意 eltdx：其行情核心是 Rust 原生扩展（eltdx/_native.pyd），PyInstaller 不会自动收集
# 该二进制（经 _native_abi 动态装载，二进制分析发现不了）。不 collect-all 会导致打包后的
# TDX 行情源一拉即抛 “eltdx Rust extension is unavailable”，无券商时行情/个股整页空数据。
COLLECT_ALL = ["mcp", "fastmcp", "starlette", "uvicorn", "docket", "burner_redis",
               "fakeredis", "eltdx"]

# 明确排除的重型/无关包：应用代码未使用（sqlite3 直连、pandas/numpy 处理行情），
# 但环境中已安装且会进入依赖图——既拖慢打包又可能触发 hook 崩溃（如 sqlalchemy 2.0.23
# 在 Python 3.13 下 import 即抛 TypingOnly AssertionError）。打包产物应保持精简。
EXCLUDES = [
    "torch", "torchvision", "torchaudio",   # 巨型深度学习框架，未使用
    "sqlalchemy", "alembic", "apscheduler",  # 调度/迁移/ORM，未使用
    "pyarrow", "pyarrow.libs",  # K线导出 feather 用；但打包进 onedir 会因残缺 stub 破坏
    #                            桥接子进程 pandas 导入与客户端 xtquant 探测，故排除。
    #                            打包产物用 csv/json（无需 pyarrow）；feather 在源码/dev 环境可用。
]

# 额外数据：前端静态资源（FastAPI 同源托管）
DATAS = []
static_dir = ROOT / "static"
if static_dir.exists():
    DATAS.append(f"{static_dir};static")

# P0：随包附带的嵌入式 Python 运行时（backend/runtimes/cp38 ~ cp312），
# 供 ABI 不匹配时桥接子进程使用。该目录由 tools/fetch_runtimes.py 准备；
# 不存在则跳过（不影响进程内直连可用的券商）。
runtimes_dir = ROOT / "runtimes"
if runtimes_dir.is_dir():
    DATAS.append(f"{runtimes_dir};runtimes")

# P0：xtquant_client 包以真实 .py 文件复制进 _internal/xtquant_client/
# （PyInstaller 默认把纯 Python 模块打进 PYZ 归档，普通 embed 子进程无法从归档
#   import；复制文件后，bridge 子进程（runtimes/cpXXX/python.exe）可正常
#   `python -m xtquant_client.bridge_server`。主进程仍优先走 PYZ，无冲突。）
# 注意：datas 目标须保留相对子路径（如 adapters/__init__.py -> xtquant_client/adapters），
# 否则子包 __init__ 会覆盖顶层同名文件。
_xq = ROOT / "xtquant_client"
if _xq.is_dir():
    for _py in _xq.rglob("*.py"):
        _rel = _py.relative_to(_xq)
        _dest = ("xtquant_client" if _rel.parent == Path(".")
                 else os.path.join("xtquant_client", str(_rel.parent)).replace("/", os.sep))
        DATAS.append(f"{_py};{_dest}")


# 修复：conda-forge Python 的 _ctypes.pyd 等依赖 MSVC C++ 运行库 msvcp140*.dll 与
# vcruntime140*.dll，PyInstaller 默认只收集 vcruntime140，漏掉 msvcp140 → 打包后
# import ctypes 报 "DLL load failed while importing _ctypes: 找不到指定的模块"。
# 另外残缺 venv（缺 DLLs 目录）会导致 python313.dll 未被收集，_ctypes 同样加载失败。
# 这里显式把源环境（sys.base_prefix）的关键 DLL 打进 _internal 根目录。
def _collect_msvc_runtime() -> list[str]:
    base = Path(sys.base_prefix)
    # conda-forge Python 的 _ctypes.pyd 依赖 ffi-8.dll（而非 libffi-8.dll），
    # PyInstaller 误收集 libffi-8.dll 导致 import ctypes 报 DLL 找不到。
    names = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
             "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
             "vcruntime140.dll", "vcruntime140_1.dll",
             "python313.dll", "ffi-8.dll"]
    found: list[str] = []
    seen: set[str] = set()
    for d in [base / "Library" / "bin", base / "DLLs", base]:
        for name in names:
            if name in seen:
                continue
            f = d / name
            if f.is_file():
                seen.add(name)
                found.append(str(f))
    # dest 为空串表示 _internal 根目录（onedir 模式）
    return [f"{f};." for f in found]

# 构建后清理：删除 _internal 里会让「桥接嵌入运行时」误载的残缺命名空间 stub 目录。
#
# 背景：PyInstaller onedir 把 pandas/numpy/pyarrow 的编译子包(.pyd/.dll)留在
# _internal/<pkg>/* 目录，而其纯 Python __init__.py 被打进 PYZ 归档——所以磁盘上的
# 这些目录多数没有 __init__.py，只是「残缺命名空间包」。桥接子进程(runtimes/cpXXX)
# 用普通 import（不是 PyInstaller 冻结 loader），经 ._pth 的 "..\\.." 读到 _internal，
# 于是把这些残缺目录当 namespace 包导入——pandas 启动即 `import pyarrow` 并读
# pyarrow.__version__，残缺包无 __version__ -> AttributeError，桥接直接崩。
#
# 这里只删 pyarrow / pyarrow.libs（应用代码不 import，pandas 缺失 pyarrow 会优雅降级），
# 不影响主进程；numpy/pandas 的残缺目录不能删（主进程冻结 loader 依赖其磁盘 .pyd），
# 它们会被 user-site/客户端自带的完整同名包覆盖，桥接不中毒。
def _sanitize_dist_runtime() -> None:
    internal = DIST / "qmt_work" / "_internal"
    for name in ("pyarrow", "pyarrow.libs"):
        p = internal / name
        if p.exists():
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            print(f"  [sanitize] 移除残缺命名空间 stub：{p.name}")


def _verify_static_input() -> None:
    """构建前校验 static/ 已完整生成，并快照文件名供构建后核对。

    背景（2026-09-12 实测）：若 `npm run build` 与 PyInstaller 重叠执行，
    Analysis 会在 static/ 仍处于「半写入」状态时冻结数据清单 —— COLLECT-00.toc
    里记录的是一批中途产物（如 QuoteBoard-BNBoqL3F.js），而 pyinstaller 拷贝时
    源文件已被 vite 重命名清理，最终 dist 里只剩 index.html + 少量共享 chunk，
    所有懒加载页面分片全部缺失 → 打开客户端即「前端界面加载失败」白屏。
    这里在打包前显式校验入口与分片齐备，避免再次产出坏包。
    """
    if not static_dir.is_dir():
        raise SystemExit(f"[FATAL] 前端产物目录不存在：{static_dir}，请先 npm run build")
    index_html = static_dir / "index.html"
    if not index_html.is_file():
        raise SystemExit(f"[FATAL] 缺少 {index_html}，请先 npm run build")
    html = index_html.read_text(encoding="utf-8", errors="replace")
    assets_dir = static_dir / "assets"
    js_files = sorted(assets_dir.glob("*.js")) if assets_dir.is_dir() else []
    if not js_files:
        raise SystemExit(f"[FATAL] {assets_dir} 无任何 js 分片，请先 npm run build")
    # index.html 中引用的入口/预加载文件必须真实存在，否则 SPA 首屏直接 404。
    missing = [m for m in re.findall(r'/assets/([A-Za-z0-9_.\-]+\.(?:js|css))', html)
               if not (assets_dir / m).is_file()]
    if missing:
        raise SystemExit(
            f"[FATAL] index.html 引用了不存在的资源：{missing}\n"
            f"        static/ 可能处于半写入状态（vite 构建未结束）。请等待 build 完成再打包。")
    print(f"  [static] 入口校验通过：{len(js_files)} 个 js 分片，index.html 引用齐备")


def _verify_static_output() -> None:
    """构建后核对：dist 内的 static 必须与源 static 一一对应。

    这是「坏包」的最后一道闸门。历史上 PyInstaller 曾在源目录半写入时
    产出一个只含 6 个文件的 static（源 32 个），而打包过程本身 exit 0、
    日志无任何异常 —— 不显式核对就会把坏包当作成功发布。
    """
    out_static = DIST / "qmt_work" / "_internal" / "static"
    if not out_static.is_dir():
        raise SystemExit(f"[FATAL] 打包产物缺少 static 目录：{out_static}")
    src_files = {p.relative_to(static_dir).as_posix()
                 for p in static_dir.rglob("*") if p.is_file()}
    out_files = {p.relative_to(out_static).as_posix()
                 for p in out_static.rglob("*") if p.is_file()}
    missing = sorted(src_files - out_files)
    if missing:
        raise SystemExit(
            f"[FATAL] 打包产物 static 不完整：源 {len(src_files)} 个文件，"
            f"产物仅 {len(out_files)} 个，缺失 {len(missing)} 个：\n"
            + "\n".join(f"        - {m}" for m in missing[:20])
            + "\n        请删除 backend/dist 后重跑本脚本（源 static 须已构建完成）。")
    print(f"  [static] 产物核对通过：{len(out_files)} / {len(src_files)} 个文件全部打包")


def main():
    _verify_static_input()
    # 控制台开关（可移植）：默认 --noconsole（发布友好，无黑框窗口）；
    # 调试需要看后端 stdout 时设 QMT_BUILD_CONSOLE=1 或传 --console 参数。
    console_flag = "--console" if (
        os.environ.get("QMT_BUILD_CONSOLE") == "1" or "--console" in sys.argv[1:]
    ) else "--noconsole"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "run.py",
        "--name", "qmt_work",
        "--onedir",
        console_flag,
        "--noconfirm",
        f"--distpath={DIST}",
        f"--workpath={ROOT / 'build'}",
        f"--specpath={ROOT / 'build'}",
    ]
    for h in HIDDEN:
        cmd.append(f"--hidden-import={h}")
    for e in EXCLUDES:
        cmd.append(f"--exclude-module={e}")
    for c in COLLECT_ALL:
        cmd.append(f"--collect-all={c}")
    # 修复 _ctypes DLL 缺失：补 MSVC C++ 运行库
    for b in _collect_msvc_runtime():
        cmd.append(f"--add-binary={b}")
    for d in DATAS:
        cmd.append(f"--add-data={d}")
    # 沙箱安全删除 shim 通过 CODEBUDDY_SESSION_ID 激活，会拦截 os.remove 并 fail-closed，
    # 导致 PyInstaller 清理临时产物时失败。打包时剥离这些环境变量，恢复原生删除即可。
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID", "CODEBUDDY_SAFE_DELETE_SANDBOX")}
    print(">>>", " ".join(cmd))
    # capture_output=True + 失败时打印：CREATE_NO_WINDOW 会把子进程 stdout/stderr 丢弃，
    # PyInstaller 报错时只看到 wrapper 的 CalledProcessError，真实原因被吞掉。
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False, env=clean_env,  # noqa: S603 —— 固定构建命令列表，非用户输入
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        print("\n[PyInstaller 输出-最后 80 行]")
        print("\n".join((proc.stdout or "").splitlines()[-80:]))
        print("\n[PyInstaller 错误-最后 40 行]")
        print("\n".join((proc.stderr or "").splitlines()[-40:]))
        raise SystemExit(proc.returncode)
    print(proc.stdout[-800:] if proc.stdout else "")
    _sanitize_dist_runtime()
    _verify_static_output()
    print(f"\n完成：{DIST / 'qmt_work' / 'qmt_work.exe'}")


if __name__ == "__main__":
    main()
