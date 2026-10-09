@echo off
REM ============================================================================
REM  qmt_work 大 QMT 一键诊断（通行脚本）
REM ----------------------------------------------------------------------------
REM  设计约束（与 deploy_qmt_work_agent.bat 同一套）：
REM   1. **不写死任何安装路径/券商名** —— QMT 目录由 qmt_agent_deploy.py 的通用
REM      探测得到（逐盘符扫 1~2 层 + `python\` 策略目录 + 任一安装标记）。
REM   2. **不写死 Python 路径** —— 走 QMT_PYTHON → py -3 → where python →
REM      常见安装目录 → 注册表 的发现链。
REM   3. 文件必须 CRLF + GBK(无 BOM) 且 chcp 936 与之一致：
REM      LF-only 的 .bat 会被 cmd 按整行拼接解析，中文行直接报「不是内部或外部命令」。
REM   4. 接受 `%~1` 显式指定 QMT 目录（或策略名），QMT_DIR 环境变量次之。
REM ============================================================================
setlocal enabledelayedexpansion
title qmt_work 大 QMT 一键诊断
chcp 936 >nul

echo ============================================================
echo  qmt_work 大 QMT 一键诊断（通用 · 自动适配任意安装位置）
echo  一次跑完：注册树 / 心跳 / bundle / agent_config
echo ============================================================
echo.

REM ---------------- 0. 定位工具链 ----------------
REM ★ 用 `cd /d "%~dp0"` + `%CD%` 取脚本目录：`set "X=%~dp0"` 的尾随 `\` 会与
REM   闭引号组成 `\"`，把变量静默变成空串（本机实测踩过）。
cd /d "%~dp0"
set "SCRIPT_DIR=%CD%"

set "TOOL="
if exist "!SCRIPT_DIR!\scripts\qmt_agent_deploy.py" set "TOOL=!SCRIPT_DIR!\scripts\qmt_agent_deploy.py"
if not defined TOOL if exist "!SCRIPT_DIR!\qmt_agent_deploy.py" set "TOOL=!SCRIPT_DIR!\qmt_agent_deploy.py"
if not defined TOOL call :find_tool_up "!SCRIPT_DIR!"
REM 兜底（独立于 %0）：某些宿主（Git Bash 的 `cmd //c`、个别 IDE/CI 终端）会
REM 让 %~dp0 指向别处。从**当前目录**再找一遍 —— 用户通常在仓库根目录执行。
if not defined TOOL if exist "%CD%\scripts\qmt_agent_deploy.py" set "TOOL=%CD%\scripts\qmt_agent_deploy.py"
if not defined TOOL if exist "%CD%\qmt_agent_deploy.py" set "TOOL=%CD%\qmt_agent_deploy.py"
if not defined TOOL call :find_tool_up "%CD%"
if not defined TOOL (
    echo [FAIL] 找不到 scripts\qmt_agent_deploy.py —— 工具链缺失。
    echo        请把本脚本放回仓库根目录（或任意能向上回溯到 scripts\ 的位置）。
    pause
    exit /b 1
)
for %%d in ("!TOOL!") do set "TOOL_DIR=%%~dpd"
echo [i] 工具链  : !TOOL!
echo.

REM ---------------- Python 输出编码 ----------------
REM 本脚本已把控制台切到 936(GBK)，Python 必须按同码页输出，否则中文乱码。
REM **无条件覆盖**：父进程（如某些终端/CI）常带 PYTHONIOENCODING=utf-8 或
REM PYTHONUTF8=1，`if not defined` 守不住这种情况。setlocal 作用域内只影响子进程。
set "PYTHONUTF8="
set "PYTHONIOENCODING=gbk"

REM ---------------- 1. 找 Python ----------------
set "PYTHON="
if defined QMT_PYTHON if exist "%QMT_PYTHON%" set "PYTHON=%QMT_PYTHON%"
if not defined PYTHON call :find_python
if not defined PYTHON (
    echo [FAIL] 找不到 Python 解释器。
    echo        请安装 Python 3.8+，或用 QMT_PYTHON 环境变量显式指定：
    echo            set QMT_PYTHON=D:\Python311\python.exe
    pause
    exit /b 1
)
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 (
    echo [FAIL] 找到的 Python 低于 3.8：%PYTHON%
    echo        用 QMT_PYTHON 指向更新的解释器后重跑。
    pause
    exit /b 1
)
echo [i] Python  : %PYTHON%

REM ---------------- 2. 定位 QMT 安装目录 ----------------
REM 优先级：QMT_DIR 环境变量 → 命令行 %~1 → 通用自动探测 → 手工输入。
REM 探测判据是通用的（python\ 策略目录 + 任一安装标记），能识别所有券商版本。
set "QMT_DIR2="
if defined QMT_DIR set "QMT_DIR2=%QMT_DIR%"
if not defined QMT_DIR2 if not "%~1"=="" set "QMT_DIR2=%~1"

if not defined QMT_DIR2 (
    set "DISC="
    "%PYTHON%" "!TOOL!" discover >"%TEMP%\_qmt_disc_diag.txt" 2>nul
    if not errorlevel 1 (
        for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%TEMP%\_qmt_disc_diag.txt") do (
            if "%%a"=="QMT_DIR" set "DISC=%%b"
        )
    )
    if defined DISC if not "!DISC!"=="(none)" set "QMT_DIR2=!DISC!"
)

if defined QMT_DIR2 if "!QMT_DIR2:~-1!"=="\" set "QMT_DIR2=!QMT_DIR2:~0,-1!"
if defined QMT_DIR2 if not exist "!QMT_DIR2!\python" (
    echo [!] !QMT_DIR2! 下没有 python\ 策略目录 —— 可能不是 QMT 安装根。
    set "QMT_DIR2="
)
if not defined QMT_DIR2 (
    echo [i] 自动探测未命中。请手填 QMT 安装根目录（其下应有 python\ 子目录）。
    echo     例： D:\国投证券QMT交易端    或     E:\某券商QMT\客户端
    echo     （不必填到 python\ 这一级；直接给它的上一级目录即可）
    set /p QMT_DIR="QMT 目录: "
    set "QMT_DIR2=!QMT_DIR!"
    if defined QMT_DIR2 if "!QMT_DIR2:~-1!"=="\" set "QMT_DIR2=!QMT_DIR2:~0,-1!"
)
if not defined QMT_DIR2 (
    echo [FAIL] 未指定 QMT 目录。
    pause
    exit /b 1
)
if not exist "!QMT_DIR2!" (
    echo [FAIL] !QMT_DIR2! 不存在。
    pause
    exit /b 1
)
echo [i] QMT 目录: !QMT_DIR2!
echo.

REM ---------------- 3. QMT 运行状态提示 ----------------
REM 用 findstr 而非 find：Git for Windows 的 GNU findutils 可能抢占 PATH 里的
REM `find`，导致 `find /I "..."` 变成「按文件名找文件」而恒失败。
tasklist /NH 2>nul | findstr /I "XtItClient.exe" >nul
if not errorlevel 1 (
    echo [提示] QMT 正在运行 —— 步骤 [3/4] 可能读不到配置文件（QMT 独占锁定）。
    echo     如需完整 inspect，请关闭 QMT 后再跑一次。
    echo.
)

REM ---------------- [1/4] 策略注册树 ----------------
echo ============================================================
echo  [1/4] 策略注册树（qmt_strategy_list_probe）
echo ============================================================
"%PYTHON%" "!TOOL_DIR!qmt_strategy_list_probe.py" --target qmt_work_agent --qmt-dir "!QMT_DIR2!"
echo.

REM ---------------- [2/4] 心跳新鲜度 + 能力清单 ----------------
echo ============================================================
echo  [2/4] 心跳新鲜度 + 能力清单（qmt_agent_verify）
echo ============================================================
"%PYTHON%" "!TOOL_DIR!qmt_agent_verify.py"
echo.

REM ---------------- [3/4] bundle + agent_config ----------------
echo ============================================================
echo  [3/4] bundle + agent_config.json（qmt_agent_deploy inspect）
echo ============================================================
"%PYTHON%" "!TOOL!" inspect --force --qmt-dir "!QMT_DIR2!"
echo.

REM ---------------- [4/4] 结构化 JSON 报告 ----------------
echo ============================================================
echo  [4/4] 生成结构化诊断报告 diag_report.json
echo ============================================================
if not exist "!SCRIPT_DIR!\output" mkdir "!SCRIPT_DIR!\output"
"%PYTHON%" "!TOOL_DIR!qmt_diag_report.py" --qmt-dir "!QMT_DIR2!" --out "!SCRIPT_DIR!\output\diag_report.json"
echo.
echo  报告文件: !SCRIPT_DIR!\output\diag_report.json
echo  用途：排障日志分享 / 开发者定位 / CI 汇总。包含：
echo    timestamp / qmt_dir / probe / verify / inspect / problems[]
echo.

REM ---------------- 判读要点 ----------------
echo ============================================================
echo  判读要点（对照上面输出）
echo ============================================================
echo.
echo  状态                    含义
echo  --------------------    ------------------------------------------------
echo  已注册: 是 + 心跳新鲜   桥可用，直接接前端
echo  已注册: 否              QMT GUI 未导入策略，双击 deploy_qmt_work_agent.bat
echo  已注册: 是 + 心跳过期   策略未启动，去 QMT「模型交易」手工启动或勾「自动运行」
echo  自动运行: 是            QMT 每次启动会自动拉起（零人工）
echo  trading_enabled: false  只连不上交易接口，编辑 agent_config.json 改 true
echo  probe_stale: true       上面结论只是「陈旧快照里未见」，不代表当前缺失
echo  PermissionError         QMT 独占锁配置，关 QMT 再跑（或跳过 [3/4]）
echo.
echo  下一步（如需修）：
echo    * 部署：  deploy_qmt_work_agent.bat
echo    * 修配置：!QMT_DIR2!\python\agent_config.json
echo ============================================================
echo.
pause
exit /b 0

REM ============================================================================
REM  子程序：找 Python 解释器（结果写入 !PYTHON!）
REM  顺序：py launcher → where python → 常见安装目录 → 注册表
REM  ★ 不写死任何具体机器的路径，只有「标准安装位置」这一层是常量。
REM ============================================================================
:find_python
py -3 -c "import sys;print(sys.executable)" >"%TEMP%\_qmt_py.txt" 2>nul
if errorlevel 1 goto :fp_next1
for /f "usebackq delims=" %%i in ("%TEMP%\_qmt_py.txt") do set "PYTHON=%%i"
if defined PYTHON if not exist "!PYTHON!" set "PYTHON="
if defined PYTHON goto :fp_end

:fp_next1
where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('where python') do (
        if not defined PYTHON if exist "%%i" set "PYTHON=%%i"
    )
)
if defined PYTHON goto :fp_end

if not defined PYTHON if exist "%ProgramFiles%\Python311\python.exe" set "PYTHON=%ProgramFiles%\Python311\python.exe"
if not defined PYTHON if exist "%ProgramFiles(x86)%\Python311\python.exe" set "PYTHON=%ProgramFiles(x86)%\Python311\python.exe"
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if not defined PYTHON (
    for /f "delims=" %%i in ('dir /b /ad /o:n "%LOCALAPPDATA%\Programs\Python" 2^>nul') do (
        if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\%%i\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\%%i\python.exe"
    )
)
if defined PYTHON goto :fp_end

for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\Python\PythonCore" /s /v InstallPath 2^>nul') do (
    if not defined PYTHON if exist "%%b\python.exe" set "PYTHON=%%b\python.exe"
)
if defined PYTHON goto :fp_end

for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Python\PythonCore" /s /v InstallPath 2^>nul') do (
    if not defined PYTHON if exist "%%b\python.exe" set "PYTHON=%%b\python.exe"
)

:fp_end
exit /b 0

REM ============================================================================
REM  子程序：从给定目录向上回溯，找 scripts\qmt_agent_deploy.py
REM  参数：%~1 = 起始目录；结果写入 !TOOL!
REM ============================================================================
:find_tool_up
set "P=%~1"
set "DEPTH=0"
:ftu_loop
set /a DEPTH+=1
if "!DEPTH!"=="6" goto :ftu_end
for %%d in ("!P!\..") do set "P=%%~fd"
if "!P!"=="" goto :ftu_end
if "!P!"=="%~1" goto :ftu_end
if exist "!P!\scripts\qmt_agent_deploy.py" set "TOOL=!P!\scripts\qmt_agent_deploy.py"
if defined TOOL goto :ftu_end
goto :ftu_loop
:ftu_end
exit /b 0
