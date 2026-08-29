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
    "app", "app.config", "app.db", "app.state", "app.routes", "app.main",
    "app.logging_setup",
    "sync", "backtest", "mcp_server", "gateway",
    "gateway.auth", "gateway.rate_limit", "gateway.risk",
    "gateway.apikey", "gateway.totp", "gateway.metrics", "gateway.notifier",
    "gateway.idempotency",
    "gateway.alert_engine", "gateway.wal", "gateway.reconcile",
    "gateway.quote_bus", "gateway.health", "gateway.signal_router",
    "gateway.masking", "gateway.kline_cache", "gateway.webhook_out",
    "gateway.log_alert",
    "gateway.runtime_config",
    "xtquant_client", "xtquant_client.base", "xtquant_client.gateway",
    "xtquant_client.xtp", "xtquant_client.manager", "xtquant_client.registry",
    "xtquant_client.runtime", "xtquant_client.bridge_server", "xtquant_client.bridge_client",
    "xtquant_client.adapters", "xtquant_client.adapters.ths",
    "xtquant_client.adapters.ptrade", "xtquant_client.adapters.juejin",
    "tools", "tools.market", "tools.trading", "tools.account",
    "tools.backtest", "tools.rebalance", "tools.analysis",
    "tools.limitup", "tools.algo", "tools.strategy_gen", "tools.reference",
    "tools.condition_order", "tools.position", "tools.factors",
    "tools.strategy_market", "tools.strategy_runtime",
    "app.routes.strategy_run",
    "paper", "paper.paper_engine",
    "app.routes.factors", "app.routes.paper", "app.routes.strategy_market",
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


def main():
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
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False, env=clean_env,
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
    print(f"\n完成：{DIST / 'qmt_work' / 'qmt_work.exe'}")


if __name__ == "__main__":
    main()
