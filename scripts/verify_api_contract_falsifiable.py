"""P1-5 可证伪性验证：制造 3 类漂移，要求 check_api_contract_drift.py 变红；
  并验证 2 类「不应报警」的对照（参数名不同 / 后端有前端没调）。每例后字节级还原。

★ 常驻守卫：改动 `check_api_contract_drift.py` 的归一化逻辑后必须重跑 ——
  归一化一旦写松（例如把路径参数统一成 `{param}` 这一步去掉），检查会**静默变绿**。

用法：backend/runtimes/cp311/python.exe -u scripts/verify_api_contract_falsifiable.py
"""
import hashlib, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "backend" / "runtimes" / "cp311" / "python.exe"
SCRIPT = ROOT / "scripts" / "check_api_contract_drift.py"
TARGET = ROOT / "frontend-next" / "src" / "services" / "api" / "account.ts"

def run():
    r = subprocess.run([str(PY), str(SCRIPT)], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

orig = TARGET.read_bytes()
orig_sha = sha(TARGET)
print(f"原始 sha256 = {orig_sha[:16]}...  ({len(orig)} bytes)")

CASES = [
    # (说明, 原字节, 新字节, 期望 rc)
    ("A. 端点改名（/account/status → /account/status-typo）",
     b'"/account/status"', b'"/account/status-typo"', 1),
    ("B. 方法错配（GET → DELETE 同一路径）",
     b'http.get<', b'http.delete<', 1),
    ("C. 不存在的顶层段（/account/pnl → /nope/pnl）",
     b'"/account/pnl"', b'"/nope/pnl"', 1),
    ("D. 对照：仅改查询串（不应报）",
     b'"/account/grid"', b'"/account/grid?limit=5"', 0),
]

fails = []
for desc, old, new, expect in CASES:
    if old not in orig:
        print(f"  ! 锚点未命中，跳过：{desc}")
        fails.append(desc); continue
    TARGET.write_bytes(orig.replace(old, new, 1))
    rc, out = run()
    ok = (rc == expect)
    print(f"  [{'OK ' if ok else 'BAD'}] {desc} → rc={rc} (期望 {expect})")
    if not ok:
        print("      输出尾部：", out.strip().splitlines()[-3:])
        fails.append(desc)
    # 立即还原
    TARGET.write_bytes(orig)
    if sha(TARGET) != orig_sha:
        print("  !!! 还原失败，字节不一致"); fails.append(desc + " [还原失败]")

print(f"\n还原后 sha256 = {sha(TARGET)[:16]}...  一致={sha(TARGET)==orig_sha}")
print(f"结果：{'全部通过' if not fails else '失败 ' + str(fails)}")
sys.exit(0 if not fails else 1)
