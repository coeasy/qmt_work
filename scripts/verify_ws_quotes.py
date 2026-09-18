"""WS 实时行情链路验收：起后端 → 订阅真实标的 → 收集行情帧 → 判定 → 关后端。

验证的是「列表里的价格到底会不会动」这条链路：
    前端 useLiveQuotes → acquireQuotes → WS {"action":"subscribe","codes":[...]}
    → 后端推 {type:"quotes"|"snapshot", ...}
REST 有真实数据 ≠ WS 会推。两者断裂时列表永远显示 `--`，且不报错。

必须在**单个进程内**跑完（本环境跨 Bash 调用会回收后台进程）。
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
PY = os.path.join(BACKEND, "runtimes", "cp311", "python.exe")
PORT = 21118
BASE = f"http://127.0.0.1:{PORT}/api/v1"
# ⚠️ 路径必须是 /api/v1/ws（前端 services/ws.ts:65 就是这么拼的），不是 /ws
WS = f"ws://127.0.0.1:{PORT}/api/v1/ws"

CODES = ["000001.SZ", "600519.SH", "513090.SH"]  # 最后一只 = 真实持仓（回归标的，见 §8.19.10）

env = dict(os.environ)
tmp = os.path.join(ROOT, "output", "tmp")
os.makedirs(tmp, exist_ok=True)
env["TMP"] = tmp
env["TEMP"] = tmp
env["QMT_PORT"] = str(PORT)
# 关掉代理，否则本机 127.0.0.1 可能被系统代理截走
env["NO_PROXY"] = "*"
env["no_proxy"] = "*"

proc = subprocess.Popen(
    [PY, "run.py"], cwd=BACKEND, env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)


def get(path, timeout=8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_ready():
    for _ in range(60):
        time.sleep(2)
        try:
            get("/health")
            return True
        except Exception:
            continue
    return False


async def collect(seconds=18):
    import websockets

    frames = []
    async with websockets.connect(WS, open_timeout=10) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=10)
        frames.append(("first", json.loads(first)))
        await ws.send(json.dumps({"action": "subscribe", "codes": CODES}))
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            frames.append(("msg", json.loads(raw)))
    return frames


def summarize(frames):
    print("=" * 72)
    print("第 1 帧：", json.dumps(frames[0][1], ensure_ascii=False)[:300])
    seen = {}
    quotes_frames = 0
    for _, f in frames[1:]:
        t = f.get("type")
        if t == "quotes":
            quotes_frames += 1
        # 两套形状都要认：快照 {type:"snapshot",quotes:{}} / 增量 {type:"quotes",data:{items}}
        items = []
        if isinstance(f.get("quotes"), dict):
            items = list(f["quotes"].values())
        elif isinstance(f.get("data"), dict):
            d = f["data"]
            items = d.get("items") if isinstance(d.get("items"), list) else list(d.values())
        for q in items:
            if not isinstance(q, dict):
                continue
            c = q.get("code")
            p = q.get("price", q.get("last"))
            if c and p:
                seen[c] = (p, q.get("change_pct"))
    print(f"共收到 {len(frames)} 帧，其中 type=quotes {quotes_frames} 帧")
    print("---- 原始帧（逐帧，截断到 400 字符）----")
    for i, (_, f) in enumerate(frames):
        print(f"  [{i}] {json.dumps(f, ensure_ascii=False)[:400]}")
    print("-" * 72)
    ok = True
    for c in CODES:
        if c in seen:
            p, pct = seen[c]
            print(f"  [真实行情] {c}: price={p}  change_pct={pct}")
        else:
            ok = False
            print(f"  [!! 无数据] {c}: WS 未推回任何价格 —— 列表会永远显示 --")
    print("-" * 72)
    print("结论：", "WS 实时行情链路通 ✅" if ok else "WS 未推回行情 ❌（列表将显示 --）")
    print("=" * 72)
    return ok


def main():
    if not wait_ready():
        print("BACKEND_NOT_READY")
        proc.kill()
        sys.exit(2)
    print("后端就绪")
    try:
        frames = asyncio.run(collect())
    except Exception as e:
        print("WS_ERROR:", type(e).__name__, e)
        proc.terminate()
        sys.exit(3)
    ok = summarize(frames)
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
    print("BACKEND_STOPPED")
    sys.exit(0 if ok else 1)


main()
