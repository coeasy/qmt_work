"""能力覆盖门禁的可证伪性验证（R19 第 3 轮）。

`check_capability_coverage.py` 在 R19-3 把判据从「前端源码里出现过该路径字面量」
收紧为「**声明不算入口、接线才算**」，并新增「豁免清单反腐烂」。这两条都属于
**放松就静默变绿**的类型 —— 必须能用「制造违规 ⇒ 变红」证明它们真的在起作用，
否则修的是一个看不见的洞。每例后字节级还原。

三例：
  A. 把一个**已接线**的调用点注释掉 ⇒ 对应能力必须掉出去（报缺口，rc=1）。
     反证旧判据：旧判据下它仍会被 api 客户端的**声明**判为「有入口」——正是假绿灯。
  B. 还原后 rc=0（对照：证明 A 的红不是因为环境本来就红）。
  C. 把一条**路径级豁免**改成不存在的端点 ⇒ 必须报「过期豁免」（rc=1），
     证明豁免清单不会退化成一份没人敢删的谎话清单。

用法：backend/runtimes/cp311/python.exe -u scripts/verify_capability_coverage_falsifiable.py
"""
import hashlib, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "backend" / "runtimes" / "cp311" / "python.exe"
GATE = ROOT / "scripts" / "check_capability_coverage.py"
PAGE = ROOT / "frontend-next" / "src" / "domains" / "market" / "MarketStructure.tsx"

#: A 例的锚点：`/market/overview` 的**唯一**调用点（MarketStructure 页）。
#: 破坏时必须连**方法名**一起换掉（`.overview(` → `.overviewData(`）——
#: 门禁按「方法名是否有 `.name(` 调用点」判定，只换对象名（`marketApi` → `mktApiX`）
#: 仍会命中 `.overview(`，那测的是别的性质，不是本条要证的「接线才算入口」。
CALL_ANCHOR = b"  const res = useAsync(() => marketApi.overview(), []);"
CALL_BROKEN = b"  const res = useAsync(() => mktApiX.overviewData(), []);"


def run():
    # --verbose：让缺口逐条打印路径，断言才能精确到「是哪一个端点掉了」。
    r = subprocess.run([str(PY), str(GATE), "--verbose"], capture_output=True,
                       text=True, cwd=str(ROOT), timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


fails = []

# ---------------------------------------------------------------- A / B
page_raw, page_sha = PAGE.read_bytes(), sha(PAGE)
if CALL_ANCHOR not in page_raw:
    print("! A 例锚点未命中，跳过")
    fails.append("A")
else:
    try:
        PAGE.write_bytes(page_raw.replace(CALL_ANCHOR, CALL_BROKEN, 1))
        rc, out = run()
        gap_hit = "/api/v1/market/overview" in out and "未覆盖且未豁免" in out
        ok = (rc == 1 and gap_hit)
        print(f"  [{'OK ' if ok else 'BAD'}] A. 注释掉唯一调用点 ⇒ 报缺口 rc={rc} "
              f"(期望 1；缺口命中={gap_hit})")
        if not ok:
            print("      输出尾部：", out.strip().splitlines()[:6])
            fails.append("A")
    finally:
        PAGE.write_bytes(page_raw)
    restored = sha(PAGE) == page_sha
    print(f"  [{'OK ' if restored else 'BAD'}] 还原 MarketStructure.tsx（一致={restored}）")
    if not restored:
        fails.append("A[还原失败]")

    rc, out = run()
    ok = (rc == 0)
    print(f"  [{'OK ' if ok else 'BAD'}] B. 还原后对照 ⇒ rc={rc} (期望 0)")
    if not ok:
        print("      输出尾部：", out.strip().splitlines()[:6])
        fails.append("B")

# ------------------------------------------------------------------- C
gate_raw, gate_sha = GATE.read_bytes(), sha(GATE)
STALE_OLD = b'"GET /api/v1/account/slippage":'
STALE_NEW = b'"GET /api/v1/account/slippage-does-not-exist":'
if STALE_OLD not in gate_raw:
    print("! C 例锚点未命中，跳过")
    fails.append("C")
else:
    try:
        GATE.write_bytes(gate_raw.replace(STALE_OLD, STALE_NEW, 1))
        rc, out = run()
        stale_hit = "过期豁免" in out and "后端已无此端点" in out
        ok = (rc == 1 and stale_hit)
        print(f"  [{'OK ' if ok else 'BAD'}] C. 豁免指向不存在的端点 ⇒ 报过期 rc={rc} "
              f"(期望 1；过期命中={stale_hit})")
        if not ok:
            print("      输出尾部：", out.strip().splitlines()[:8])
            fails.append("C")
    finally:
        GATE.write_bytes(gate_raw)
    restored = sha(GATE) == gate_sha
    print(f"  [{'OK ' if restored else 'BAD'}] 还原 check_capability_coverage.py（一致={restored}）")
    if not restored:
        fails.append("C[还原失败]")

rc, out = run()
ok = (rc == 0)
print(f"  [{'OK ' if ok else 'BAD'}] 终态对照 ⇒ rc={rc} (期望 0)")
if not ok:
    fails.append("终态")

print(f"\n结果：{'全部通过' if not fails else '失败 ' + str(fails)}")
sys.exit(0 if not fails else 1)
