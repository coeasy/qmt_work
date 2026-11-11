#!/usr/bin/env python3
"""CI 数字核对：后端测试收集数 / 前端组件数 / 注册页数量 / API 契约必须自洽。

四项契约（不是通过率旁路）：
  1. EXPECTED_TESTS      —— pytest 逐文件 --collect-only 收集到的用例总数。
  2. EXPECTED_COMPONENTS —— frontend-next/src/{components,shell,charts,domains} 下全部 .tsx。
  3. EXPECTED_PAGES      —— app/routes.tsx 的 PAGES 键数量（= 前端「页」的真源）。
  4. API 契约            —— 前端 services/api 声明的端点必须都存在于后端注册表（P1-5）。

  注：旧 frontend/ 已退役；前端契约全部改指 frontend-next/（见 §12.4 / 退役改造）。

★ 第 4 项为什么放在这里而不是另起一个 hook：它和前三项是**同一类**问题 ——
  「两处对同一事实的声明已经不一致，而没有任何编译期错误会提醒你」。三项核的是
  文档里的数字，第四项核的是前端代码里的路径。分开放只会让「跑门禁」变成
  「记得跑两个门禁」，而漏跑的那个永远不会有人发现。

任一项不符即退出非 0，提示是「改了代码忘了更文档」还是「文档数字过时」。
用法：
    python scripts/ci_reconcile.py            # 校验，不符即失败
    python scripts/ci_reconcile.py --update   # 用实测值回写下方常量

为什么逐文件收集：pytest 在同进程内一次性收集全部 86 个测试文件会在 Windows
上偶发崩溃/导入污染（项目已知问题，见 .workbuddy/memory/MEMORY.md §5）。
因此每文件起独立子进程收集，再汇总。
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend-next")
SRC = os.path.join(FRONTEND, "src")
PAGES_REGISTRY = os.path.join(SRC, "app", "routes.tsx")
README = os.path.join(ROOT, "README.md")
GITATTRIBUTES = os.path.join(ROOT, ".gitattributes")

# ── 计数契约（与 README「核心能力」「项目结构」章节同步）─────────────────────
# ★ 2026-11-09 盘前加固 +35（2910→2945）：§15.7 的 +23（帧半包/ACK 不算行情/
#   ok_basis 三态/同名 label 拒绝/任意入口路径/15 列回读/拒绝静默截断/列序锁/
#   拒绝同名覆盖/邻近监控目录/realpath/批量单盘点）+ §15.8 的 +12：
#   test_xtq_rpc +5（classify_payload：代码表≠行情 / fulltick「代码→字段」不得漏判 /
#   K 线对象表 / ACK 与空结果 / 嵌套 quote marker）；
#   test_qmt_tradehours_validate 新文件 +7（拉取轴也可判 0 / 推送轴判 0 /
#   代码表不顶数 / 订阅 ACK 不算推送 / 58670 缺失归因环境 / 无监听判 1 /
#   netstat 不可用绝不判绿）。test_*.py 218→219。
EXPECTED_TESTS = 3002          # 后端用例收集数（★ 2026-11-11 §29 巡检引擎化 +19：test_qmt_iface_check_engine（新文件：安全守卫在发帧前拦下 passorder/撤单/白名单外方法 / ★ R1 单次缺失=transient_missing 不得称漂移、全 False=stable_absent、只有 flapping 允许说漂移 / 序列只收真正采过该方法的轮次，没采该轴不贡献 None / ★ R2 pid 变化=新授权会话 / R3 三个 mission 轴集互不相同且并集覆盖 ALL_AXES / 探针异常也必须写出 failed_preconditions / 产物名带 mission）；★ 2026-11-11 §24 文件单落地 +12：test_qmt_file_order（+7：列名/列序锁二进制出处 0x3100F8–0x3101C4 / INSERTTIME 格式单点且与二进制 0x310418 无空格写法的分歧必须被记录 / armed=0 时目录齐备也不许 ready / armed=1 且目录存在才 ready（且 honesty 边界仍在）/ ★ readiness 绝不往监控目录写探针文件（armed 时写即下单）/ 目录不存在必须卡在 monitor_dir / 未上报客户端运行态只能写「未检查」）；test_qmt_client_routes（+5：/qmt/file-order/readiness 的 blocking_step、ready、client 未检查、路由层不写监控目录、空 roots 提示）；★ 2026-11-10 §22.R7 早停+三轴 +12：test_qmt_client_log_diagnosis（+9：游标=尝试前文件长度故历史拒绝行不进增量 / 别的 session 的拒绝行不得早停且不限 session 时反向成立 / 无 session 标记的措辞仍算拒绝 / 日志被截断时游标失效重扫 / 无日志不抛异常；refused 才可称「未监听」而 timeout 只能说「不可用」/ 三轴任一为 ok 都不得填平其余两轴 / 行情侧结论只在实测 unusable 时补写且 bound 时不补 / 旧文案「行情（xtdata）不受影响」必须删除但「只裁决交易通道」边界保留）；test_qmt_client_mode（+3：证到 PID 拒绝只试 1 次且不再谎称「session 0..5 均失败」/ 无 PID 证据时必须照旧跑满 12 次组合（反向锚，不得凭猜测早停）/ 三轴并列渲染且互不顶替）；★ 2026-11-10 §21 PID 诊断修复 +7：★ 2026-11-10 §21 PID 诊断修复 +7：test_qmt_client_log_diagnosis（+3：国信真实措辞 `illegal pid 21664` 在 xtquant_auth=1 / 三个 strict=0 的现场必须判 pid_not_allowed 且带 session_retry_useless / `illegal pid 0` 与大小写变体仍命中 / 新旧两种措辞并存时证据不重复且「服务未授权启动」不退化）；test_qmt_client_mode（+2：客户端日志已证 PID 拒绝时报错必须跳过「换 session」与「重登极简模式」、诊断为空时反向保留原四步 FAQ）；test_qmt_tradehours_validate（+2：channel_58610_listening 独立轴如实上报且不改动 58600/58670 裁决 / 58610 单独在监听绝不能把未证实的一轮判绿）；★ 2026-11-10 下单/撤单冒烟 +7：test_qmt_order_smoke（新文件）——exit_code=0 不得被 `or 1` 吞成「环境不满足」/ paper 已成交时撤单必须判 SKIP 而非 FAIL 且 meaning 明写「真实撤单未验证」/ live 四道前置缺项逐条列出 / 前置不全时连 precheck 都不跑（绝不发单）/ paper 立即成交 → cancel_applicable=False 且 exit 0 / health 不通 → exit 1；★ 2026-11-07 新规合规 +16：+14 test_bigqmt_sandbox_compliance（新文件：沙箱替身跑闭环全生命周期零 IO/零外联 / bridge 反向对照必须撞墙 / 闭环独立进程不导入 xtdata / G4 静态闸门可证伪 / G4 不得误伤变量式 __import__）；+2 test_qmt_agent_routes（bundle 与 deploy 的 compliance 画像必须随产物返回，部署成功≠产物可用）；+1 test_qmt_agent_routes deployed 磁盘真名大小写；+7 test_paper_mode_queries：paper 账本读回且零券商触碰/契约形状 PAPER- 前缀/symbol 过滤/account/status 来源标注/引擎缺失 503/dry_run 模式归因/live 券商路径不变；+5 test_sync_cold_hot；+3 bigqmt 桥 root_cause/can_submit；+5 qmt_agent_verify 部署漂移/注册大小写；+1 STOP 消费/alive 诚实；+3 agent_ver 版本单源/client_version 优先桥壳版；+1 trade/order 幂等 key 纳入 mode/conn；+3 forwarded_funcs list 兼容/root_cause 单字段降级/can_submit 一致性；★ 2026-10-14「真实下单 513090 取证」+44：+15 test_autoconnect（+11 桥发现准入/拒绝口径与「路径 A 建连但未连通必须兜底走桥」；+3 自动连接守护不得用「路径 A 有没有候选」当闸门 + 尊重关闭开关与手动断开；+1 一轮自动连接只全盘探测一次候选 A/B 共用）、+7 test_bigqmt_deploy_scripts（两种运行形态可从日志判出 / cp1252 mojibake 还原 / 无日志证据只能标「推断」/ 旁路日志排除 / check 必须给修法 / ★ check 在独立进程形态下退出码必须非 0，健康形态必须 0）、+10 test_broker_capability_health（新文件：能力缺口必须降级 needs_action / 缺口消失恢复 / 无该方法适配器行为不变 / 诊断抛异常不炸巡检 / capability_gap 零 IO）、+4 test_sync_state（冷热分区流 zone→流名 映射与接线 / zone 缺省推断 / 旧库回退读遗留 sync.bars）、+1 test_sync_bars_full（sync_many 缺省计划的 zone→cold 语义）、+7 test_paper_mode_queries（paper 分流后查询归因与零券商触碰）；★ 2026-10-15 二轮「自动连接时序 + 误导归因」+15：+4 test_autoconnect 时序回归（守护首轮先跑再睡 / 空闲轮不涨退避 / 路径 A add_connection 挂起不饿死路径 B / 路径 A activate 挂起不饿死路径 B）、+3 test_autoconnect 守护诚实性（conn 实际断开不得报 connected / 真连上才报 connected / 连续失败必须退避且退避有上限）、+2 test_paper_mode_queries（live 能力门禁可执行归因且门禁先于执行 orders==[] / connector_probe 缺失时门禁放行不误伤）、+1 test_trade_chain_contract（broker_unavailable reason 原样透传、无「未连接任何券商客户端」假前缀）、+5 test_xtp_connect_leak（新文件：xtp 适配器连接泄漏）；★ 2026-10-15 全量 flaky 修复 +1 test_autoconnect（守护装配受开关控制：开⇒装配 / 关⇒不装配，正反两面）；★ 2026-10-15 打包产物复测 +1 test_autoconnect（守护装配器同步契约 + setup() 接线 AST 双锁：不 await 也必须真建 task；40b53a8 曾 async 化 + 调用点漏 await ⇒ 协程永不执行 ⇒ 守护从未创建）；★ 2026-10-15 源码编码复测 +1 test_bundle_hardening（GBK+CRLF 放行 / GBK+LF 判红 —— 行尾是「GBK 安全」的另一半：LF 行尾会让内置 py3.6 丢开头两行、docstring 失效，报错却指向一行中文）；★ 2026-11-04 P0-1/P0-2/P0-3 回归 +23：test_wal_intent_unknown(9)/test_reconcile_intent(5)/test_broker_token_crypto(5)/test_broker_connectors_route(4)；★ 2026-11-07 P0-3 前端闭环入口 +4：test_broker_connector_ui_coverage（registry 可用 connector 必须在前端有入口 / 退役项不得进快速模板 / 闭环不得要求 bridge_dir / 前端入口 key 必须存在于 registry）；★ 2026-11-07 D-12 状态码分层 +6：test_envelope_middleware（503 提升为真 HTTP 503 / body 一字不改 / content-length 一致 / 400-404 保持 200 / 显式状态码不被覆盖 / 非 /api/v1 路径不提升 / _promote_status 三重闸门）；★ 2026-11-08 多 QMT 文件单（DBF）+38：test_qmt_file_order（新文件：15 列列序锁死 / 头两长度字段按公式 / 字段名必须 0x00 补齐且读侧容忍空格补齐 / C 型浮点不得取整（mode_price 10.5→"10" 是价格错的单）/ 头自相矛盾必须报错 / 监控目录从 Config.xml 读且必须容忍非合法 XML / monitor 与 backup 平级不得前缀误判）
EXPECTED_COMPONENTS = 76     # 前端 .tsx 组件数（components + shell + charts + domains）
EXPECTED_PAGES = 46          # 注册页数量（routes.tsx PAGES 键；含占位）

# ── 收集失败时允许跳过的辅助模块（非测试）────────────────────────────────────
_SKIP_FILES = {"smoke2.py", "fake_bridge_server.py", "_phase4_support.py",
               "_phase7_support.py", "_verify_phase34.py", "_verify_phase4.py"}


def _pytest_available() -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", "import pytest, sys; print(pytest.__version__)"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        sys.stderr.write(
            "✗ 当前解释器无 pytest，无法核对测试收集数。\n"
            f"  解释器: {sys.executable}\n"
            "  请使用项目托管 venv 运行本脚本，例如：\n"
            "    python backend/pyproject.toml 约束 Python >=3.11,<3.12\n"
            "    pip install -r backend/requirements.txt && pip install pytest\n"
        )
        return False
    return True


def collect_backend_tests() -> int:
    """逐文件 collect-only 汇总。每个文件独立子进程，规避同进程收集崩溃。"""
    files = sorted(
        f for f in glob.glob(os.path.join(BACKEND, "tests", "test_*.py"))
        if os.path.basename(f) not in _SKIP_FILES
    )
    if not files:
        raise RuntimeError(f"未找到任何测试文件：{BACKEND}/tests/test_*.py")

    total = 0
    failed = []
    for f in files:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", f, "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=BACKEND, capture_output=True, text=True, check=False,
        )
        combined = (out.stdout or "") + "\n" + (out.stderr or "")
        # 优先解析汇总行「N tests? collected」（含参数化展开的真实计数），
        # 回退到逐行 '::' 计数。
        m = re.search(r"(\d+) tests? collected", combined)
        if m:
            total += int(m.group(1))
            continue
        if out.returncode == 0:
            total += sum(1 for line in out.stdout.splitlines() if "::" in line)
            continue
        failed.append(os.path.basename(f))
        tail = "\n".join(combined.strip().splitlines()[-6:])
        sys.stderr.write(f"  ✗ collect 失败 {os.path.basename(f)}:\n{tail}\n")
        if any(k in combined for k in ("ModuleNotFoundError", "ImportError")):
            sys.stderr.write(
                "    → 依赖未装：请使用后端托管 venv 运行，先 "
                "`pip install -r backend/requirements.txt`。\n"
            )

    if failed:
        raise RuntimeError(f"{len(failed)}/{len(files)} 个测试文件 collect 失败：{failed}")
    return total


def count_frontend_components() -> int:
    """src/{components,shell,charts,domains} 下全部 .tsx（递归，排除 tests）。

    frontend-next 无 features/ 目录：可复用 UI 在 components/shell/charts，
    页面级（域）组件在 domains/。glob 天然排除 *.tsx.dead 等已移出构建路径的文件。
    注意：Python 的 glob 不支持 bash 式的 {a,b} 花括号展开，须逐目录列。
    """
    n = 0
    for d in ("components", "shell", "charts", "domains"):
        n += len(glob.glob(os.path.join(FRONTEND, "src", d, "**", "*.tsx"),
                           recursive=True))
    return n


def count_pages() -> int:
    """pagesRegistry.jsx 的 PAGES 键数量 —— 前端「页」的唯一真源。"""
    if not os.path.isfile(PAGES_REGISTRY):
        raise RuntimeError(f"缺少页面注册表：{PAGES_REGISTRY}")
    src = open(PAGES_REGISTRY, encoding="utf-8").read()
    # 兼容两种声明：旧 `PAGES = {` 与 frontend-next 的 `PAGES: Record<...> = {`
    m = re.search(r"PAGES\b[^\{]*=\s*\{", src)
    if not m:
        raise RuntimeError(f"{os.path.basename(PAGES_REGISTRY)} 中未找到 PAGES = {{")
    # 从 PAGES = { 起做括号配对，取到对象体
    depth = 0
    start = src.index("{", m.end() - 1)
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[start + 1:i]
                break
    else:
        raise RuntimeError("PAGES 对象括号不配对")
    # 只统计顶层「key: {" 形式的条目：缩进为对象首层
    return len(re.findall(r"(?m)^\s{2,6}([a-z][a-z0-9_]*)\s*:\s*\{", body))


def check_readme_numbers(tests: int, pages: int) -> list[str]:
    """README 里的**计数声明**必须与实测一致 —— README 漂移门禁。

    ★ 为什么必须自动核：README 曾长期声称「37 个页面入口（36 已实现，1 占位：分仓再平衡）」，
    而实际早已是 38 个且全部 ``status: "done"``；测试文件数与用例数同样停在很早以前。
    根因是门禁只核 ``EXPECTED_*`` 常量，**没人核 README 本身** —— 于是文档可以无限期地
    说错话。文档说谎比没有文档更糟：读者会照着它做判断。

    只在 README 里**确实写了**该数字时才比对（缺了不算错，避免门禁变成格式检查）。
    """
    if not os.path.isfile(README):
        return [f"缺少 README：{README}"]
    text = open(README, encoding="utf-8").read()
    problems: list[str] = []

    m = re.search(r"(\d+)\s*个页面", text)
    if m and int(m.group(1)) != pages:
        problems.append(f"README 称「{m.group(1)} 个页面」，实测 {pages} 个")

    m = re.search(r"(\d+)\s*个\s*`test_\*\.py`\s*[，,]\s*(\d+)\s*个用例", text)
    if m:
        n_files = len(glob.glob(os.path.join(BACKEND, "tests", "test_*.py")))
        if int(m.group(1)) != n_files:
            problems.append(f"README 称「{m.group(1)} 个 test_*.py」，实测 {n_files} 个")
        # pytest 不可用时 tests 为 0，跳过（否则会误报）
        if tests and int(m.group(2)) != tests:
            problems.append(f"README 称「{m.group(2)} 个用例」，实测收集 {tests} 个")

    # 路由模块数：README 长期写「36 个路由模块」而实测 34 —— 与页面数同源的漂移。
    # 判据用「文件里真有 @router. 装饰器」，_common.py 之类辅助模块不该被算进去。
    m = re.search(r"(\d+)\s*个\s*(?:REST\s*)?路由模块", text)
    if m:
        route_dir = os.path.join(BACKEND, "app", "routes")
        n_mod = len([f for f in glob.glob(os.path.join(route_dir, "*.py"))
                     if not f.endswith("__init__.py")
                     and "@router." in open(f, encoding="utf-8", errors="ignore").read()])
        if int(m.group(1)) != n_mod:
            problems.append(f"README 称「{m.group(1)} 个路由模块」，实测 {n_mod} 个")
    return problems


def check_api_contract_drift() -> list[str]:
    """第 4 项契约（P1-5）：前端调用的每个端点都必须存在于后端注册表。

    判据逻辑**不在这里复制**，而是复用 ``scripts/check_api_contract_drift.py`` ——
    复制一份就会出现「两份判据各自漂移」，那正是本门禁要防的病。这里只负责
    把它的结果翻译成 ci_reconcile 的问题清单。

    解析不到任何东西时（正则与代码形态脱节 / 契约文件缺失）返回一条失败，
    而不是静默通过 —— 一个「永远绿」的检查比没有检查更糟。
    """
    script = os.path.join(ROOT, "scripts", "check_api_contract_drift.py")
    if not os.path.isfile(script):
        return [f"缺少契约漂移检查脚本：{script}"]
    # 同目录模块：用 spec 加载，避免依赖运行时的 sys.path[0] 恰好是 scripts/
    import importlib.util
    spec = importlib.util.spec_from_file_location("_api_contract_drift", script)
    if spec is None or spec.loader is None:
        return [f"无法加载契约漂移检查脚本：{script}"]
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not os.path.isfile(mod.CONTRACT):
        return [f"缺少后端契约文件：{mod.CONTRACT}（先跑 tests/contracts/introspect.py）"]
    if not os.path.isdir(mod.API_DIR):
        return [f"缺少前端 API 目录：{mod.API_DIR}"]

    backend = mod.load_backend_endpoints()
    backend_norm = mod.contract_normalized(backend)
    frontend, _unresolved, _fe_query = mod.scan_frontend()

    if len(backend) < 50:
        return [f"后端契约只解析到 {len(backend)} 个端点 —— 检查失效（疑契约文件被清空）"]
    if len(frontend) < 20:
        return [f"前端只解析到 {len(frontend)} 个端点 —— 正则已与代码形态脱节"]

    missing = sorted(k for k in frontend if k not in backend_norm)
    # 参数级对账（R38）：前端查询参数名必须被后端签名认识（FastAPI 静默忽略未知参数
    # ⇒ 200 + 默认值 = 看似正常的错误数据）。be_query 解析不到的端点不误报。
    be_query = mod.backend_query_params()
    for key, names in sorted(_fe_query.items()):
        known = be_query.get(key)
        if known is None:
            continue
        unknown = names - known
        if unknown:
            missing.append(f"{key} 查询参数漂移: {', '.join(sorted(unknown))}"
                           f"（后端可认: {', '.join(sorted(known))}）")
    return [f"前端调用了后端不存在的端点：{k}（{', '.join(frontend[k])}）"
            for k in missing]


def check_line_endings() -> list[str]:
    """`.gitattributes` 声明的行尾 ↔ 工作区实际字节 —— 混排即失败。

    ★ 为什么必须自动核（2026-09-26 R30 实测）：`build_all.bat` 是 `.bat`，cmd.exe
      **逐字节**读它。只要文件里出现**混排行尾**（绝大多数行 CRLF、刚被某个编辑器
      保存过的那几行是裸 LF），括号块（`if` / `for`）就会在 LF 处被撕裂。症状极其
      隐蔽：**脚本照跑、产物照出、退出码仍是 0**，只在 stderr 多几行
      ``'xxx' is not recognized as an internal or external command``
      —— 那正是被截断那几行的后半段，肉眼与 `git diff` 都看不出来。

      而「工具只把**改动行**写成 LF」是默认行为（本项目实测踩到）。`.gitattributes`
      早已声明谁必须 CRLF、谁必须 LF，但**从没有人核过工作区是否遵约** ⇒
      同一个坑踩了三次（TD-16 入库 LF / TD-17 检出未生效 / TD-21 编辑引入混排）。
      这里把它变成门禁：声明即契约，违约就报红。
    """
    if not os.path.isfile(GITATTRIBUTES):
        return [f"缺少行尾契约文件：{GITATTRIBUTES}"]
    rules = []
    for line in open(GITATTRIBUTES, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+)\s+.*?\beol=(crlf|lf)\b", line)
        if m:
            rules.append((m.group(1), m.group(2)))
    if not rules:
        return ["`.gitattributes` 里没有任何 `eol=crlf` / `eol=lf` 声明 —— 门禁已失效"]

    problems: list[str] = []
    for pattern, want in rules:
        # 本仓库的规则都是字面路径（无通配）；仍用 glob 兼容将来加通配
        matches = glob.glob(os.path.join(ROOT, pattern), recursive=True)
        if not matches:
            continue  # 该文件在本工作区不存在（如 .sh 在别处生成）时不算错
        for path in matches:
            if os.path.isdir(path):
                continue
            raw = open(path, "rb").read()
            bad: list[int] = []
            line_no = 1
            for i, byte in enumerate(raw):
                if byte != 0x0A:
                    continue
                crlf = i > 0 and raw[i - 1] == 0x0D
                if (want == "crlf" and not crlf) or (want == "lf" and crlf):
                    bad.append(line_no)
                line_no += 1
            if bad:
                rel = os.path.relpath(path, ROOT).replace("\\", "/")
                shown = ", ".join(str(n) for n in bad[:20])
                more = f" …共 {len(bad)} 行" if len(bad) > 20 else ""
                problems.append(
                    f"{rel} 要求 eol={want}，但第 {shown} 行{more} 不符"
                    f" → 用编辑器「行尾=CRLF/LF」另存，或对该文件做一次整文件行尾归一化")
    return problems


def _self_update(tests: int, components: int, pages: int) -> None:
    """把实测值回写本文件的期望常量。"""
    path = os.path.abspath(__file__)
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"EXPECTED_TESTS\s*=\s*\d+", f"EXPECTED_TESTS = {tests}", text)
    text = re.sub(r"EXPECTED_COMPONENTS\s*=\s*\d+",
                  f"EXPECTED_COMPONENTS = {components}", text)
    text = re.sub(r"EXPECTED_PAGES\s*=\s*\d+", f"EXPECTED_PAGES = {pages}", text)
    open(path, "w", encoding="utf-8", newline="\n").write(text)
    print(f"已回写 {path}")
    print(f"  EXPECTED_TESTS      = {tests}")
    print(f"  EXPECTED_COMPONENTS = {components}")
    print(f"  EXPECTED_PAGES      = {pages}")


def main() -> int:
    parser = argparse.ArgumentParser(description="qmt_work 文档/代码计数核对")
    parser.add_argument("--update", action="store_true",
                        help="用实测值回写本文件期望常量后退出")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    tests = components = pages = 0
    if _pytest_available():
        tests = collect_backend_tests()
    components = count_frontend_components()
    pages = count_pages()

    if args.update:
        _self_update(tests, components, pages)
        return 0

    checks = [
        ("backend tests", tests, EXPECTED_TESTS, "改动了测试文件或用例，请同步 README 与 EXPECTED_TESTS"),
        ("frontend components", components, EXPECTED_COMPONENTS,
         "增删了 .jsx 组件，请同步 README 与 EXPECTED_COMPONENTS"),
        ("registered pages", pages, EXPECTED_PAGES,
         "页面注册表有增删，请同步 README 与 EXPECTED_PAGES"),
    ]
    ok = True
    for name, actual, expected, hint in checks:
        flag = "✓" if actual == expected else "✗"
        print(f"[{flag}] {name} = {actual} (expected {expected})")
        if actual != expected:
            ok = False
            print(f"     → {hint}；执行 `python scripts/ci_reconcile.py --update` 可回写期望值")

    # README 里写死的计数同样要核（否则文档可以无限期地说错话）
    readme_problems = check_readme_numbers(tests, pages)
    if readme_problems:
        ok = False
        for p in readme_problems:
            print(f"[✗] README: {p}")
        print("     → 请更新 README 对应数字（或删掉该处数字声明）")
    else:
        print("[✓] README 计数声明与实测一致")

    # P1-5：前端 API 层 ↔ 后端 REST 契约（前端调了后端没有的端点 ⇒ 运行时 404）
    drift = check_api_contract_drift()
    if drift:
        ok = False
        for p in drift:
            print(f"[✗] API 契约: {p}")
        print("     → 修前端路径或补后端端点；详见 "
              "`python scripts/check_api_contract_drift.py -v`")
    else:
        print("[✓] 前端 API 调用全部命中后端契约")

    # 行尾契约（.gitattributes ↔ 工作区字节）：混排会让 .bat 的括号块被撕裂
    eol = check_line_endings()
    if eol:
        ok = False
        for p in eol:
            print(f"[✗] 行尾契约: {p}")
    else:
        print("[✓] .gitattributes 声明的行尾与工作区一致")

    if not ok:
        print("RECONCILE FAILED")
        return 1
    print("RECONCILE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
