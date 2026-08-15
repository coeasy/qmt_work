#!/usr/bin/env bash
# qmt_work 一键构建脚本
# 依次执行：前端构建 -> 后端 EXE -> Electron 桌面壳打包（默认 NSIS 安装包）
# 用法:
#   chmod +x build_all.sh && ./build_all.sh
#   或: bash build_all.sh
#
# 可选参数:
#   --desktop-only   跳过后端 EXE，仅打包 Electron（需 backend/dist 已存在）
#   --backend-only   仅打包后端 EXE（前端需已构建）
#   --skip-frontend  跳过前端构建（需 backend/static 已存在）
#   --portable       只打 zip 便携版（默认打 NSIS 安装包 + zip）
#   --force          跳过运行中实例检测（不推荐）
#
# 环境变量（可选）:
#   QMT_UPDATE_URL   自动更新服务器地址（默认 GitHub Releases）
#   CSC_LINK         Windows 代码签名证书路径（*.pfx / *.p12）
#   CSC_KEY_PASSWORD 证书密码（设置后自动签名）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[build]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
fail() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# 解析参数
SKIP_FRONTEND=false
BACKEND_ONLY=false
DESKTOP_ONLY=false
USE_NSIS=true
FORCE=false

for arg in "$@"; do
    case "$arg" in
        --skip-frontend) SKIP_FRONTEND=true ;;
        --backend-only)  BACKEND_ONLY=true ;;
        --desktop-only)  DESKTOP_ONLY=true ;;
        --portable)      USE_NSIS=false ;;
        --force)         FORCE=true ;;
        *)               echo "未知参数: $arg"; exit 1 ;;
    esac
done

# ---- 运行中实例检测：构建会覆盖运行中的 EXE/静态资源，
#      导致运行中进程句柄异常（前端 500、StaticFiles 崩），必须先退出 ----
detect_running() {
    local exe_name="$1" msg="$2"
    local match="${exe_name%.exe}"  # 去扩展名，兼容 tasklist 输出格式
    if command -v tasklist >/dev/null 2>&1; then
        local hit
        hit=$(tasklist 2>/dev/null | grep -i "${match}" || true)
        if [[ -n "$hit" ]]; then
            if [[ "$FORCE" == true ]]; then
                warn "$msg 正在运行（$exe_name）—— 使用 --force 继续（不推荐，可能再次引发 500）"
            else
                fail "$msg 正在运行（$exe_name）。请先完全退出（托盘右键→退出，或任务管理器结束所有 $exe_name），再用 --force 跳过本检查"
            fi
        fi
    fi
}

if [[ "$DESKTOP_ONLY" == false ]]; then
    detect_running "qmt_work.exe" "后端 EXE 实例"
fi
detect_running "electron.exe" "桌面壳实例"

# ---- 自动更新地址：未配置时用 GitHub Releases 占位 ----
if [[ -z "${QMT_UPDATE_URL:-}" ]]; then
    export QMT_UPDATE_URL="https://github.com/qmt-work/qmt_work/releases/download"
    warn "QMT_UPDATE_URL 未设置，使用默认: $QMT_UPDATE_URL"
    warn "请按实际仓库地址设置环境变量 QMT_UPDATE_URL 后重新构建。"
fi

echo "========================================="
echo " qmt_work 一键构建"
echo " 工作目录: $ROOT"
echo "========================================="
echo ""

# ---- 前端依赖：node_modules 缺失时自动安装 ----
if [[ ! -d "$FRONTEND/node_modules" ]]; then
    log "frontend/node_modules 不存在，执行 npm install ..."
    (cd "$FRONTEND" && npm install)
fi

# ---- Step 1: 前端构建 ----
if [[ "$DESKTOP_ONLY" == false ]]; then
    if [[ "$SKIP_FRONTEND" == false ]]; then
        log "Step 1/3: 前端构建"
        cd "$FRONTEND"
        if [[ ! -f "package.json" ]]; then
            fail "frontend/package.json 不存在"
        fi
        npm run build
        cd "$ROOT"
        log "前端构建完成 → backend/static/"
    else
        warn "跳过前端构建（--skip-frontend）"
    fi
fi

# ---- Step 2: 后端 EXE ----
if [[ "$DESKTOP_ONLY" == false ]]; then
    if [[ "$BACKEND_ONLY" == false ]]; then
        log "Step 2/3: 后端 EXE 打包（PyInstaller）"
        cd "$BACKEND"
        if [[ ! -f "build_exe.py" ]]; then
            fail "backend/build_exe.py 不存在"
        fi
        # 检查 static 目录
        if [[ ! -d "static" || -z "$(ls -A static 2>/dev/null)" ]]; then
            warn "backend/static 为空，前端可能未构建（打包后 EXE 无法托管前端）"
        fi
        python build_exe.py
        cd "$ROOT"
        log "后端 EXE 完成 → backend/dist/qmt_work/qmt_work.exe"
    fi

    if [[ "$BACKEND_ONLY" == true ]]; then
        echo ""
        log "仅构建后端 EXE 完成"
        exit 0
    fi
fi

# ---- Step 3: Electron 打包 ----
log "Step 3/3: Electron 桌面壳打包"
cd "$FRONTEND"

# 检查后端 EXE 是否存在
BACKEND_EXE="$BACKEND/dist/qmt_work/qmt_work.exe"
if [[ -f "$BACKEND_EXE" ]]; then
    log "检测到后端 EXE，将随包分发"
else
    warn "未找到后端 EXE ($BACKEND_EXE)，桌面壳打包后无法启动后端"
fi

if [[ "$USE_NSIS" == true ]]; then
    log "打包 NSIS 安装包 + zip 便携版"
    npm run dist
else
    log "仅打包 zip 便携版"
    npm run dist:portable
fi

cd "$ROOT"
log "Electron 打包完成 → frontend/dist-electron/"
echo ""
echo "========================================="
echo " 构建完成"
echo " 后端 EXE: $BACKEND_EXE"
echo " 桌面壳:   $FRONTEND/dist-electron/"
echo " 更新源:   $QMT_UPDATE_URL"
echo "========================================="
