#!/usr/bin/env bash
# qmt_work 一键构建脚本（Linux / Git Bash / macOS）
#
# 流程：前端构建 → 后端 EXE（PyInstaller）→ Electron 桌面壳打包
#
# 用法:
#   bash build_all.sh                 全流程（zip 便携版）+ 构建后自检
#   bash build_all.sh --nsis          同时产出 NSIS 安装包（需 NSIS，否则退回 zip）
#   bash build_all.sh --portable      等价于默认：仅 zip 便携版
#   bash build_all.sh --backend-only  仅后端 EXE（Step 1 前端 + Step 2 PyInstaller，跳过 Electron）
#   bash build_all.sh --desktop-only  仅 Electron（需 backend/dist 已存在）
#   bash build_all.sh --skip-frontend 跳过前端构建（需 backend/static 已存在）
#   bash build_all.sh --no-verify     跳过构建后自检（client_start_test.py）
#   bash build_all.sh --clean-dist    打包前彻底删除上一轮 backend/dist 产物
#   bash build_all.sh --force         跳过运行中实例检测（不推荐）
#
# 环境变量（可选）:
#   QMT_UPDATE_URL   自动更新服务器地址（默认 GitHub Releases 占位）
#   CSC_LINK         Windows 代码签名证书路径（*.p12，配合 signtool）
#   CSC_KEY_PASSWORD 证书密码（设置后自动签名，消除 SmartScreen 告警）
#   QMT_PYTHON       指定后端构建解释器（默认见 resolve_python）
#   QMT_NODE         指定前端构建解释器（默认见 resolve_node）
#
# 本脚本只删除【构建产物】（backend/dist/qmt_work 与 backend/dist/qmt_work.exe，
# Electron 自身会重建 dist-electron/）。前端产物 backend/static 由 vite emptyOutDir
# 自行清理，本脚本不做 rm。不触碰源码与 data/。
# 沙箱环境下 PyInstaller 写文件可能被拦，请用 --dangerouslyDisableSandbox 运行本脚本。

set -euo pipefail

# set -e 下的静默失败极难定位：脚本会直接退出、什么都不打印（实测踩过两次，
# 都是「函数最后一句话是 `[[ ... ]] && cmd`，条件为假时函数返回 1」）。
# 这里补一条 ERR trap，至少把出错行号与退出码打出来。
trap 'rc=$?; echo "[error] 构建中止：${BASH_SOURCE[0]:-?}:${BASH_LINENO[0]:-?}（退出码 $rc）" >&2' ERR

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend-next"
export ROOT BACKEND FRONTEND

# Git Bash 下 $ROOT 是 POSIX 形式（/p/github_public/...），而 Windows 原生 Python
# 会把 `/p/...` 解析成盘符 `P:` + 相对路径 `p/...` → P:\p\github_public\...（不存在）。
# 凡是要传给 Python 的路径都必须先转成 Windows 形式；bash 自身的 [[ -f ]] / cd 用 POSIX 即可。
wpath() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        printf '%s' "$1"
    fi
}

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[build]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
info() { echo -e "${NC}$*"; }
fail() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

SKIP_FRONTEND=false; BACKEND_ONLY=false; DESKTOP_ONLY=false
USE_NSIS=false; FORCE=false; VERIFY=true; CLEAN_DIST=false
for arg in "$@"; do
    case "$arg" in
        --skip-frontend) SKIP_FRONTEND=true ;;
        --backend-only)  BACKEND_ONLY=true ;;
        --desktop-only)  DESKTOP_ONLY=true ;;
        --nsis)          USE_NSIS=true ;;
        --portable)      USE_NSIS=false ;;
        --force)         FORCE=true ;;
        --no-verify)     VERIFY=false ;;
        --clean-dist)    CLEAN_DIST=true ;;
        --help|-h)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg（用 --help 查看用法）"; exit 1 ;;
    esac
done

# ────────────────────────── 解释器解析 ──────────────────────────
# 构建对解释器敏感：PyInstaller 必须用装了 fastmcp 的 venv，Electron 需要 Node 20+。
# 裸 `python`/`npm` 在 CI 与其他开发机上可能是别的解释器 → 产出不可用的包。
resolve_python() {
    if [[ -n "${QMT_PYTHON:-}" ]]; then
        [[ -x "$QMT_PYTHON" ]] || fail "QMT_PYTHON 不存在: $QMT_PYTHON"
        echo "$QMT_PYTHON"; return
    fi
    local c
    for c in \
        "$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe" \
        "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
        "$ROOT/backend/.venv/Scripts/python.exe" \
        "$ROOT/backend/.venv/bin/python"; do
        if [[ -x "$c" ]]; then echo "$c"; return; fi
    done
    command -v python3 || command -v python || fail \
        "找不到可用的 Python：设置 QMT_PYTHON 或先建 backend/.venv"
}

resolve_node() {
    if [[ -n "${QMT_NODE:-}" ]]; then
        [[ -x "$QMT_NODE" ]] || fail "QMT_NODE 不存在: $QMT_NODE"
        echo "$QMT_NODE"; return
    fi
    local d c
    # 版本目录名可能带补丁后缀（如 22.22.2-3），故用通配而非精确路径
    for d in "$HOME/.workbuddy/binaries/node/versions/"*; do
        [[ -d "$d" ]] || continue
        for c in "$d/node.exe" "$d/bin/node"; do
            if [[ -x "$c" ]]; then echo "$c"; return; fi
        done
    done
    command -v node || fail "找不到 node：设置 QMT_NODE 或安装 Node 20+"
}

PY="$(resolve_python)"
NODE="$(resolve_node)"
NODE_DIR="$(dirname "$NODE")"

# Python 版本校验：pyproject 约束 >=3.11,<3.12，越界直接失败而不是产出半包
PY_VER="$("$PY" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "?")"
case "$PY_VER" in
    3.11) ;;
    3.12) warn "Python $PY_VER 超出 pyproject 约束 >=3.11,<3.12（CI 亦固定 3.11），构建结果可能与 CI 不一致" ;;
    3.1[3-9]|3.[4-9][0-9]) warn "Python $PY_VER 超出 pyproject 约束 >=3.11,<3.12（CI 固定 3.11）"
                          warn "本地功能可用，但打包出的 EXE 运行时与 CI 校验矩阵不一致"
                          warn "建议：python3.11 -m venv backend/.venv && backend/.venv/Scripts/python.exe build_exe.py"
                          warn "或显式指定：QMT_PYTHON=<你的3.11解释器> bash build_all.sh" ;;
    *) warn "无法确认 Python 版本（$PY_VER），跳过约束校验" ;;
esac
NODE_MAJOR="$("$NODE" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo '?')"
NODE_VER="$("$NODE" -v 2>/dev/null || echo '?')"
NODE_VER="${NODE_VER#v}"   # node -v 自带 v 前缀，横幅里另加一个 v
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] && (( NODE_MAJOR < 20 )) && \
    warn "Node $NODE_VER 低于建议的 20+，可能触发构建告警"

# ────────────────────────── 运行中实例检测 ──────────────────────────
# 构建会覆盖运行中的 EXE/静态资源，导致句柄异常（前端 500、StaticFiles 崩）。
detect_running() {
    local exe_name="$1" msg="$2"
    local match="${exe_name%.exe}"
    if command -v tasklist >/dev/null 2>&1; then
        local hit
        hit=$(tasklist 2>/dev/null | grep -i "${match}" || true)
        if [[ -n "$hit" ]]; then
            if [[ "$FORCE" == true ]]; then
                warn "$msg 正在运行（$exe_name）—— --force 继续（不推荐）"
            else
                fail "$msg 正在运行（$exe_name）。请先完全退出（托盘右键→退出，或任务管理器结束 $exe_name），或用 --force 跳过"
            fi
        fi
    fi
}
[[ "$DESKTOP_ONLY" == false ]] && detect_running "qmt_work.exe" "后端 EXE 实例"
detect_running "electron.exe" "桌面壳实例"

# ────────────────────────── 静态资源就绪闸门 ──────────────────────────
# 历史教训：vite 构建中途（assets 已被重命名清理）就开始 PyInstaller，
# 产出的 EXE 只有 6/32 个静态文件 → 桌面端白屏，且 exit code 0 无告警。
# build_exe.py 内有同类闸门，此处是更早的失败点，便于定位是前端还是打包。
verify_static_ready() {
    local idx="$BACKEND/static/index.html"
    local assets="$BACKEND/static/assets"
    [[ -f "$idx" ]] || fail "缺少 $idx：请先完成前端构建"
    [[ -d "$assets" ]] || fail "缺少 $assets：请先完成前端构建"
    local n
    n=$( { find "$assets" -name '*.js' 2>/dev/null || true; } | wc -l | tr -d ' ')
    (( n > 0 )) || fail "$assets 下无 js 分片，前端构建未完成"
    # index.html 引用的入口资源必须真实存在
    local m missing=0
    # grep 出来的是 index.html 里的原始 URL（带 /assets/ 前缀），
    # 落到磁盘时要剥掉这一层再拼 assets 目录，否则会拼成 assets//assets/xxx.js。
    while IFS= read -r m; do
        [[ -z "$m" ]] && continue
        m="${m#/assets/}"
        [[ -f "$assets/$m" ]] || { warn "index.html 引用了不存在的 $m"; missing=$((missing+1)); }
    done < <(grep -oE '/assets/[A-Za-z0-9_.-]+\.(js|css)' "$idx" 2>/dev/null || true)
    (( missing > 0 )) && fail "前端产物不完整（$missing 个引用缺失）：vite 可能仍在写入，请重试"
    log "静态资源就绪：assets 下 $n 个 js 分片，index.html 引用齐备"
}

# ────────────────────────── 清理旧产物 ──────────────────────────
# PyInstaller --noconfirm 只覆盖同名文件，不会清掉上一轮 COLLECT 的残留
# （含已被 vite 重命名清理的旧分片名），会导致「产物数 > 源文件数」的幽灵文件。
# 默认不清理 PyInstaller 输出目录。原因：上一次 COLLECT 常有数万个文件，
# 批量 rm 在装有安全删除 shim 的机器上会被拦（实测 3096 个 > 阈值 50），
# 而这一步失败会让整条构建流水线在 Step 2 直接中断。
# PyInstaller --noconfirm 会覆盖同名文件，产物依然正确；残留的旧分片名
# 属于「孤儿文件」，由下方打包后的 static 核对列出，而不是默默混进包里。
# 确实想彻底清一次时显式加 --clean-dist。
clean_dist() {
    local d="$BACKEND/dist/qmt_work" n=0
    # ⚠️ 不能写成 `n=$(find "$d" ... | wc -l)`：目录不存在时 find 返回 1，脚本又开了
    # pipefail ⇒ 整个命令替换失败 ⇒ set -e 让构建静默中止（实测：Step 2 刚打印完就退出，
    # 无任何错误输出）。用 `{ ... || true; }` 兜住，缺失目录等价于「0 个文件」。
    n=$( { find "$d" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')
    if [[ "$CLEAN_DIST" != true ]]; then
        # 同样必须用 if：`[[ ... ]] && log ...` 在 n=0（dist 不存在或刚被清空）时返回 1，
        # 紧跟的裸 `return` 会把这个 1 带出去，配合 set -e 让构建在 Step 2 静默终止。
        if [[ "$n" != "0" ]]; then
            log "保留上一轮产物 $d（$n 个文件），PyInstaller 将覆盖同名文件；彻底清理请加 --clean-dist"
        else
            log "无上一轮产物 $d，将全新打包"
        fi
        return 0
    fi
    if [[ -d "$d" ]]; then rm -rf "$d" && log "已清理 $d"; fi
}
# 前端产物由 vite 自行清理：frontend-next/vite.config.ts 已设 emptyOutDir: true，
# vite 会整体清空 ../backend/static 再写入。此处不再 rm -rf —— 那既冗余，
# 又会因文件数超阈值触发沙箱批量删除保护（实测 64 个分片被拦）。
# 完整性仍由 verify_static_ready 兜住（引用缺失 / 无 js 分片 → 直接 fail）。
clean_static_assets() {
    local n
    # 同 clean_dist：find 目标不存在会返回 1，配合 pipefail + set -e 静默中止构建。
    n=$( { find "$BACKEND/static" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')
    # 必须用 if 而不是 `[[ ... ]] && log ...`：后者在条件为假时让函数返回 1，
    # 而调用处没有 `|| true`，配合脚本顶部的 set -e 会让整个构建静默 exit 1
    # （没有任何错误输出）。触发条件是「上一轮前端产物为空」——即首次构建、
    # 或刚被清空 backend/static 之后，正是最不该失败的时刻。
    if [[ -n "$n" && "$n" != "0" ]]; then
        log "检测到上一轮前端产物 $n 个文件，vite emptyOutDir 会整体清空"
    else
        log "backend/static 为空，vite 将全新写入（无需清理）"
    fi
}

# electron-builder 的退出码不足以判定成败：实测在【产物已全部生成之后】，它还会去删除
# 中间文件（如 *.nsis.7z），这一步会被本机的安全删除护栏拦下 ⇒ 退出码非 0，但
# qmt_work-Setup-*.exe 与 *.zip 都已落地。此时若直接 fail，后面的「包内 static 核对」
# 与 Step 4 自检会被整段跳过，把「已完成」误报成「失败」。
# 判据：win-unpacked/qmt_work.exe 存在 ⇒ 打包本身成功，降级为 warn 并继续。
run_electron_builder() {
    if PATH="$NODE_DIR:$PATH" node node_modules/electron-builder/cli.js "$@"; then
        return 0
    fi
    if [[ -f "$FRONTEND/dist-electron/win-unpacked/qmt_work.exe" ]]; then
        warn "electron-builder 退出码非 0，但 win-unpacked/qmt_work.exe 已生成 —— 视为打包成功"
        warn "（多为收尾清理中间文件被环境安全删除护栏拦下；中间 *.nsis.7z 可手动清理）"
        return 0
    fi
    return 1
}

# ────────────────────────── 开始 ──────────────────────────
if [[ -z "${QMT_UPDATE_URL:-}" ]]; then
    export QMT_UPDATE_URL="https://github.com/coeasy/qmt_work/releases/download"
    warn "QMT_UPDATE_URL 未设置，使用默认: $QMT_UPDATE_URL"
    warn "请按实际仓库地址设置环境变量后重新构建。"
fi

echo "========================================="
echo " qmt_work 一键构建"
echo " 工作目录: $ROOT"
echo " python  : $PY (v$PY_VER)"
echo " node    : $NODE (v$NODE_VER)"
echo " 更新源  : $QMT_UPDATE_URL"
echo "========================================="
echo ""

# ---- 前端依赖 ----
if [[ ! -d "$FRONTEND/node_modules" ]]; then
    log "frontend-next/node_modules 不存在，执行 npm install ..."
    (cd "$FRONTEND" && PATH="$NODE_DIR:$PATH" npm install) || fail "npm install 失败"
fi

# ---- Step 1: 前端构建 ----
if [[ "$DESKTOP_ONLY" == false ]]; then
    if [[ "$SKIP_FRONTEND" == false ]]; then
        log "Step 1/3: 前端构建"
        cd "$FRONTEND"
        [[ -f package.json ]] || fail "frontend-next/package.json 不存在"
        clean_static_assets
        PATH="$NODE_DIR:$PATH" npm run build || { cd "$ROOT"; fail "前端构建失败"; }
        cd "$ROOT"
        verify_static_ready
        log "前端构建完成 → backend/static/"
    else
        warn "跳过前端构建（--skip-frontend）"
        [[ -f "$BACKEND/static/index.html" ]] || fail "--skip-frontend 但 backend/static/index.html 不存在"
    fi
fi

# ---- Step 2: 后端 EXE ----
#
# ★ 2026-09-22 修的真缺陷：这里原先包了一层 `if [[ "$BACKEND_ONLY" == false ]]`，
#   于是 `--backend-only` 变成「跑完 Step 1 前端就直接 exit 0」，**PyInstaller 一次都没跑**。
#   而脚本头部文档写的是「--backend-only  仅后端 EXE」—— 契约与实现相反。
#   危害在于它**不报错**：日志照样打印「[build] 仅构建后端 EXE 完成」，
#   产物却仍是上一轮的（`backend/dist/qmt_work/qmt_work.exe` 时间戳不变），
#   极易把「复用了旧 EXE」误判成「构建成功」。
#   判据（本轮实测）：`--backend-only` 的日志里**没有** "Step 2/3" 与 build_exe.py 的输出。
#   正确语义：只要不是 --desktop-only，Step 2 就必须执行；--backend-only 只是
#   「执行完 Step 2 后停下，不进 Step 3（Electron）」。
if [[ "$DESKTOP_ONLY" == false ]]; then
    log "Step 2/3: 后端 EXE 打包（PyInstaller）"
    clean_dist
    cd "$BACKEND"
    [[ -f build_exe.py ]] || fail "backend/build_exe.py 不存在"
    [[ -f static/index.html ]] || warn "backend/static 为空，打包出的 EXE 无法托管前端"
    "$PY" build_exe.py || { cd "$ROOT"; fail "后端 EXE 打包失败"; }
    cd "$ROOT"
    [[ -f "$BACKEND/dist/qmt_work/qmt_work.exe" ]] \
        || fail "打包脚本退出 0 但产物缺失: $BACKEND/dist/qmt_work/qmt_work.exe"
    log "后端 EXE 完成 → $BACKEND/dist/qmt_work/qmt_work.exe"

    if [[ "$BACKEND_ONLY" == true ]]; then
        echo ""
        log "仅构建后端 EXE 完成（Step 1 + Step 2，已跳过 Electron）"
        exit 0
    fi
fi

# ---- Step 3: Electron 打包 ----
log "Step 3/3: Electron 桌面壳打包"
cd "$FRONTEND"
if [[ -f "$BACKEND/dist/qmt_work/qmt_work.exe" ]]; then
    log "检测到后端 EXE，将随包分发"
else
    fail "未找到后端 EXE ($BACKEND/dist/qmt_work/qmt_work.exe)，桌面壳无法启动后端"
fi

# 直接调 electron-builder，而不用 npm run dist / dist:portable：
# 那两条 script 会串跑 `npm run build` 与 `backend:build`，即重复一次前端构建
# 和一次 PyInstaller 打包（本次已各自执行过），纯浪费时间且可能引入竞态。
# 注：前端不进 asar（extraResources: ../backend/dist 已含 static），故无需重建。
if [[ "$USE_NSIS" == true ]]; then
    if ! command -v makensis >/dev/null 2>&1 \
       && [[ ! -f "/c/Program Files (x86)/NSIS/makensis.exe" ]]; then
        warn "本机未检测到 NSIS，electron-builder 将尝试按需下载；失败则退回仅 zip"
    fi
    log "打包 NSIS 安装包 + zip 便携版"
    run_electron_builder --win nsis zip \
        || { cd "$ROOT"; fail "Electron 打包失败（NSIS）：可用 --portable 仅出 zip"; }
else
    log "打包 zip 便携版（--nsis 可同时产出 NSIS 安装包）"
    run_electron_builder --win zip \
        || { cd "$ROOT"; fail "Electron 打包失败"; }
fi
cd "$ROOT"
log "Electron 打包完成 → $FRONTEND/dist-electron/"
UNPACKED="$FRONTEND/dist-electron/win-unpacked/qmt_work.exe"
[[ -f "$UNPACKED" ]] || fail "桌面壳产物缺失: $UNPACKED"

# 包内静态资源完整性核对（白屏问题只能靠这一层发现）。
# electron-builder 的 extraResources 把 ../backend/dist 的【内容】拷入 resources/backend，
# 故 dist/qmt_work 在包内变成 backend/qmt_work（注意没有 dist 层）。
PACKED_STATIC="$FRONTEND/dist-electron/win-unpacked/resources/backend/qmt_work/_internal/static"
[[ -d "$PACKED_STATIC" ]] || PACKED_STATIC="$FRONTEND/dist-electron/win-unpacked/resources/backend/dist/qmt_work/_internal/static"
if [[ -d "$PACKED_STATIC" ]]; then
    src_n=$( { find "$BACKEND/static" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')
    out_n=$( { find "$PACKED_STATIC" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')
    info "  包内 static: $out_n / $src_n 个文件"
    if [[ "$out_n" != "$src_n" ]]; then
        if (( out_n > src_n )); then
            warn "包内多出 $((out_n - src_n)) 个孤儿文件（上一轮构建残留，源已不存在）："
            comm -13 <(cd "$BACKEND/static" && find . -type f | sort) \
                     <(cd "$PACKED_STATIC" && find . -type f | sort) \
                | head -20 | sed 's/^/    /'
            warn "这些文件不参与运行（index.html 不引用），但会增大包体积；可用 --clean-dist 彻底清理后重打包"
        else
            warn "包内 static 文件数少于源（$out_n < $src_n）：可能是打包中断，建议 --clean-dist 后重打包"
        fi
    fi
    # index.html 里的引用带 /assets/ 前缀，拼到磁盘上要剥掉再补 assets/
    entry=$(grep -oE '/assets/[A-Za-z0-9_.-]+\.js' "$BACKEND/static/index.html" 2>/dev/null | head -1)
    entry="${entry#/assets/}"
    if [[ -z "$entry" ]]; then
        warn "index.html 中未找到 js 入口引用，跳过入口核对"
    elif [[ ! -f "$PACKED_STATIC/assets/$entry" ]]; then
        warn "包内缺少入口 assets/$entry：桌面端可能白屏"
    else
        info "  入口 assets/$entry 在包内"
    fi
else
    warn "未在包内找到 static 目录，跳过完整性核对"
fi

# ---- Step 4: 构建后自检 ----
echo ""
if [[ "$VERIFY" == true ]]; then
    if [[ -f "$ROOT/scripts/client_start_test.py" ]]; then
        log "Step 4/4: 构建后自检（启动客户端 → REST/WS/窗口冒烟）"
        if "$PY" "$(wpath "$ROOT/scripts/client_start_test.py")" --target client; then
            log "自检通过：产物可用"
        else
            warn "自检未全部通过（见上方输出）。构建产物已生成，但请人工复核。"
            VERIFY_RC=1
        fi
    else
        warn "未找到 scripts/client_start_test.py，跳过自检"
    fi

    # ---- Step 4.5: MCP 协议端到端（打包态） ----
    #
    # 为什么要单列这一步：REST 自省能列出 116 个工具，**不等于 Agent 真能连上**。
    # 实测撞到过 ``POST /mcp`` 405（文档三处都写 ``/mcp``，照文档配的客户端全挂），
    # 而自省走的是另一条代码路径，照样返回 116 —— 只有按协议握手才暴露。
    if [[ -f "$ROOT/scripts/verify_packaged_mcp_boot.sh" && -f "$BACKEND/dist/qmt_work/qmt_work.exe" ]]; then
        log "Step 4.5: MCP 协议端到端（启动打包态后端 → 真握手 → tools/call）"
        if bash "$ROOT/scripts/verify_packaged_mcp_boot.sh" 21189; then
            log "MCP 端到端通过：Agent 可接入"
        else
            warn "MCP 端到端未通过（见上方输出）。产物已生成，但 Agent 可能无法接入，请人工复核。"
            VERIFY_RC=1
        fi
    else
        info "未找到 MCP 冒烟脚本或后端 EXE，跳过 MCP 端到端"
    fi
else
    info "跳过自检（--no-verify）"
fi

echo ""
echo "========================================="
echo " 构建完成"
echo " 后端 EXE : $BACKEND/dist/qmt_work/qmt_work.exe"
echo " 桌面客户端: $UNPACKED"
echo " 安装包目录: $FRONTEND/dist-electron/"
echo " 更新源   : $QMT_UPDATE_URL"
echo "========================================="
exit "${VERIFY_RC:-0}"
