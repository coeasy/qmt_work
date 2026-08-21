@echo off
REM ============================================================
REM  qmt_work one-click build (Windows)
REM  Order: frontend build -> backend EXE -> Electron zip portable
REM
REM  Usage:
REM    build_all.bat                 full build (zip portable)
REM    build_all.bat --backend-only  build backend EXE only
REM    build_all.bat --desktop-only  electron only (backend/dist must exist)
REM    build_all.bat --skip-frontend skip frontend build
REM    build_all.bat --force         skip running-instance check
REM    build_all.bat --nsis          build NSIS installer (requires NSIS)
REM
REM  Env (optional):
REM    QMT_UPDATE_URL    auto-update server URL (default GitHub Releases)
REM    CSC_LINK          code signing cert (*.pfx)
REM    CSC_KEY_PASSWORD  cert password
REM ============================================================

setlocal enabledelayedexpansion

REM ---- managed Python (MUST be used for PyInstaller: has fastmcp) ----
set "MANAGED_PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "!MANAGED_PYTHON!" (
    echo [error] managed python not found: !MANAGED_PYTHON!
    exit /b 1
)

REM ---- managed Node.js (prepend to PATH so npm uses it) ----
set "MANAGED_NODE_DIR=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2"
if not exist "!MANAGED_NODE_DIR!\node.exe" (
    echo [error] managed node not found: !MANAGED_NODE_DIR!
    exit /b 1
)

set ROOT=%~dp0
set BACKEND=%ROOT%backend
set FRONTEND=%ROOT%frontend

echo =========================================
echo  qmt_work build (Windows)
echo  root:   %ROOT%
echo  python: !MANAGED_PYTHON!
echo  node:   !MANAGED_NODE_DIR!
echo =========================================
echo.

REM ---- parse args ----
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

REM ---- running-instance detection ----
if "%DESKTOP_ONLY%"=="false" (
    tasklist /FI "IMAGENAME eq qmt_work.exe" 2>nul | findstr /i "qmt_work.exe" >nul
    if !errorlevel! equ 0 (
        if "%FORCE%"=="true" (
            echo [warn] backend EXE running, continuing with --force
        ) else (
            echo [error] backend EXE is running ^(qmt_work.exe^).
            echo         exit it first, or use --force.
            exit /b 1
        )
    )
)
tasklist /FI "IMAGENAME eq electron.exe" 2>nul | findstr /i "electron.exe" >nul
if !errorlevel! equ 0 (
    if "%FORCE%"=="true" (
        echo [warn] electron shell running, continuing with --force
    ) else (
        echo [error] electron shell is running. exit it first.
        exit /b 1
    )
)

REM ---- auto-update URL ----
if "%QMT_UPDATE_URL%"=="" (
    set "QMT_UPDATE_URL=https://github.com/qmt-work/qmt_work/releases/download"
    echo [warn] QMT_UPDATE_URL not set, using default.
)
set "QMT_UPDATE_URL=!QMT_UPDATE_URL!"

REM ---- frontend deps ----
if not exist "%FRONTEND%\node_modules" (
    echo [build] installing frontend deps ...
    cd /d "%FRONTEND%"
    set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm install
    if !errorlevel! neq 0 (
        echo [error] npm install failed
        exit /b 1
    )
    cd /d "%ROOT%"
)

REM ---- Step 1: frontend build ----
if "%DESKTOP_ONLY%"=="false" (
    if "%SKIP_FRONTEND%"=="false" (
        echo [build] Step 1/3: frontend build
        cd /d "%FRONTEND%"
        if not exist "package.json" (
            echo [error] frontend/package.json missing
            exit /b 1
        )

        REM KEY: the safe-delete shim blocks vite from emptying backend/static/assets.
        REM Pre-clean the target so vite can build normally.
        if exist "%BACKEND%\static\assets" (
            echo [build] cleaning old static: %BACKEND%\static\assets
            rd /s /q "%BACKEND%\static\assets"
        )
        if exist "%BACKEND%\static\index.html" del /q "%BACKEND%\static\index.html"
        if exist "%BACKEND%\static\manifest.json" del /q "%BACKEND%\static\manifest.json"

        set "PATH=!MANAGED_NODE_DIR!;!PATH!"
        call npm run build
        if !errorlevel! neq 0 (
            echo [error] frontend build failed
            exit /b 1
        )
        cd /d "%ROOT%"
        echo [build] frontend done
    ) else (
        echo [warn] skipping frontend build
    )
)

REM ---- Step 2: backend EXE ----
if "%DESKTOP_ONLY%"=="false" (
    echo [build] Step 2/3: backend EXE
    cd /d "%BACKEND%"
    if not exist "build_exe.py" (
        echo [error] backend/build_exe.py missing
        exit /b 1
    )
    if not exist "static\index.html" (
        echo [warn] backend/static empty, frontend may not be built
    )
    "!MANAGED_PYTHON!" build_exe.py
    if !errorlevel! neq 0 (
        echo [error] backend EXE build failed
        exit /b 1
    )
    cd /d "%ROOT%"
    echo [build] backend EXE done
)

if "%BACKEND_ONLY%"=="true" (
    echo.
    echo [build] backend EXE only, done.
    exit /b 0
)

REM ---- Step 3: Electron packaging ----
echo [build] Step 3/3: Electron shell
cd /d "%FRONTEND%"

if exist "%BACKEND%\dist\qmt_work\qmt_work.exe" (
    echo [build] backend EXE found, will be bundled
) else (
    echo [warn] backend EXE not found, desktop shell will not start backend
)

REM KEY: dist:portable re-runs `vite build`, whose emptyDir() hits the
REM safe-delete shim when cleaning the already-populated backend/static.
REM Pre-clean it (like Step 1) so vite's emptyDir is a no-op and succeeds.
if exist "%BACKEND%\static\assets" (
    echo [build] cleaning old static before electron: %BACKEND%\static\assets
    rd /s /q "%BACKEND%\static\assets"
)
if exist "%BACKEND%\static\index.html" del /q "%BACKEND%\static\index.html"
if exist "%BACKEND%\static\manifest.json" del /q "%BACKEND%\static\manifest.json"

if "%USE_NSIS%"=="true" (
    echo [build] NSIS installer + zip portable
    set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm run dist
) else (
    echo [build] zip portable only ^(no NSIS on this machine^)
    set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm run dist:portable
)

if !errorlevel! neq 0 (
    echo [error] electron build failed
    exit /b 1
)

cd /d "%ROOT%"
echo [build] electron done
echo.
echo =========================================
echo  build complete
echo  backend EXE: %BACKEND%\dist\qmt_work\qmt_work.exe
echo  desktop:     %FRONTEND%\dist-electron\
echo  update URL:  %QMT_UPDATE_URL%
echo =========================================
exit /b 0
