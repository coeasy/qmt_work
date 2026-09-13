@echo off
REM ============================================================
REM  qmt_work one-click build (Windows CMD / Git Bash)
REM  Order: frontend build -> backend EXE (PyInstaller) -> Electron
REM
REM  Usage:
REM    build_all.bat                    full build + post-build self-check
REM    build_all.bat --portable         zip portable only (default: NSIS + zip)
REM    build_all.bat --backend-only     backend EXE only
REM    build_all.bat --desktop-only     electron only (backend/dist must exist)
REM    build_all.bat --skip-frontend    skip frontend build
REM    build_all.bat --no-verify        skip post-build self-check
REM    build_all.bat --force            skip running-instance check
REM    build_all.bat --nsis             build NSIS installer
REM    build_all.bat --clean-dist       fully wipe previous backend/dist before build
REM
REM  Env (optional):
REM    QMT_UPDATE_URL    auto-update server URL (default GitHub Releases)
REM    CSC_LINK          code signing cert (*.pfx)
REM    CSC_KEY_PASSWORD  cert password
REM    QMT_PYTHON        python used for PyInstaller (default: resolve below)
REM    QMT_NODE_DIR      node install dir used by npm (default: resolve below)
REM
REM  Only backend/dist is deleted by this script. frontend/static is wiped by
REM  vite itself (emptyOutDir: true). Source, config and data/ are never touched.
REM ============================================================

setlocal enabledelayedexpansion

set ROOT=%~dp0
if "%ROOT:~ -1%"=="\" set ROOT=%ROOT:~0,-1%
set BACKEND=%ROOT%\backend
set FRONTEND=%ROOT%\frontend
set VERIFY_RC=0

REM ---------- resolve interpreters (no hardcoded absolute paths) ----------
REM Build is interpreter-sensitive: PyInstaller needs the venv with fastmcp;
REM npm needs Node 20+. Bare `python`/`npm` may point at something else.
if defined QMT_PYTHON (
    set "MANAGED_PYTHON=%QMT_PYTHON%"
) else if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
    set "MANAGED_PYTHON=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) else if exist "%BACKEND%\.venv\Scripts\python.exe" (
    set "MANAGED_PYTHON=%BACKEND%\.venv\Scripts\python.exe"
) else (
    set "MANAGED_PYTHON=python"
)

if defined QMT_NODE_DIR (
    set "MANAGED_NODE_DIR=%QMT_NODE_DIR%"
) else if exist "%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2\node.exe" (
    set "MANAGED_NODE_DIR=%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2"
) else (
    set "MANAGED_NODE_DIR="
)

echo =========================================
echo  qmt_work build (Windows)
echo  root:   %ROOT%
echo  python: %MANAGED_PYTHON%
echo  node:   %MANAGED_NODE_DIR%
echo =========================================
echo.

REM ---------- args ----------
set SKIP_FRONTEND=false
set BACKEND_ONLY=false
set DESKTOP_ONLY=false
set FORCE=false
set USE_NSIS=false
set VERIFY=true
set CLEAN_DIST=false

:parse_args
if "%~1"=="" goto done_parse
if "%~1"=="--skip-frontend" set SKIP_FRONTEND=true
if "%~1"=="--backend-only" set BACKEND_ONLY=true
if "%~1"=="--desktop-only" set DESKTOP_ONLY=true
if "%~1"=="--portable" set USE_NSIS=false
if "%~1"=="--nsis" set USE_NSIS=true
if "%~1"=="--force" set FORCE=true
if "%~1"=="--no-verify" set VERIFY=false
if "%~1"=="--clean-dist" set CLEAN_DIST=true
shift /1
goto parse_args

:done_parse

REM ---------- running-instance detection ----------
REM Building overwrites the running EXE/static, breaking file handles (frontend
REM 500, StaticFiles crash). Must stop first.
if "%DESKTOP_ONLY%"=="false" (
    tasklist /FI "IMAGENAME eq qmt_work.exe" 2>nul | findstr /i "qmt_work.exe" >nul
    if !errorlevel! equ 0 (
        if "%FORCE%"=="true" (
            echo [warn] backend EXE running, continuing with --force
        ) else (
            echo [error] backend EXE is running ^(qmt_work.exe^). exit it first, or use --force.
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

REM ---------- auto-update URL ----------
if not defined QMT_UPDATE_URL (
    set "QMT_UPDATE_URL=https://github.com/coeasy/qmt_work/releases/download"
    echo [warn] QMT_UPDATE_URL not set, using default. please set it to your repo.
)
set "QMT_UPDATE_URL=!QMT_UPDATE_URL!"

REM ---------- helper: clean stale backend/dist ----------
REM PyInstaller --noconfirm overwrites same-named files but does NOT remove
REM leftovers of the previous COLLECT (including chunk names vite renamed away),
REM producing ghost files: packaged count > source count.
:clean_dist
REM Do NOT wipe the previous PyInstaller output by default: it holds tens of
REM thousands of files and bulk deletion is blocked by safe-delete shims.
REM PyInstaller --noconfirm overwrites same-named files, so artifacts stay
REM correct; leftover stale chunks are reported after packaging (orphan list)
REM instead of silently shipping. Pass --clean-dist to force a full wipe.
if "%CLEAN_DIST%"=="true" (
    if exist "%BACKEND%\dist\qmt_work" (
        rd /s /q "%BACKEND%\dist\qmt_work"
        echo [build] cleaned %BACKEND%\dist\qmt_work
    )
) else (
    echo [build] keeping previous dist output; use --clean-dist for a full wipe
)
if exist "%BACKEND%\dist\qmt_work.exe" del /q "%BACKEND%\dist\qmt_work.exe"
goto :eof

REM ---------- helper: report frontend output state ----------
REM frontend/vite.config.js already sets emptyOutDir: true, so vite wipes
REM ../backend/static itself. Deleting it here is redundant AND trips the
REM safe-delete bulk guard (>50 files). Completeness is gated later by
REM :verify_static (missing refs / no js chunks -> fail).
:clean_static
dir /b "%BACKEND%\static\*" 2>nul | findstr /r "." >nul
if !errorlevel! equ 0 (
    echo [build] previous frontend output present; vite emptyOutDir will wipe it
)
goto :eof

REM ---------- helper: verify frontend output is complete ----------
REM Historical bug: PyInstaller started while vite was mid-write, producing an
REM EXE with only 6/32 static files -> white screen on desktop, exit code 0.
:verify_static
if not exist "%BACKEND%\static\index.html" (
    echo [error] %BACKEND%\static\index.html missing: frontend build did not run
    exit /b 1
)
if not exist "%BACKEND%\static\assets" (
    echo [error] %BACKEND%\static\assets missing
    exit /b 1
)
dir /b "%BACKEND%\static\assets\*.js" 2>nul | findstr /r "." >nul
if !errorlevel! neq 0 (
    echo [error] no js chunks in %BACKEND%\static\assets: frontend build incomplete
    exit /b 1
)
echo [build] static ready: %BACKEND%\static
goto :eof

REM ---------- helper: verify static survived into the electron package ----------
REM extraResources copies the CONTENTS of ../backend/dist into resources/backend,
REM so backend/dist/qmt_work becomes backend/qmt_work (no dist layer).
:verify_packaged_static
set "PACKED=%FRONTEND%\dist-electron\win-unpacked\resources\backend\qmt_work\_internal\static"
if not exist "!PACKED!" (
    echo [warn] packaged static not found at !PACKED!
    goto :eof
)
set SRC_N=0
set OUT_N=0
for /f %%f in ('dir /s /b "%BACKEND%\static\*" 2^>nul') do set /a SRC_N+=1
for /f %%f in ('dir /s /b "!PACKED!\*" 2^>nul') do set /a OUT_N+=1
echo   packaged static: !OUT_N! / !SRC_N! files
if !OUT_N! neq !SRC_N! (
    echo [warn] packaged static count differs from source (!OUT_N! vs !SRC_N!)
)
REM the js entry referenced by index.html must exist inside the package,
REM otherwise the desktop app renders a white screen.
powershell -NoProfile -Command ^
  "$h = Get-Content -Raw '%BACKEND%\static\index.html'; " ^
  "$m = [regex]::Match($h, '/assets/[A-Za-z0-9_.-]+\.js'); " ^
  "if (-not $m.Success) { Write-Output '  no js entry reference found, skip check' } " ^
  "elseif (Test-Path ('!PACKED!' + $m.Value)) { Write-Output ('  entry ' + $m.Value + ' present in package') } " ^
  "else { Write-Output ('[warn] package is missing entry ' + $m.Value + ' - desktop may show a white screen') }"
goto :eof

REM ---------- frontend deps ----------
if not exist "%FRONTEND%\node_modules" (
    echo [build] installing frontend deps ...
    cd /d "%FRONTEND%"
    if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call npm install
    if !errorlevel! neq 0 ( echo [error] npm install failed & exit /b 1 )
    cd /d "%ROOT%"
)

REM ---------- Step 1: frontend build ----------
if "%DESKTOP_ONLY%"=="false" (
    if "%SKIP_FRONTEND%"=="false" (
        echo [build] Step 1/3: frontend build
        cd /d "%FRONTEND%"
        if not exist "package.json" ( echo [error] frontend/package.json missing & exit /b 1 )
        call :clean_static
        if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
        call npm run build
        if !errorlevel! neq 0 ( echo [error] frontend build failed & cd /d "%ROOT%" & exit /b 1 )
        cd /d "%ROOT%"
        call :verify_static
        echo [build] frontend done
    ) else (
        echo [warn] skipping frontend build
        if not exist "%BACKEND%\static\index.html" (
            echo [error] --skip-frontend but %BACKEND%\static\index.html is missing
            exit /b 1
        )
    )
)

REM ---------- Step 2: backend EXE ----------
if "%DESKTOP_ONLY%"=="false" (
    echo [build] Step 2/3: backend EXE ^(PyInstaller^)
    call :clean_dist
    cd /d "%BACKEND%"
    if not exist "build_exe.py" ( echo [error] backend/build_exe.py missing & exit /b 1 )
    if not exist "static\index.html" echo [warn] backend/static empty, EXE cannot host frontend
    "!MANAGED_PYTHON!" build_exe.py
    if !errorlevel! neq 0 ( echo [error] backend EXE build failed & cd /d "%ROOT%" & exit /b 1 )
    if not exist "dist\qmt_work\qmt_work.exe" (
        echo [error] build_exe.py exited 0 but %BACKEND%\dist\qmt_work\qmt_work.exe is missing
        cd /d "%ROOT%"
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

REM ---------- Step 3: Electron packaging ----------
echo [build] Step 3/3: Electron shell
cd /d "%FRONTEND%"

if not exist "%BACKEND%\dist\qmt_work\qmt_work.exe" (
    echo [error] backend EXE not found, desktop shell cannot start backend
    cd /d "%ROOT%"
    exit /b 1
)
echo [build] backend EXE found, will be bundled

REM Call electron-builder directly instead of `npm run dist` / `dist:portable`:
REM those scripts chain `npm run build` AND `backend:build`, re-running the
REM frontend build and PyInstaller that we just did (pure waste + race risk).
REM The frontend is not packed into asar anyway (extraResources carries static).
if "%USE_NSIS%"=="true" (
    echo [build] NSIS installer + zip portable
    where makensis >nul 2>nul
    if !errorlevel! neq 0 if not exist "C:\Program Files (x86)\NSIS\makensis.exe" (
        echo [warn] NSIS not found; electron-builder will try to download it, otherwise zip only
    )
    if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call node node_modules\electron-builder\cli.js --win nsis zip
) else (
    echo [build] zip portable only
    if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call node node_modules\electron-builder\cli.js --win zip
)
if !errorlevel! neq 0 ( echo [error] electron build failed & cd /d "%ROOT%" & exit /b 1 )

if not exist "dist-electron\win-unpacked\qmt_work.exe" (
    echo [error] desktop artifact missing: dist-electron\win-unpacked\qmt_work.exe
    cd /d "%ROOT%"
    exit /b 1
)

cd /d "%ROOT%"
call :verify_packaged_static
echo [build] electron done

REM ---------- Step 4: post-build self-check ----------
echo.
if "%VERIFY%"=="true" (
    if exist "%ROOT%\scripts\client_start_test.py" (
        echo [build] Step 4/4: post-build self-check ^(start client, REST/WS/window^)
        "!MANAGED_PYTHON!" "%ROOT%\scripts\client_start_test.py" --target client
        if !errorlevel! neq 0 (
            echo [warn] self-check did not fully pass (see output). artifacts exist, please review.
            set VERIFY_RC=1
        ) else (
            echo [build] self-check passed: artifact is usable
        )
    ) else (
        echo [warn] scripts/client_start_test.py not found, skipping self-check
    )
) else (
    echo [skip] self-check disabled (--no-verify)
)

echo.
echo =========================================
echo  build complete
echo  backend EXE : %BACKEND%\dist\qmt_work\qmt_work.exe
echo  desktop     : %FRONTEND%\dist-electron\win-unpacked\qmt_work.exe
echo  outputs     : %FRONTEND%\dist-electron\
echo  update URL  : %QMT_UPDATE_URL%
echo =========================================
exit /b %VERIFY_RC%
