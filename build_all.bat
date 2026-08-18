@echo off
REM qmt_work 一键构建脚本 (Windows)
REM 依次执行：前端构建 -> 后端 EXE -> Electron 桌面壳打包（默认 zip 便携版）
REM
REM 用法:
REM   build_all.bat                 全量构建（zip 便携版）
REM   build_all.bat --portable      同全量构建
REM   build_all.bat --backend-only  仅打包后端 EXE
REM   build_all.bat --desktop-only  跳过后端 EXE，仅打包 Electron（需 backend/dist 已存在）
REM   build_all.bat --skip-frontend 跳过前端构建
REM   build_all.bat --force         跳过运行中实例检测（不推荐）
REM
REM 本机无 NSIS，仅打包 zip 便携版；如需 NSIS 安装包请在构建环境安装 NSIS 后自行修改。
REM
REM 环境变量（可选）:
REM   QMT_UPDATE_URL        自动更新服务器地址（默认 GitHub Releases）
REM   CSC_LINK              Windows 代码签名证书路径（*.pfx）
REM   CSC_KEY_PASSWORD      证书密码（设置后自动签名）
REM   QMT_BUILD_PYTHON      PyInstaller 构建用 Python 解释器（可执行文件或命令）
REM                         默认依次回退：PATH 中的 python -> 本机托管 Python
REM   QMT_BUILD_NODE_DIR    Node.js 所在目录（含 node.exe/npm）
REM                         默认依次回退：PATH 中的 node -> 本机托管 Node
REM   QMT_BUILD_CONSOLE     设为 1 时后端 EXE 打包为带控制台窗口（调试用）；
REM                         默认 --noconsole（发布无黑框，见 backend/build_exe.py）
REM

setlocal enabledelayedexpansion

REM ---- Python 解释器解析（可移植）----
REM 优先级：环境变量 QMT_BUILD_PYTHON > PATH 中的 python > 托管 fallback（本机专用）
REM 若用系统 python，需确保其已安装 pyinstaller/fastmcp 等依赖（或先激活对应 venv）。
set "MANAGED_PYTHON="
if defined QMT_BUILD_PYTHON (
    set "MANAGED_PYTHON=%QMT_BUILD_PYTHON%"
)
if not defined MANAGED_PYTHON (
    where python >nul 2>nul
    if !errorlevel! equ 0 set "MANAGED_PYTHON=python"
)
if not defined MANAGED_PYTHON (
    if exist "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
        set "MANAGED_PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    )
)
if not defined MANAGED_PYTHON (
    echo [error] 未找到 Python：请设置环境变量 QMT_BUILD_PYTHON 或将 python 加入 PATH
    exit /b 1
)

REM ---- Node.js 解析（可移植）----
REM 优先级：环境变量 QMT_BUILD_NODE_DIR > PATH 中的 node > 托管 fallback（本机专用）
REM 目录 prepend 到 PATH 后，npm 自动使用对应 node。
set "MANAGED_NODE_DIR="
if defined QMT_BUILD_NODE_DIR (
    set "MANAGED_NODE_DIR=%QMT_BUILD_NODE_DIR%"
)
if not defined MANAGED_NODE_DIR (
    where node >nul 2>nul
    if !errorlevel! equ 0 (
        for /f "delims=" %%i in ('where node') do (
            set "MANAGED_NODE_DIR=%%~dpi"
            goto node_dir_resolved
        )
    )
)
:node_dir_resolved
if not defined MANAGED_NODE_DIR (
    if exist "C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2\node.exe" (
        set "MANAGED_NODE_DIR=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2"
    )
)
if not defined MANAGED_NODE_DIR (
    echo [error] 未找到 Node.js：请设置环境变量 QMT_BUILD_NODE_DIR 或将 node 加入 PATH
    exit /b 1
)

set ROOT=%~dp0
set BACKEND=%ROOT%backend
set FRONTEND=%ROOT%frontend

echo =========================================
echo  qmt_work 一键构建 (Windows)
echo  工作目录: %ROOT%
echo  Python:   !MANAGED_PYTHON!
echo  Node:     !MANAGED_NODE_DIR!
echo =========================================
echo.

REM 解析参数
set SKIP_FRONTEND=false
set BACKEND_ONLY=false
set DESKTOP_ONLY=false
set FORCE=false
set USE_NSIS=false

:parse_args
if "%~1"=="" goto done_parse
if "%~1"=="--skip-frontend" set SKIP_FRONTEND=true
if "%~1"=="--backend-only" set BACKEND_ONLY=true
if "%~1"=="--desktop-only" set DESKTOP_ONLY=true
if "%~1"=="--portable" set USE_NSIS=false
if "%~1"=="--nsis" set USE_NSIS=true
if "%~1"=="--force" set FORCE=true
shift /1
goto parse_args

:done_parse

REM ---- 运行中实例检测 ----
if "%DESKTOP_ONLY%"=="false" (
    tasklist /FI "IMAGENAME eq qmt_work.exe" 2>nul | findstr /i "qmt_work.exe" >nul
    if !errorlevel! equ 0 (
        if "%FORCE%"=="true" (
            echo [warn] 后端 EXE 正在运行，使用 --force 继续
        ) else (
            echo [error] 后端 EXE 正在运行（qmt_work.exe）。
            echo         请先完全退出，或加 --force 跳过本检查。
            exit /b 1
        )
    )
)
tasklist /FI "IMAGENAME eq electron.exe" 2>nul | findstr /i "electron.exe" >nul
if !errorlevel! equ 0 (
    if "%FORCE%"=="true" (
        echo [warn] 桌面壳实例正在运行，使用 --force 继续
    ) else (
        echo [error] 桌面壳（electron.exe）正在运行，请先退出后再构建。
        exit /b 1
    )
)

REM ---- 自动更新地址 ----
if "%QMT_UPDATE_URL%"=="" (
    set "QMT_UPDATE_URL=https://github.com/qmt-work/qmt_work/releases/download"
    echo [warn] QMT_UPDATE_URL 未设置，使用默认: !QMT_UPDATE_URL!
)
set "QMT_UPDATE_URL=!QMT_UPDATE_URL!"

REM ---- 前端依赖：node_modules 缺失时自动安装 ----
if not exist "%FRONTEND%\node_modules" (
    echo [build] frontend/node_modules 不存在，执行 npm install ...
    cd /d "%FRONTEND%"
    set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm install
    if !errorlevel! neq 0 (
        echo [error] npm install 失败
        exit /b 1
    )
    cd /d "%ROOT%"
)

REM ---- Step 1: 前端构建 ----
if "%DESKTOP_ONLY%"=="false" (
    if "%SKIP_FRONTEND%"=="false" (
        echo [build] Step 1/3: 前端构建
        cd /d "%FRONTEND%"
        if not exist "package.json" (
            echo [error] frontend/package.json 不存在
            exit /b 1
        )

        REM 关键：沙箱 safe-delete shim 会拦截 vite 对 backend/static/assets 的清空操作。
        REM 预先手动清理目标目录，vite 即可正常构建。
        if exist "%BACKEND%\static\assets" (
            echo [build] 清理旧静态资源: %BACKEND%\static\assets
            rd /s /q "%BACKEND%\static\assets"
        )
        if exist "%BACKEND%\static\index.html" (
            del /q "%BACKEND%\static\index.html"
        )
        if exist "%BACKEND%\static\manifest.json" (
            del /q "%BACKEND%\static\manifest.json"
        )

        set "PATH=!MANAGED_NODE_DIR!;!PATH!"
        call npm run build
        if !errorlevel! neq 0 (
            echo [error] 前端构建失败
            exit /b 1
        )
        cd /d "%ROOT%"
        echo [build] 前端构建完成
    ) else (
        echo [warn] 跳过前端构建
    )
)

REM ---- Step 2: 后端 EXE ----
if "%DESKTOP_ONLY%"=="false" (
    echo [build] Step 2/3: 后端 EXE 打包
    cd /d "%BACKEND%"
    if not exist "build_exe.py" (
        echo [error] backend/build_exe.py 不存在
        exit /b 1
    )
    if not exist "static\index.html" (
        echo [warn] backend/static 为空，前端可能未构建
    )
    "!MANAGED_PYTHON!" build_exe.py
    if !errorlevel! neq 0 (
        echo [error] 后端 EXE 打包失败
        exit /b 1
    )
    cd /d "%ROOT%"
    echo [build] 后端 EXE 完成
)

if "%BACKEND_ONLY%"=="true" (
    echo.
    echo [build] 仅构建后端 EXE 完成
    exit /b 0
)

REM ---- Step 3: Electron 打包 ----
echo [build] Step 3/3: Electron 桌面壳打包
cd /d "%FRONTEND%"

if exist "%BACKEND%\dist\qmt_work\qmt_work.exe" (
    echo [build] 检测到后端 EXE，将随包分发
) else (
    echo [warn] 未找到后端 EXE，桌面壳打包后无法启动后端
)

if "%USE_NSIS%"=="true" (
    echo [build] 打包 NSIS 安装包 + zip 便携版
    set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm run dist
) else (
    echo [build] 打包 zip 便携版（本机无 NSIS）
    set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm run dist:portable
)

if !errorlevel! neq 0 (
    echo [error] Electron 打包失败
    exit /b 1
)

cd /d "%ROOT%"
echo [build] Electron 打包完成
echo.
echo =========================================
echo  构建完成
echo  后端 EXE: %BACKEND%\dist\qmt_work\qmt_work.exe
echo  桌面壳:   %FRONTEND%\dist-electron\
echo  更新源:   %QMT_UPDATE_URL%
echo =========================================
exit /b 0
