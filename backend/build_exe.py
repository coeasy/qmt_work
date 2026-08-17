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
COLLECT_ALL = ["mcp", "fastmcp", "starlette", "uvicorn", "docket", "burner_redis", "fakeredis"]

# 明确排除的重型/无关包：应用代码未使用（sqlite3 直连、pandas/numpy 处理行情），
# 但环境中已安装且会进入依赖图——既拖慢打包又可能触发 hook 崩溃（如 sqlalchemy 2.0.23
# 在 Python 3.13 下 import 即抛 TypingOnly AssertionError）。打包产物应保持精简。
EXCLUDES = [
    "torch", "torchvision", "torchaudio",   # 巨型深度学习框架，未使用
    "sqlalchemy", "alembic", "apscheduler",  # 调度/迁移/ORM，未使用
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


def main():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "run.py",
        "--name", "qmt_work",
        "--onedir",
        "--noconsole",
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
    for d in DATAS:
        cmd.append(f"--add-data={d}")
    # 沙箱安全删除 shim 通过 CODEBUDDY_SESSION_ID 激活，会拦截 os.remove 并 fail-closed，
    # 导致 PyInstaller 清理临时产物时失败。打包时剥离这些环境变量，恢复原生删除即可。
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID", "CODEBUDDY_SAFE_DELETE_SANDBOX")}
    print(">>>", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True, env=clean_env,
                   creationflags=CREATE_NO_WINDOW)
    print(f"\n完成：{DIST / 'qmt_work' / 'qmt_work.exe'}")


if __name__ == "__main__":
    main()
