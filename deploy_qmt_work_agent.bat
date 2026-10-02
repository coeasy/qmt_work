@echo off
setlocal enabledelayedexpansion
title qmt_work 大 QMT 一键部署
chcp 65001 >nul

echo ============================================================
echo  qmt_work 大 QMT 一键部署
echo  路径: 生成 bundle → 打开资源管理器 → 打印下一步 3 步操作
echo ============================================================
echo.

REM ---------------- 1. 找 Python ----------------
set "PYTHON="
if exist "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
    set "PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) else if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.11.9\python.exe" (
    set "PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.11.9\python.exe"
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where python') do (
            if not defined PYTHON set "PYTHON=%%i"
        )
    )
)
if not defined PYTHON (
    echo [FAIL] 找不到 Python 解释器。
    echo        请安装 Python 3.8+ 或设置 PATH。
    pause
    exit /b 1
)
echo [i] Python  : %PYTHON%

REM ---------------- 2. 找 QMT 目录 ----------------
set "QMT_DIR="
REM 常见路径优先探测；第一个存在 python/ 子目录的即用
if exist "P:\stock\gd_qmt\python"              set "QMT_DIR=P:\stock\gd_qmt" & goto found_qmt
if exist "D:\QMT\python"                       set "QMT_DIR=D:\QMT" & goto found_qmt
if exist "C:\QMT\python"                       set "QMT_DIR=C:\QMT" & goto found_qmt
if exist "D:\国投证券QMT交易端\python"          set "QMT_DIR=D:\国投证券QMT交易端" & goto found_qmt
if exist "C:\国投证券QMT交易端\python"          set "QMT_DIR=C:\国投证券QMT交易端" & goto found_qmt

echo.
echo [!] 未自动找到 QMT 安装目录。
echo     请手动指定（把 QMT 安装路径用双引号包起来传进来）：
echo         deploy_qmt_work_agent.bat "D:\你的QMT路径"
echo.
if not "%~1"=="" (
    set "QMT_DIR=%~1"
    goto found_qmt
)
set /p QMT_DIR="QMT 目录: "
if not defined QMT_DIR (
    echo [FAIL] 未指定 QMT 目录。
    pause
    exit /b 1
)
:found_qmt
if not exist "%QMT_DIR%\python" (
    echo [FAIL] %QMT_DIR%\python 不存在。请检查路径。
    pause
    exit /b 1
)
echo [i] QMT 目录: %QMT_DIR%
echo.

REM ---------------- 3. 检查 QMT 是否在运行 ----------------
tasklist /FI "IMAGENAME eq XtItClient.exe" 2>nul | find /I "XtItClient.exe" >nul
if not errorlevel 1 (
    echo [!] 检测到 QMT 正在运行 —— 不影响本次部署（只写文件），但注册树读取会失败。
    echo     建议：部署完后先关 QMT、重启，再做导入本地策略。
    echo.
)

REM ---------------- 4. 部署 bundle ----------------
echo [1/2] 生成 bundle 并部署到 %QMT_DIR%\python\...
"%PYTHON%" "%~dp0scripts\qmt_agent_deploy.py" deploy --txt --qmt-dir "%QMT_DIR%"
if errorlevel 1 (
    echo.
    echo [FAIL] 部署失败。上面应能看到具体原因。
    pause
    exit /b 1
)

REM ---------------- 5. agent_config.json ----------------
set "CFG=%QMT_DIR%\python\agent_config.json"
if not exist "%CFG%" (
    echo [2/2] 生成 agent_config.json 模板 → %CFG%
    copy "%~dp0backend\agent_bigqmt\agent_config.example.json" "%CFG%" >nul
    echo       [!] 请打开该文件按注释填真实路径与 token 后再用 QMT 启动策略。
) else (
    echo [2/2] agent_config.json 已存在，跳过（保留用户原有配置）
)
echo.

REM ---------------- 6. 打印下一步 ----------------
echo ============================================================
echo  下一步：在 QMT 里手工注册策略（约 30 秒）
echo ============================================================
echo.
echo  路径 A（推荐 · 导入本地策略）
echo    1. 打开 QMT 客户端（若已在跑，先关再开）
echo    2. 「模型研究」→ 策略区 → 右键 → 「导入本地策略」/「本地.rzrk导入」
echo    3. 选文件: %QMT_DIR%\python\qmt_work_agent.py
echo    4. 关闭并重启 QMT
echo.
echo  路径 B（备用 · 新建 + 粘贴，QMT「导入本地策略」被禁时才用）
echo    1. 「我的」→ 新建策略 → Python 策略
echo    2. 全选删除模板，粘贴 %QMT_DIR%\python\qmt_work_agent.txt 全部
echo       （.txt 版本用记事本双击打开，粘贴比 IDE 稳）
echo    3. 点「编译」保存（编译/保存才会登记进注册树）
echo.
echo  注册后验证：
echo    "%PYTHON%" "%~dp0scripts\qmt_strategy_list_probe.py" --target qmt_work_agent --qmt-dir "%QMT_DIR%"
echo    "%PYTHON%" "%~dp0scripts\qmt_agent_verify.py"
echo.
echo  或直接双击 diag_qmt_work_agent.bat 一键诊断。
echo.

REM ---------------- 7. 打开资源管理器选中 bundle ----------------
echo [i] 打开资源管理器并选中 qmt_work_agent.py ...
explorer /select,"%QMT_DIR%\python\qmt_work_agent.py"

echo.
echo [DONE] 部署完成。按任意键退出。
pause >nul
