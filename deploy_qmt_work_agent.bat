@echo off
setlocal enabledelayedexpansion
title QMT agent 通用部署工具
chcp 936 >nul

REM ============================================================
REM  QMT agent 通用部署脚本（通行版）
REM ------------------------------------------------------------
REM  本脚本是通行工具，不写死任何客户端、券商或本机路径。
REM  它会在任意 Windows 机器上自动探测：
REM      1. Python 解释器  env -^> py launcher -^> PATH -^> 常见根 -^> 注册表
REM      2. QMT 安装目录    env -^> 参数 -^> 工具脚本全盘扫描
REM      3. 工具链脚本      仓库布局 / 单独工具目录 / 向上回溯
REM  所以它能随仓库跑、随安装包跑、或解压到任意位置单独跑。
REM
REM  用法
REM  ----
REM    deploy_qmt_work_agent.bat                        全自动探测后部署
REM    deploy_qmt_work_agent.bat "D:\QMT\..."           指定 QMT 目录
REM    deploy_qmt_work_agent.bat "D:\QMT\..." --reveal  部署后打开资源管理器
REM
REM  环境变量覆盖（脚本内优先级最高）
REM    QMT_DIR     QMT 安装根目录（存在 python\ 子目录的那一层）
REM    QMT_PYTHON  Python 解释器绝对路径（须不低于 3.8）
REM    AGENT_FILE  落盘文件名，默认 QMT_WORK_AGENT.py
REM                必须与 QMT 注册树里那条策略指向的文件名一致；
REM                文件名对不上时表现为「列表里有、点了跑不起来」。
REM
REM  注意：cmd 不允许标签写在括号块内，所以参数解析用 shift 循环。
REM ============================================================

set "EXTRA="
set "OPT_QMT="
:arg_loop
if "%~1"=="" goto :arg_done
if not defined OPT_QMT (
    if /i "%~1"=="--reveal" goto :arg_extra
    if /i "%~1"=="--txt" goto :arg_extra
    if /i "%~1"=="--no-run-check" goto :arg_extra
    if /i "%~1"=="--force" goto :arg_extra
    set "OPT_QMT=%~1"
    shift
    goto :arg_loop
)
:arg_extra
set "EXTRA=!EXTRA! %~1"
shift
goto :arg_loop
:arg_done

cd /d "%~dp0"
set "SCRIPT_DIR=%CD%"

echo ============================================================
echo  QMT agent 通用部署
echo ============================================================
echo.

REM ---------------- 0. 找工具链脚本 ----------------
set "TOOL="
if exist "!SCRIPT_DIR!\scripts\qmt_agent_deploy.py" set "TOOL=!SCRIPT_DIR!\scripts\qmt_agent_deploy.py"
if not defined TOOL if exist "!SCRIPT_DIR!\qmt_agent_deploy.py" set "TOOL=!SCRIPT_DIR!\qmt_agent_deploy.py"
if not defined TOOL call :find_tool_up "!SCRIPT_DIR!"
REM 兜底（独立于 %0）：某些宿主（Git Bash 的 `cmd //c`、个别 IDE/CI 终端）会
REM 让 %~dp0 指向别处（实测：带 --flag 时 %~dp0 变成首个路径参数的目录）。
REM 从**当前目录**再找一遍 —— 用户通常就在仓库根目录执行本脚本。
if not defined TOOL if exist "%CD%\scripts\qmt_agent_deploy.py" set "TOOL=%CD%\scripts\qmt_agent_deploy.py"
if not defined TOOL if exist "%CD%\qmt_agent_deploy.py" set "TOOL=%CD%\qmt_agent_deploy.py"
if not defined TOOL call :find_tool_up "%CD%"
if not defined TOOL goto :fail_tool
echo [i] 工具链  : !TOOL!
echo.

REM 本脚本已把控制台切到 936(GBK)，Python 必须按同码页输出，否则中文乱码。
REM 无条件覆盖（父进程可能带 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8，会让中文变乱码）；
REM 且在本脚本 setlocal 作用域内设置，只影响下面的子进程，不改用户全局环境。
set "PYTHONUTF8="
set "PYTHONIOENCODING=gbk"

REM ---------------- 1. 找 Python ----------------
set "PYTHON="
if defined QMT_PYTHON if exist "%QMT_PYTHON%" set "PYTHON=%QMT_PYTHON%"
if not defined PYTHON call :find_python
if not defined PYTHON goto :fail_python

REM 版本门禁：低于 3.8 的 CPython 语法与标准库不满足工具链要求
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 goto :fail_python_ver
echo [i] Python  : %PYTHON%
echo.

REM ---------------- 2. 定位 QMT 安装目录 ----------------
REM 优先 env / 参数；都没有则交给 python 工具脚本做全盘扫描。
REM 扫描判据是通用的（python\ 策略目录 + 任一安装标记），
REM 能识别所有券商版本的 QMT，不写死盘位或券商名。
set "QMT_DIR2="
if defined QMT_DIR set "QMT_DIR2=%QMT_DIR%"
if not defined QMT_DIR2 if defined OPT_QMT set "QMT_DIR2=%OPT_QMT%"

if not defined QMT_DIR2 (
    set "DISC="
    "%PYTHON%" "!TOOL!" discover >"%TEMP%\_qmt_disc.txt" 2>nul
    if not errorlevel 1 (
        for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%TEMP%\_qmt_disc.txt") do (
            if "%%a"=="QMT_DIR" set "DISC=%%b"
        )
    )
    if defined DISC if not "!DISC!"=="(none)" set "QMT_DIR2=!DISC!"
)

if not defined QMT_DIR2 goto :ask_qmt
if "!QMT_DIR2:~-1!"=="\" set "QMT_DIR2=!QMT_DIR2:~0,-1!"
if not exist "!QMT_DIR2!\python" goto :bad_qmt
echo [i] QMT 目录: !QMT_DIR2!
goto :run_deploy

REM ============================================================
REM  部署主流程（自动探测 / 参数 / 手工输入三条路径共用）
REM ============================================================
:run_deploy
tasklist /NH 2>nul | findstr /I "XtItClient.exe" >nul
if not errorlevel 1 (
    echo [提示] 检测到 QMT 客户端正在运行 —— 不影响本次部署（只写文件），
    echo     但注册树读取会失败。建议部署后重启 QMT 再做导入。
    echo.
)

REM 落盘文件名：默认 QMT_WORK_AGENT.py；可用环境变量 AGENT_FILE 覆盖。
REM 必须与 QMT 注册树里那条策略指向的文件名一致，否则「列表里有、点了跑不起来」。
if not defined AGENT_FILE set "AGENT_FILE=QMT_WORK_AGENT.py"
set "AGENT_NAME=QMT_WORK_AGENT"

echo [1/2] 生成 bundle 并部署到 !QMT_DIR2!\python\...
"%PYTHON%" "!TOOL!" deploy --txt --qmt-dir "!QMT_DIR2!" --filename "!AGENT_FILE!" --strategy "!AGENT_NAME!"!EXTRA!
if errorlevel 1 goto :fail_deploy

set "CFG=!QMT_DIR2!\python\agent_config.json"
set "TPL="
if not exist "%CFG%" call :find_template
if not exist "%CFG%" if defined TPL goto :write_tpl
if not exist "%CFG%" (
    echo [2/2] 未找到配置模板，已跳过（可在 QMT 客户端「大 QMT 部署」页手工写入）。
    goto :done_msg
)
echo [2/2] agent_config.json 已存在，跳过（保留用户原有配置）
goto :done_msg

:write_tpl
echo [2/2] 生成配置模板 -^> %CFG%
copy "%TPL%" "%CFG%" >nul
echo       [提示] 请打开该文件按注释填真实路径与 token 后再用 QMT 启动策略。
goto :done_msg

:done_msg
echo.
echo ============================================================
echo  下一步：在 QMT 里手工注册策略（约 30 秒）
echo ============================================================
echo.
echo  路径 A（推荐 - 导入本地策略）
echo    1. 打开 QMT 客户端（若已在跑，先关再开）
echo    2. 「模型研究」-^> 策略区 -^> 右键 -^> 「导入本地策略」/「本地.rzrk导入」
echo    3. 选文件: !QMT_DIR2!\python\!AGENT_FILE!
echo    4. 关闭并重启 QMT
echo.
echo  路径 B（备用 - 新建 + 粘贴，QMT「导入本地策略」被禁时才用）
echo    1. 「我的」-^> 新建策略 -^> Python 策略
echo    2. 全选删除模板，粘贴 !QMT_DIR2!\python\!AGENT_FILE:~0,-3!.txt 全部内容
echo       （.txt 版本用记事本双击打开，粘贴比 IDE 稳）
echo    3. 点「编译」保存（编译/保存才会登记进注册树）
echo.
echo  注册后验证:
echo    "%PYTHON%" "!TOOL!" check --qmt-dir "!QMT_DIR2!"
echo.
echo [i] 打开资源管理器并选中 !AGENT_FILE! ...
explorer /select,"!QMT_DIR2!\python\!AGENT_FILE!"
echo.
echo [DONE] 部署完成。按任意键退出。
pause >nul
exit /b 0

REM ============================================================
REM  错误分支
REM ============================================================
:fail_tool
echo [FAIL] 找不到 qmt_agent_deploy.py。
echo        请确认脚本与 tools 目录结构未被破坏。
pause
exit /b 1

:fail_python
echo [FAIL] 找不到 Python 解释器。
echo        请安装 Python 3.8 以上版本，或用环境变量显式指定：
echo            set QMT_PYTHON="C:\Python311\python.exe"
echo        再重新运行本脚本。
pause
exit /b 1

:fail_python_ver
echo [FAIL] 找到的 Python 版本过低，需要 3.8 以上。
echo        当前: %PYTHON%
"%PYTHON%" --version 2>&1
echo        请安装更高版本 Python 或用 QMT_PYTHON 指定。
pause
exit /b 1

:ask_qmt
echo.
echo [提示] 未自动找到 QMT 安装目录（已扫描所有固定盘）。
echo     请手动指定，两种方式任选其一：
echo         deploy_qmt_work_agent.bat "D:\你的QMT路径"
echo     或
echo         set QMT_DIR="D:\你的QMT路径"
echo.
set /p QMT_DIR2="QMT 目录（例如 D:\QMT）: "
if not defined QMT_DIR2 goto :bad_qmt
if "!QMT_DIR2:~-1!"=="\" set "QMT_DIR2=!QMT_DIR2:~0,-1!"
if not exist "!QMT_DIR2!\python" goto :bad_qmt
echo [i] QMT 目录: !QMT_DIR2!
goto :run_deploy

:bad_qmt
echo [FAIL] QMT 目录无效：!QMT_DIR2!\python 不存在。
echo        请确认给的是 QMT 安装根目录（其下应含 python\、bin.x64\ 或 userdata\）。
pause
exit /b 1

:fail_deploy
echo.
echo [FAIL] 部署失败。上面应能看到具体原因。
pause
exit /b 1

REM ============================================================
REM  子程序：找 Python 解释器
REM  顺序：py launcher -^> PATH -^> 常见安装根 -^> 注册表
REM  结果写入 !PYTHON!
REM ============================================================
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
if not defined PYTHON if exist "%ProgramFiles(x86)%\Python311\python.exe" set "PYTHON=%ProgramFiles(x86%)\Python311\python.exe"
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

REM ============================================================
REM  子程序：从给定目录向上回溯，找 scripts\qmt_agent_deploy.py
REM  参数：%~1 = 起始目录；结果写入 !TOOL!
REM ============================================================
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

REM ============================================================
REM  子程序：找 agent_config 模板
REM  兼容：仓库布局（backend\agent_bigqmt\）与工具目录布局
REM  结果写入 !TPL!
REM ============================================================
:find_template
if exist "!SCRIPT_DIR!\backend\agent_bigqmt\agent_config.example.json" (
    set "TPL=!SCRIPT_DIR!\backend\agent_bigqmt\agent_config.example.json"
    exit /b 0
)
if exist "!SCRIPT_DIR!\agent_config.example.json" (
    set "TPL=!SCRIPT_DIR!\agent_config.example.json"
    exit /b 0
)
exit /b 0
