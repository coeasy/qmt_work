@echo off
REM qmt_work 一键构建脚本 (Windows)
REM 依次执行：前端构建 → 后端 EXE → Electron 桌面壳打包
REM 用法:
REM   build_all.bat
REM
REM 可选参数:
REM   --desktop-only   跳过后端 EXE，仅打包 Electron
REM   --backend-only   仅打包后端 EXE
REM   --skip-frontend  跳过前端构建
REM   --dist           Electron 打包使用 NSIS 安装包

setlocal enabledelayedexpansion
set ROOT=%~dp0
set BACKEND=%ROOT%backend
set FRONTEND=%ROOT%frontend

echo =========================================
echo  qmt_work 一键构建 (Windows)
echo  工作目录: %ROOT%
echo =========================================
echo.

REM 解析参数
set SKIP_FRONTEND=false
set BACKEND_ONLY=false
set DESKTOP_ONLY=false
set USE_NSIS=false

:parse_args
if "%~1"=="" goto done_parse
if "%~1"=="--skip-frontend" set SKIP_FRONTEND=true
if "%~1"=="--backend-only" set BACKEND_ONLY=true
if "%~1"=="--desktop-only" set DESKTOP_ONLY=true
if "%~1"=="--dist" set USE_NSIS=true
shift /1
goto parse_args

:done_parse

REM ---- Step 1: 前端构建 ----
if "%DESKTOP_ONLY%"=="false" (
    if "%SKIP_FRONTEND%"=="false" (
        echo [build] Step 1/3: 前端构建
        cd /d "%FRONTEND%"
        if not exist "package.json" (
            echo [error] frontend/package.json 不存在
            exit /b 1
        )
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
    python build_exe.py
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
    echo [build] 使用 NSIS 安装包模式
    call npm run dist
) else (
    call npm run pack
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
echo =========================================
exit /b 0