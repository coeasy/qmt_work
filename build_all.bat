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
REM  !! THIS FILE MUST KEEP CRLF LINE ENDINGS !!
REM  cmd.exe reads .bat line by line and mis-parses LF-only files: the
REM  parenthesised `for` / `if` blocks below get torn apart and the run dies with
REM  "'x' is not recognized as an internal or external command" /
REM  ") was unexpected at this time". Real incident: build_all.bat was stored
REM  LF-only in git -> the whole release build aborted before step 1.
REM  Do NOT run a "normalise to LF" pass over this file.
REM
REM  !! ASCII PARENTHESES INSIDE A BLOCK MUST BE ESCAPED !!
REM  Inside a parenthesised `if` / `for` block, cmd treats a bare open-paren as the start
REM  of a NESTED block and a bare close-paren as its end; nothing but `else` may follow
REM  that close-paren. So a harmless-looking line of echo text that contains a
REM  parenthesised remark and then MORE TEXT inside a block gets parsed as
REM  "close block, then the junk token `.`" and cmd aborts the ENTIRE batch file with
REM  "`. was unexpected at this time.`". Outside a block the very same line is perfectly
REM  legal, which is why this only bites once the line moves into a block.
REM  Real incident: the Step 4 self-check line aborted the run AFTER the installer was
REM  built, so the last two gates (post-build self-check + MCP end-to-end) never ran.
REM  See docs/TECH_DEBT.md::TD-19.
REM  Rule: write escaped open/close parens in echo text inside a block, or use
REM  full-width parens. A quoted path such as "C:\Program Files (x86)\..." is fine.
REM
REM  Env (optional):
REM    CSC_LINK          code signing cert (*.pfx)
REM    CSC_KEY_PASSWORD  cert password
REM    QMT_PYTHON        python used for PyInstaller (default: resolve below)
REM    QMT_NODE_DIR      node install dir used by npm (default: resolve below)
REM  注：自动更新源由 frontend-next/electron-builder.yml 的 publish 段决定，
REM      不是环境变量（旧的 QMT_UPDATE_URL 无消费者，已删除）。
REM
REM  Only backend/dist is deleted by this script. frontend-next static output is wiped
REM  by vite itself (emptyOutDir: true). Source, config and data/ are never touched.
REM ============================================================

setlocal enabledelayedexpansion

set ROOT=%~dp0
if "%ROOT:~ -1%"=="\" set ROOT=%ROOT:~0,-1%
set BACKEND=%ROOT%\backend
set FRONTEND=%ROOT%\frontend-next
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

REM  ★ 托管 Node 目录名可能带补丁后缀（如 22.22.2-3），必须用通配探测。
REM    历史缺陷：这里硬编码过 `versions\22.22.2`，而实际安装目录是 `22.22.2-3`
REM    ⇒ 探测不到托管 node ⇒ 静默退回到 PATH 上的任意 node（乃至根本没有 node），
REM    构建结果与 build_all.sh 不一致。sh 版一直是通配遍历，此处对齐。
if defined QMT_NODE_DIR (
    set "MANAGED_NODE_DIR=%QMT_NODE_DIR%"
) else (
    set "MANAGED_NODE_DIR="
    for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\node\versions\*") do (
        if not defined MANAGED_NODE_DIR if exist "%%~fd\node.exe" set "MANAGED_NODE_DIR=%%~fd"
    )
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

REM ---------- auto-update source ----------
REM 自动更新源**不在这里配置**：electron-builder 依据 frontend-next/electron-builder.yml
REM 的 publish 段生成 resources/app-update.yml，electron-updater 运行期只读那个文件。
REM （历史遗留：这里曾有 QMT_UPDATE_URL 环境变量，但**没有任何消费者** —— 设置它
REM  不会改变更新源，只会让人以为改成功了。死旋钮已删除。）
if not exist "%FRONTEND%\electron-builder.yml" (
    echo [warn] %FRONTEND%\electron-builder.yml missing: update source cannot be resolved
)

REM ---------- jump over the helper subroutines ----------
REM  ★ 下面这些 :clean_dist / :clean_static / :verify_* 都是 **子程序**：末尾的
REM    `goto :eof` 是「返回调用点」。若顶层执行自上而下流进第一个 :label，就会执行
REM    它的 `goto :eof` —— 顶层 `goto :eof` 是**结束整个脚本**，于是后面 4 个步骤
REM    一行都不会跑，而且**退出码仍是 0**（调用方以为构建成功）。
REM    历史事故：本文件自 HEAD 起一直如此 —— 跑完只打印一行
REM    "[build] keeping previous dist output" 就退出 0，产物为零。
REM    因此必须显式跳到 :main，让子程序只能经 `call` 进入。
goto :main

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
REM frontend-next/vite.config.ts already sets emptyOutDir: true, so vite wipes
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

REM ---------- main flow resumes here ----------
:main

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
        if not exist "package.json" ( echo [error] frontend-next/package.json missing & exit /b 1 )
        call :clean_static
        if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
        call npm run build
        if !errorlevel! neq 0 ( echo [error] frontend build failed & cd /d "%ROOT%" & exit /b 1 )
        cd /d "%ROOT%"
        call :verify_static
        REM :verify_static returns via `exit /b 1`; under `call` that only returns to the
        REM call site, so the script would keep going -- an incomplete static bundle then
        REM got packaged into the EXE as a blank window while the exit code stayed 0.
        if !errorlevel! neq 0 ( echo [error] frontend output incomplete & exit /b 1 )
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
REM
REM  ★ 必须显式 --publish never：
REM    electron-builder 在【CI 环境变量为真】且当前提交无 git tag 时，会自行把发布策略
REM    推断成 onTagOrDraft（app-builder-lib/out/publish/PublishManager.js L56~L58），
REM    于是【产物已全部生成之后】去创建 GitHubPublisher ⇒ 缺 GH_TOKEN 直接抛错 ⇒
REM    退出码 1，且收尾的 update-info 任务被整段跳过 —— **latest.yml 不落盘**。
REM    实测（CI=true、无 GH_TOKEN）：zip 与 Setup.exe 都生成了，latest.yml 缺失、exit 1。
REM    latest.yml 的写出条件只依赖 event.isWriteUpdateInfo（同文件 L158），与 isPublish 无关，
REM    因此 --publish never 既不会削掉自动更新清单，也不会削掉包内 resources/app-update.yml。
REM    上传资产是 scripts/publish_release.py 的职责，构建脚本一律不发布。
if "%USE_NSIS%"=="true" (
    echo [build] NSIS installer + zip portable
    where makensis >nul 2>nul
    if !errorlevel! neq 0 if not exist "C:\Program Files (x86)\NSIS\makensis.exe" (
        echo [warn] NSIS not found; electron-builder will try to download it, otherwise zip only
    )
    if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call node node_modules\electron-builder\cli.js --win nsis zip --publish never
) else (
    echo [build] zip portable only
    if defined MANAGED_NODE_DIR set "PATH=!MANAGED_NODE_DIR!;!PATH!"
    call node node_modules\electron-builder\cli.js --win zip --publish never
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

REM ---------- auto-update manifest gate ----------
REM  ★ latest.yml **只由 NSIS target 写出**（zip 便携版根本不写）。历史上出现过
REM    「安装包已生成、latest.yml 缺失」的半成品发布：客户端「检查更新」会**静默失效**，
REM    界面上毫无提示，事后极难归因。
REM    build_all.sh 一直有此硬核对，而 .bat 缺这一段 —— Windows 侧发布等于少了一道闸门，
REM    同一个 --nsis 在两个脚本下的把关强度不一致。此处对齐。
if "%USE_NSIS%"=="true" (
    if not exist "%FRONTEND%\dist-electron\latest.yml" (
        echo [error] NSIS build finished but latest.yml is missing:
        echo         %FRONTEND%\dist-electron\latest.yml
        echo         客户端「检查更新」会静默失效（下载不到清单且无任何提示）。
        echo         常见原因：收尾删除 nsis 中间包被安全删除护栏拦下。
        echo         修法：清理 dist-electron 后重跑  build_all.bat --desktop-only --nsis
        exit /b 1
    )
    set "REPO_VER="
    for /f "usebackq delims=" %%v in ("%ROOT%\VERSION") do if not defined REPO_VER set "REPO_VER=%%v"
    set "YML_VER="
    set "YML_PATH="
    for /f "tokens=1,* delims=:" %%a in ('findstr /b /c:"version:" "%FRONTEND%\dist-electron\latest.yml"') do if not defined YML_VER set "YML_VER=%%b"
    for /f "tokens=1,* delims=:" %%a in ('findstr /b /c:"path:" "%FRONTEND%\dist-electron\latest.yml"') do if not defined YML_PATH set "YML_PATH=%%b"
    set "YML_VER=!YML_VER: =!"
    set "YML_PATH=!YML_PATH: =!"
    echo   latest.yml: version=!YML_VER! path=!YML_PATH!
    if not defined REPO_VER (
        echo [warn] cannot read %ROOT%\VERSION, skipping latest.yml version check
    ) else if not "!YML_VER!"=="!REPO_VER!" (
        echo [error] latest.yml version ^(!YML_VER!^) != VERSION ^(!REPO_VER!^): clients will reject or mis-update
        exit /b 1
    )
    if defined YML_PATH if not exist "%FRONTEND%\dist-electron\!YML_PATH!" (
        echo [error] latest.yml points at a missing installer: %FRONTEND%\dist-electron\!YML_PATH!
        exit /b 1
    )
) else (
    echo [skip] zip portable does not emit latest.yml; auto-update needs --nsis
)

REM ---------- Step 4: post-build self-check ----------
echo.
if "%VERIFY%"=="true" (
    if exist "%ROOT%\scripts\client_start_test.py" (
        echo [build] Step 4/4: post-build self-check ^(start client, REST/WS/window^)
        "!MANAGED_PYTHON!" "%ROOT%\scripts\client_start_test.py" --target client
        if !errorlevel! neq 0 (
            echo [warn] self-check did not fully pass ^(see output^). artifacts exist, please review.
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

REM ---------- Step 4.5: MCP end-to-end (packaged) ----------
REM  ★ 为什么要单列：REST 自省能列出 127 个工具，**不等于 Agent 真能连上**。
REM    实测撞到过 ``POST /mcp`` 405（文档三处都写 ``/mcp``，照文档配的客户端全挂），
REM    而自省走的是另一条代码路径，照样返回 127 —— 只有按协议握手才暴露。
REM    build_all.sh 有此步，.bat 原先缺（Windows 侧发布没有覆盖 Agent 接入链路）。
REM
REM  ★★ 「工具只把**改动行**写成 LF」是本文件最阴的坑（2026-09-26 R30 实测）：
REM     cmd 是**逐字节**读 .bat 的，只要文件里出现**混排的行尾**（绝大多数行 CRLF、
REM     刚被某个编辑器保存过的那几行是裸 LF），括号块（`if` / `for`）就会在 LF 处被
REM     撕裂 —— 症状极其隐蔽：**脚本照跑、产物照出、退出码仍是 0**，只在 stderr 多几行
REM     ``'xxx' is not recognized as an internal or external command``
REM     （实测报 ``'），' is not recognized ...`` / ``'）才是执行期取值。同理' is not ...``
REM     —— 正是被截断那几行的后半段）。肉眼完全看不出来，`git diff` 也不显眼。
REM     规则：**每次编辑本文件后，必须复核行尾**，并且块内注释只写纯 ASCII。
REM     复核方式（一行式命令与门禁说明）见 docs/TECH_DEBT.md::TD-21。
REM     `python scripts/ci_reconcile.py` 已把「.gitattributes 声明的行尾 ↔ 工作区实际
REM     字节」纳入门禁，混排会直接报红。
REM
REM  ★ `where bash` **不足以**判定「有可用的 bash」：Windows 10/11 的
REM    C:\Windows\System32\bash.exe 是 **WSL 启动器**，未安装 WSL 发行版时它照样在
REM    PATH 上、照样被 `where` 命中，但一执行就打印「适用于 Linux 的 Windows 子系统
REM    没有已安装的分发版」并返回非 0。历史缺陷：本步只 `where bash` 就认定有 bash，
REM    在这类机器上定位到 WSL 桩 ⇒ MCP 端到端**假报失败**（产物其实完全正常）⇒
REM    整个构建 exit 1。修法：先按已知安装位置探真 Git Bash，再对 `where` 的结果
REM    用 `--version` 实测（必须回 `GNU bash`）过滤掉 WSL 桩；一个都没有才 skip。
if "%VERIFY%"=="true" (
    REM probe the known Git Bash install locations first -- ASCII only inside a block
    set "BASH_EXE="
    for %%b in (
        "%ProgramFiles%\Git\bin\bash.exe"
        "%ProgramFiles(x86)%\Git\bin\bash.exe"
        "%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
    ) do if not defined BASH_EXE if exist "%%~b" set "BASH_EXE=%%~b"
    if not defined BASH_EXE (
        for /f "delims=" %%b in ('where bash 2^>nul') do (
            if not defined BASH_EXE (
                "%%~b" --version 2>nul | findstr /i /c:"GNU bash" >nul
                if !errorlevel! equ 0 set "BASH_EXE=%%~b"
            )
        )
    )
    if not defined BASH_EXE (
        echo [skip] no usable bash ^(Git Bash^) found: skipping MCP end-to-end
        echo        System32\bash.exe is the WSL launcher and cannot run bash scripts.
        echo        Install Git for Windows to enable this gate.
    ) else (
        if not exist "%ROOT%\scripts\verify_packaged_mcp_boot.sh" (
            echo [skip] scripts/verify_packaged_mcp_boot.sh not found, skipping MCP end-to-end
        ) else (
            if not exist "%BACKEND%\dist\qmt_work\qmt_work.exe" (
                echo [skip] packaged backend EXE not found, skipping MCP end-to-end
            ) else (
                set "MCP_SH=%ROOT:\=/%"
                echo [build] Step 4.5: MCP end-to-end ^(packaged backend, real handshake^)
                REM must use !BASH_EXE! not %BASH_EXE%: inside a block %VAR% is expanded at
                REM BLOCK-PARSE time -- BASH_EXE is not assigned yet, so it becomes "" and
                REM cmd reports '""' is not recognized as an internal or external command.
                REM Delayed expansion !VAR! is the execution-time value. Same for MCP_SH.
                "!BASH_EXE!" "!MCP_SH!/scripts/verify_packaged_mcp_boot.sh" 21189
                if !errorlevel! neq 0 (
                    echo [warn] MCP end-to-end failed. Agent may not connect; please review.
                    set VERIFY_RC=1
                ) else (
                    echo [build] MCP end-to-end passed: agents can connect
                )
            )
        )
    )
)

echo.
echo =========================================
echo  build complete
echo  backend EXE : %BACKEND%\dist\qmt_work\qmt_work.exe
echo  desktop     : %FRONTEND%\dist-electron\win-unpacked\qmt_work.exe
echo  outputs     : %FRONTEND%\dist-electron\
echo  update src  : frontend-next\electron-builder.yml (publish)
echo =========================================
exit /b %VERIFY_RC%
