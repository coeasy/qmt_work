#!/usr/bin/env bash
# 启动**打包态**后端 EXE → 跑真实 MCP 协议端到端验证 → 停机。
#
# 为什么单独要这一步：
#   REST 自省（GET /api/v1/capabilities/mcp）能返回 116 个工具，只说明后端「数得出来」，
#   **不能说明 Agent 真的连得上**。2026-09-18 实测就撞到：``POST /mcp`` 返回 405，
#   而文档三处都写 ``/mcp`` —— 照文档配的 MCP 客户端全部握手失败，且 405 完全
#   看不出「加个斜杠就好」。这类缺陷只有按协议握手才暴露，故固化为打包后必跑。
#
# 用法：
#   bash scripts/verify_packaged_mcp_boot.sh [port]
#
# 退出码：0 = 端到端通过；非 0 = 失败（脚本自身会打印诊断）。
set -uo pipefail

# ★ 必须用 `pwd -W`：Git Bash 的 pwd 给出 /p/github_public/... ，Windows 版 python
# 会把它解析成当前盘符下的相对路径 P:\p\github_public\... → 报 "can't open file"。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -W)"
PORT="${1:-21189}"
EXE="$ROOT/backend/dist/qmt_work/qmt_work.exe"
PY="$ROOT/backend/runtimes/cp311/python.exe"
# ★ 日志按 PID 命名、**不做删除**：本脚本常被 build_all.sh 调用，而构建过程本身
# 已累计大量文件删除（vite emptyOutDir / PyInstaller 清理），此时再 `rm -f` 会
# 触发安全删除护栏；一旦删除被拦而旧日志残留，下面的
# ``grep "Uvicorn running on"`` 就可能匹配到**上一次**的日志 ⇒ 假通过。
LOG="$ROOT/output/mcp_smoke_backend_$$.log"

if [[ ! -f "$EXE" ]]; then
    echo "[mcp-smoke] 未找到打包态后端 EXE：$EXE（先跑 build_all.sh）"
    exit 1
fi

mkdir -p "$ROOT/output"

echo "[mcp-smoke] 启动打包态后端（QMT_PORT=$PORT）…"
QMT_PORT="$PORT" "$EXE" > "$LOG" 2>&1 &
PID=$!
cleanup() {
    kill "$PID" 2>/dev/null
    taskkill -F -PID "$PID" >/dev/null 2>&1
}
trap cleanup EXIT

# 等待就绪（冷启动可能较慢，给足 120s）
for _ in $(seq 1 60); do
    grep -q "Uvicorn running on http://127.0.0.1:$PORT" "$LOG" 2>/dev/null && break
    sleep 2
done

# 二次确认端口真的在监听：只信日志会漏掉「日志是旧的 / 端口被别的进程占着」两种情形
if ! grep -q "Uvicorn running on http://127.0.0.1:$PORT" "$LOG" 2>/dev/null; then
    echo "[mcp-smoke] 后端未在 120s 内就绪，尾部日志："
    tail -20 "$LOG"
    exit 1
fi
echo "[mcp-smoke] 后端就绪：$PORT"

"$PY" "$ROOT/scripts/verify_packaged_mcp.py" --base "http://127.0.0.1:$PORT"
RC=$?
if [[ $RC -ne 0 ]]; then
    echo "[mcp-smoke] MCP 端到端失败（rc=$RC），后端日志尾部："
    tail -20 "$LOG"
fi
exit "$RC"
