#!/usr/bin/env bash
# 一次性冒烟编排：同一 bash 调用内「起后端 → 探活 → 跑冒烟 → 停后端」。
# 为什么必须同一调用：后台进程跨工具调用会被回收（见项目记忆 §7）。
# 用法：bash frontend/tests/_run_smoke.sh [port]
set -u
export PATH="/usr/bin:/bin:$PATH"
PORT="${1:-21118}"
ROOT="/p/github_public/qmt_work"
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
NODE="C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"

echo "== 清理残留 =="
taskkill //F //IM qmt_work.exe >/dev/null 2>&1 || true
taskkill //F //IM electron.exe >/dev/null 2>&1 || true

echo "== 启动后端 :$PORT =="
cd "$ROOT/backend" || exit 1
QMT_PORT="$PORT" nohup "$PY" run.py > "$ROOT/backend/_smoke_srv.log" 2>&1 &
SRV_PID=$!

echo "== 探活 =="
UP=0
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --noproxy '*' --max-time 3 \
    "http://127.0.0.1:$PORT/api/v1/live" 2>/dev/null)
  if [ "$code" = "200" ]; then echo "ready after ${i}s"; UP=1; break; fi
  sleep 1
done
if [ "$UP" != "1" ]; then
  echo "[FATAL] 后端未就绪，日志尾部："
  tail -30 "$ROOT/backend/_smoke_srv.log"
  exit 1
fi

echo "== 跑全页渲染冒烟 =="
cd "$ROOT/frontend" || exit 1
DEV_PORT="$PORT" "$NODE" tests/render_smoke_all.mjs
RC=$?

echo "== 停止后端 =="
kill "$SRV_PID" >/dev/null 2>&1 || true
taskkill //F //PID "$SRV_PID" >/dev/null 2>&1 || true
exit $RC
