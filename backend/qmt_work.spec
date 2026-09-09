# -*- mode: python ; coding: utf-8 -*-
# 桌面打包用 PyInstaller 规格：把 FastAPI 后端连同前端静态产物打包成单目录可执行，
# 供 electron-builder 经 extraResources 吸入。产物：backend/dist/qmt_work/qmt_work.exe
# （与 electron/main.cjs 的 resources/backend/qmt_work/qmt_work.exe 对应）。
import os
from PyInstaller.utils.hooks import collect_submodules

def _find_backend(start):
    # PyInstaller spec 中 __file__ 未定义、SPECPATH 解析不稳定；
    # 向上回溯到真正包含 run.py 的目录（即 backend/）。
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, "run.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.abspath(start)

SPEC_DIR = _find_backend(SPECPATH)

# 路由/适配器多为动态导入（main 启动时扫描 app.routes.*），必须显式收集，否则运行期 ImportError
hiddenimports = (
    collect_submodules("app")
    + collect_submodules("sync")
    + collect_submodules("xtquant_client")
    + collect_submodules("connectors")
    + collect_submodules("plugins")
    + collect_submodules("datasource")
    + [
        "uvicorn", "uvicorn.server", "uvicorn.lifespan", "uvicorn.logging",
        "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "starlette", "starlette.applications", "starlette.routing",
        "starlette.responses", "starlette.staticfiles", "starlette.websockets",
        "starlette.middleware", "starlette.middleware.cors", "starlette.middleware.gzip",
        "fastapi", "fastapi.applications", "fastapi.routing", "fastapi.middleware",
        "pydantic", "pydantic_core", "pydantic.version",
        "python_multipart", "multipart", "websockets", "httpx", "aiofiles",
        "email_validator",
    ]
)

# 随包资源：前端构建产物（vite build -> backend/static），以及可能的模板目录
datas = [(os.path.join(SPEC_DIR, "static"), "static")]
for extra in ("app/templates", "templates"):
    p = os.path.join(SPEC_DIR, extra)
    if os.path.isdir(p):
        datas.append((p, extra))

a = Analysis(
    [os.path.join(SPEC_DIR, "run.py")],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "matplotlib", "PIL", "cv2", "torch", "tensorflow", "scipy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="qmt_work",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=True,  # 保留控制台便于排障；如要无黑窗窗口改 False
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
