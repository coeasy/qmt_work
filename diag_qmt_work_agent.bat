@echo off
setlocal enabledelayedexpansion
title qmt_work 大 QMT 一键诊断
chcp 65001 >nul

echo ============================================================
echo  qmt_work 大 QMT 一键诊断
echo  一次跑完：注册树 / 心跳 / bundle / agent_config
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
    pause
    exit /b 1
)
echo [i] Python  : %PYTHON%

REM ---------------- 2. 找 QMT 目录 ----------------
set "QMT_DIR="
if exist "P:\stock\gd_qmt\python"              set "QMT_DIR=P:\stock\gd_qmt" & goto found_qmt
if exist "D:\QMT\python"                       set "QMT_DIR=D:\QMT" & goto found_qmt
if exist "C:\QMT\python"                       set "QMT_DIR=C:\QMT" & goto found_qmt
if exist "D:\国投证券QMT交易端\python"          set "QMT_DIR=D:\国投证券QMT交易端" & goto found_qmt
if exist "C:\国投证券QMT交易端\python"          set "QMT_DIR=C:\国投证券QMT交易端" & goto found_qmt

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
if not exist "%QMT_DIR%" (
    echo [FAIL] %QMT_DIR% 不存在。
    pause
    exit /b 1
)
echo [i] QMT 目录: %QMT_DIR%
echo.

REM ---------------- 3. QMT 运行状态提示 ----------------
tasklist /FI "IMAGENAME eq XtItClient.exe" 2>nul | find /I "XtItClient.exe" >nul
if not errorlevel 1 (
    echo [!] QMT 正在运行 —— 步骤 [3/3] 可能读不到配置文件（QMT 独占锁定）。
    echo     如需完整 inspect，请关闭 QMT 后再跑一次。
    echo.
)

REM ---------------- [1/3] 策略注册树 ----------------
echo ============================================================
echo  [1/3] 策略注册树（qmt_strategy_list_probe）
echo ============================================================
"%PYTHON%" "%~dp0scripts\qmt_strategy_list_probe.py" --target qmt_work_agent --qmt-dir "%QMT_DIR%"
echo.

REM ---------------- [2/3] 心跳新鲜度 + 能力清单 ----------------
echo ============================================================
echo  [2/3] 心跳新鲜度 + 能力清单（qmt_agent_verify）
echo ============================================================
"%PYTHON%" "%~dp0scripts\qmt_agent_verify.py"
echo.

REM ---------------- [3/3] bundle + agent_config ----------------
echo ============================================================
echo  [3/3] bundle + agent_config.json（qmt_agent_deploy inspect）
echo ============================================================
"%PYTHON%" "%~dp0scripts\qmt_agent_deploy.py" inspect --force --qmt-dir "%QMT_DIR%"
echo.

REM ---------------- 判读要点 ----------------
echo ============================================================
echo  判读要点（对照上面输出）
echo ============================================================
echo.
echo  状态                    含义
echo  --------------------    ------------------------------------------------
echo  已注册: 是 + 心跳新鲜   桥可用，直接接前端
echo  已注册: 否              QMT GUI 未导入策略 → 双击 deploy_qmt_work_agent.bat
echo  已注册: 是 + 心跳过期   策略未启动 → 去 QMT「模型交易」手工启动，或勾「自动运行」
echo  自动运行: 是            QMT 每次启动会自动拉起（零人工）
echo  trading_enabled: false  只连不上交易接口 → 编辑 agent_config.json 改 true
echo  probe_stale: true       上面结论只是「陈旧快照里未见」，不代表当前缺失
echo  PermissionError         QMT 独占锁配置 → 关 QMT 再跑（或跳过 3/3）
echo.
echo  下一步（如需修）：
echo    • 部署：  deploy_qmt_work_agent.bat
echo    • 修配置：%QMT_DIR%\python\agent_config.json
echo ============================================================
echo.
pause
